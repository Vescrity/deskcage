"""CLI end-to-end: profiles, start, reuse, exec, logs, keep, stop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib

import harness

from deskcage import config
from deskcage.cli import live_rows
from deskcage.client import ManagerClient, ManagerError
from deskcage.profile import Profile


def dbus_contract() -> None:
    """The XML, the client and the daemon must agree on Launch's arguments."""

    from gi.repository import Gio

    from deskcage import daemon

    node = Gio.DBusNodeInfo.new_for_xml(daemon.XML)
    interface = node.lookup_interface(daemon.INTERFACE)
    method = interface.lookup_method("Launch")
    signature = "".join(arg.signature for arg in method.in_args)
    assert signature == "ssbasa{ss}sba{ss}", signature

    rename = interface.lookup_method("RenameProfile")
    assert "".join(arg.signature for arg in rename.in_args) == "ss"
    assert "".join(arg.signature for arg in rename.out_args) == "s"


def launch_carries_caller_env() -> None:
    """Launch sends the calling process's environment alongside the profile."""

    from gi.repository import GLib

    client = ManagerClient.__new__(ManagerClient)
    seen: dict[str, tuple] = {}

    def fake_call(method, params, reply, timeout=60000):
        seen["payload"] = params.unpack()
        return GLib.Variant("(s)", ("demo",))

    client.call = fake_call
    os.environ["DESKCAGE_LAUNCH_SENTINEL"] = "from-caller"
    try:
        assert client.launch("p") == "demo"
        payload = seen["payload"]
        assert payload[-1]["DESKCAGE_LAUNCH_SENTINEL"] == "from-caller", payload
        assert client.launch("p", caller_env={"HOME": "/x"}) == "demo"
        assert seen["payload"][-1] == {"HOME": "/x"}, seen["payload"]
    finally:
        os.environ.pop("DESKCAGE_LAUNCH_SENTINEL", None)


def live_filter() -> None:
    """`ps` never lists an instance that is only waiting to be reaped."""

    rows = [
        ("a", "p", "running", "", True, 1),
        ("b", "p", "exited", "", False, 2),
        ("c", "p", "stopped", "", False, 3),
        ("d", "p", "starting", "", False, 4),
        ("e", "p", "failed", "", False, 5),
        ("f", "p", "new", "", False, 6),
    ]
    assert [row[0] for row in live_rows(rows)] == ["a", "d", "f"]


def follow_logs(env: harness.TestEnv) -> None:
    """`dcage logs -f` streams while the instance runs and stops when it ends."""

    path = env.dir / "follow.out"
    instance = env.cli("start", "t", "--instance", "follow").stdout.strip()
    with open(path, "wb") as handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "deskcage", "logs", "-f", instance],
            env=env.env(),
            stdout=handle,
            stderr=subprocess.DEVNULL,
        )
    try:
        assert harness.wait_until(lambda: proc.poll() is None, 5)
        env.cli("exec", instance, "--", "sh", "-c", "echo followed-line")
        assert harness.wait_until(
            lambda: "followed-line" in path.read_text(errors="replace"), 10
        ), path.read_text(errors="replace")
        env.cli("stop", instance)
        proc.wait(timeout=30)
        assert proc.returncode == 0, proc.returncode
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert "followed-line" in path.read_text(errors="replace")

    # Ctrl-C returns 130 instead of hanging or printing a traceback.
    instance = env.cli("start", "t", "--instance", "interrupt").stdout.strip()
    proc = subprocess.Popen(
        [sys.executable, "-m", "deskcage", "logs", "-f", instance],
        env=env.env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        harness.wait_until(lambda: proc.poll() is None, 5)
        time.sleep(0.5)
        proc.send_signal(2)
        _, err = proc.communicate(timeout=30)
        assert proc.returncode == 130, (proc.returncode, err)
        assert "Traceback" not in err, err
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        env.cli("stop", instance)

    # Following a released instance just prints what is on disk.
    done = env.cli("logs", "-f", "follow")
    assert done.returncode == 0, done
    assert "followed-line" in done.stdout, done.stdout


def daemon_log_location() -> None:
    """The daemon log lives in the runtime dir and is opt-in on disk."""
    path = config.daemon_log_path()
    assert path == config.runtime_root() / "dcaged.log", path
    assert path.is_file(), path
    assert "profiles" not in path.parts and "state" not in path.parts, path

    saved = os.environ.get("DESKCAGE_DAEMON_LOG")
    try:
        os.environ["DESKCAGE_DAEMON_LOG"] = "off"
        assert str(config.daemon_log_path()) == os.devnull
        os.environ["DESKCAGE_DAEMON_LOG"] = "~/dcaged.log"
        assert str(config.daemon_log_path()) == os.path.expanduser("~/dcaged.log")
    finally:
        if saved is None:
            os.environ.pop("DESKCAGE_DAEMON_LOG", None)
        else:
            os.environ["DESKCAGE_DAEMON_LOG"] = saved


def main() -> int:
    os.environ.pop("DESKCAGE_CALLER_ENV", None)
    dbus_contract()
    launch_carries_caller_env()
    live_filter()
    with harness.TestEnv() as env:
        assert env.cli("daemon", "--stop", check=False).returncode == 1
        assert "running" in env.cli("daemon", "--detach").stdout
        assert env.daemon_pid().isdigit()
        daemon_log_location()
        assert not (env.state_home / "dcage" / "dcaged.log").exists()

        assert "no profiles" in env.cli("profile", "list").stdout

        env.cli("profile", "create", "nv", "--nvidia", "--a11y")
        extra = tomllib.loads(env.cli("profile", "show", "nv").stdout)
        assert extra["devices"]["nvidia"] is True, extra
        assert extra["desktop"]["a11y"] is True, extra
        env.cli("profile", "delete", "nv")

        assert "stopped" in env.cli("daemon", "--stop").stdout

        out = env.cli("profile", "create", "t", "--bus", "isolated",
                      "--command", "echo", "--command", "profile-command")
        assert out.stdout.strip() == "t", out

        listing = env.cli("profile", "list").stdout
        assert "t" in listing, listing

        duplicate = env.cli("profile", "create", "t", check=False)
        assert duplicate.returncode != 0 and "already exists" in duplicate.stderr

        shown = env.cli("profile", "show", "t").stdout
        assert 'mode = "isolated"' in shown, shown
        assert 'command = [\n    "echo",\n    "profile-command",\n]' in shown, shown
        assert "[env-preset]\nxdg = true\nsession = true\n" in shown, shown
        assert "LANG = true" not in shown, shown

        copied = env.cli("profile", "duplicate", "t", "t-copy").stdout.strip()
        assert copied == "t-copy", copied
        assert env.cli("profile", "path", "t-copy").stdout.strip().endswith(
            "t-copy.toml"
        )
        env.cli("profile", "delete", "t-copy")

        # Renaming moves the profile, keeps its content and frees the old name.
        env.cli("profile", "create", "rn", "--bus", "none")
        renamed = env.cli("profile", "rename", "rn", "rn2")
        assert renamed.stdout.strip() == "rn2", renamed
        assert not (env.config_home / "dcage" / "profiles" / "rn.toml").exists()
        assert (env.config_home / "dcage" / "profiles" / "rn2.toml").is_file()
        assert 'mode = "none"' in env.cli("profile", "show", "rn2").stdout
        assert env.cli("profile", "show", "rn", check=False).returncode != 0
        # Renaming onto a taken name fails, and so does an unusable id.
        taken = env.cli("profile", "rename", "rn2", "t", check=False)
        assert taken.returncode != 0 and "already exists" in taken.stderr
        unknown = env.cli("profile", "rename", "nope", "x", check=False)
        assert unknown.returncode != 0 and "unknown profile" in unknown.stderr
        invalid = env.cli("profile", "rename", "rn2", "bad id", check=False)
        assert invalid.returncode != 0 and "invalid profile id" in invalid.stderr
        # Renaming to the same id is a no-op, not an error.
        assert env.cli("profile", "rename", "rn2", "rn2").stdout.strip() == "rn2"
        env.cli("profile", "delete", "rn2")

        # Inheritance follows the process that starts the sandbox, not the
        # daemon.  The daemon was started above without this variable, so
        # only the caller's environment can supply it.
        profiles = env.config_home / "dcage" / "profiles"
        profiles.mkdir(parents=True, exist_ok=True)
        (profiles / "caller-env.toml").write_text(
            Profile(
                id="caller-env",
                bus="none",
                keep_alive=False,
                command=["sh", "-c", "echo caller=$DESKCAGE_CALLER_ENV"],
                env={"DESKCAGE_CALLER_ENV": True},
            ).to_toml(),
            encoding="utf-8",
        )
        os.environ["DESKCAGE_CALLER_ENV"] = "from-cli"
        try:
            out = env.cli("start", "caller-env", "--instance", "caller-env-1")
            assert out.stdout.strip() == "caller-env-1", out
            assert harness.wait_until(
                lambda: not (
                    env.runtime_dir / "dcage" / "instances" / "caller-env-1"
                ).exists(),
                10,
            )
            assert "caller=from-cli" in env.cli("logs", "caller-env-1").stdout
        finally:
            os.environ.pop("DESKCAGE_CALLER_ENV", None)
        out = env.cli("start", "caller-env", "--instance", "caller-env-2")
        assert out.stdout.strip() == "caller-env-2", out
        assert harness.wait_until(
            lambda: not (
                env.runtime_dir / "dcage" / "instances" / "caller-env-2"
            ).exists(),
            10,
        )
        second = env.cli("logs", "caller-env-2").stdout
        assert "caller=\n" in second or second.rstrip().endswith("caller="), second
        env.cli("profile", "delete", "caller-env")

        # The exact command a profile would run, for scripts and bug reports.
        command = env.cli("profile", "command", "t")
        assert command.stdout.startswith("bwrap \\\n"), command.stdout
        assert "\n  --unshare-user" in command.stdout, command.stdout
        one_line = env.cli("profile", "command", "t", "--one-line")
        assert one_line.stdout.startswith("bwrap ")
        assert one_line.stdout.count("\n") == 1, one_line.stdout
        argv = json.loads(env.cli("profile", "command", "t", "--json").stdout)
        assert argv[0] == "bwrap" and "--" in argv, argv

        # A profile command runs in a fresh instance; its output ends up in the
        # per-instance log directory and survives the instance.
        out = env.cli("start", "t", "--instance", "once", "--no-keep")
        assert out.stdout.strip() == "once", out
        assert harness.wait_until(
            lambda: "profile-command" in env.cli("logs", "once").stdout, 10
        ), env.cli("logs", "once").stdout
        assert harness.wait_until(
            lambda: (env.runtime_dir / "dcage" / "instances" / "once").exists() is False,
            10,
        )
        assert "once" not in env.cli("ps").stdout
        assert (env.runtime_dir / "dcage" / "logs" / "once").is_dir()

        # Long-lived instance + exec + logs.
        out = env.cli("start", "t", "--", "sleep", "60")
        instance = out.stdout.strip()
        assert instance.startswith("t-"), out
        ps = env.cli("ps").stdout
        assert instance in ps and "running" in ps, ps

        out = env.cli("exec", instance, "--", "sh", "-c", "echo from-exec; exit 4",
                      check=False)
        assert out.returncode == 4, (out.returncode, out.stdout, out.stderr)
        assert "from-exec" in out.stdout, out.stdout

        logs = env.cli("logs", instance).stdout
        assert "from-exec" in logs, logs

        # --env KEY inherits from the invoking shell, KEY=VALUE is literal.
        os.environ["DESKCAGE_PASS_TEST"] = "from-host"
        try:
            out = env.cli(
                "exec", instance, "--env", "DESKCAGE_PASS_TEST", "--",
                "sh", "-c", "echo pass=$DESKCAGE_PASS_TEST",
            )
            assert "pass=from-host" in out.stdout, out.stdout
            out = env.cli(
                "exec", instance, "--env", "DESKCAGE_PASS_TEST=own", "--",
                "sh", "-c", "echo pass=$DESKCAGE_PASS_TEST",
            )
            assert "pass=own" in out.stdout, out.stdout
            bad = env.cli(
                "exec", instance, "--env", "DESKCAGE_UNSET_TEST", "--", "true",
                check=False,
            )
            assert bad.returncode != 0 and "not set" in bad.stderr, bad.stderr
        finally:
            os.environ.pop("DESKCAGE_PASS_TEST", None)

        info = json.loads(env.cli("inspect", instance).stdout)
        assert info["profile"] == "t" and info["state"] == "running", info
        assert any(p["argv"] == ["sleep", "60"] for p in info["processes"]), info

        env.cli("keep", instance, "off")
        assert json.loads(env.cli("inspect", instance).stdout)["keep"] is False

        env.cli("stop", instance)
        assert instance not in env.cli("ps").stdout
        assert "no instances" in env.cli("ps").stdout
        assert not (env.runtime_dir / "dcage" / "instances" / instance).exists()

        # --reuse runs another command in the running instance instead of
        # failing on the name; the instance and its logs stay the same.
        out = env.cli("start", "t", "--instance", "reuse", "--reuse", "--no-keep",
                      "--", "sleep", "60")
        assert out.stdout.strip() == "reuse", out
        out = env.cli(
            "start", "t", "--instance", "reuse", "--reuse", "--",
            "sh", "-c", "echo reused-command",
        )
        assert out.stdout.strip() == "reuse", out
        assert "reused instance reuse" in out.stderr, out.stderr
        assert harness.wait_until(
            lambda: "reused-command" in env.cli("logs", "reuse").stdout, 10
        ), env.cli("logs", "reuse").stdout
        ps = env.cli("ps").stdout
        assert ps.count("reuse") == 1, ps

        # Reusing never changes the keep-alive flag of the instance it finds.
        out = env.cli("start", "t", "--instance", "reuse", "--reuse", "--keep",
                      "--", "true")
        assert out.stdout.strip() == "reuse", out
        assert json.loads(env.cli("inspect", "reuse").stdout)["keep"] is False

        # Renaming a profile follows into running instances: ps shows the new
        # name and --reuse matches the instance again.
        env.cli("profile", "create", "live", "--bus", "none",
                "--command", "sleep", "--command", "60")
        live = env.cli("start", "live", "--instance", "live-1").stdout.strip()
        assert live == "live-1", live
        assert env.cli("profile", "rename", "live", "live2").stdout.strip() == "live2"
        rows = [line.split() for line in env.cli("ps").stdout.splitlines()[1:]]
        assert [row[1] for row in rows if row[0] == live] == ["live2"], rows
        out = env.cli("start", "live2", "--instance", live, "--reuse", "--", "true")
        assert out.stdout.strip() == live, out
        env.cli("stop", live)
        env.cli("profile", "delete", "live2")

        # The sandbox belongs to its profile; borrowing a name across
        # profiles would hide which environment the command lands in.
        env.cli("profile", "create", "other", "--bus", "isolated")
        bad = env.cli(
            "start", "other", "--instance", "reuse", "--reuse", "--", "true",
            check=False,
        )
        assert bad.returncode != 0 and "belongs to profile" in bad.stderr, bad.stderr
        env.cli("profile", "delete", "other")

        # Without --reuse the same name is still taken.
        bad = env.cli("start", "t", "--instance", "reuse", "--", "true", check=False)
        assert bad.returncode != 0 and "already exists" in bad.stderr, bad.stderr

        # Stopping frees the name and the runtime directory, keeping the logs.
        env.cli("stop", "reuse")
        assert "reuse" not in env.cli("ps").stdout
        assert not (env.runtime_dir / "dcage" / "instances" / "reuse").exists()
        assert (env.runtime_dir / "dcage" / "logs" / "reuse").is_dir()
        assert "reused-command" in env.cli("logs", "reuse").stdout

        # The same name starts over with a clean log directory.
        out = env.cli("start", "t", "--instance", "reuse", "--reuse", "--", "sleep", "60")
        assert out.stdout.strip() == "reuse", out
        assert harness.wait_until(
            lambda: "reused-command" not in env.cli("logs", "reuse").stdout, 10
        ), env.cli("logs", "reuse").stdout
        env.cli("stop", "reuse")

        # Logs can be listed for every instance at once.
        everything = env.cli("logs").stdout
        assert "profile-command" in everything, everything

        # Following waits on signals and file monitors, not on a timer.
        follow_logs(env)

        # A pid in an instance that is gone can never signal again.
        client = ManagerClient.connect()
        try:
            client.wait_process_exit("no-such-instance", 12345)
        except ManagerError as exc:
            assert "is gone" in str(exc), exc
        else:
            raise AssertionError("waiting for a pid in a missing instance did not fail")

        # Prefix matching for instance ids.
        out = env.cli("start", "t", "--", "sleep", "60")
        full = out.stdout.strip()
        prefix = full[:-1]
        env.cli("stop", prefix)

        # Unknown names are a clean failure, not a traceback.
        bad = env.cli("stop", "no-such-instance", check=False)
        assert bad.returncode != 0 and "unknown instance" in bad.stderr, bad.stderr
        bad = env.cli("start", "--instance", "../escape", "t", "--", "true",
                      check=False)
        assert bad.returncode != 0 and "invalid instance id" in bad.stderr, bad.stderr

        # Profile deletion.
        env.cli("profile", "delete", "t")
        remaining = env.cli("profile", "list").stdout
        assert not any(line.split()[:1] == ["t"] for line in remaining.splitlines()), remaining

        doctor = env.cli("doctor")
        assert "dcaged               running" in doctor.stdout, doctor.stdout
        assert "overlayfs" in doctor.stdout, doctor.stdout
        assert (
            f"daemon log           {env.runtime_dir / 'dcage' / 'dcaged.log'}"
            in doctor.stdout
        ), doctor.stdout
    print("cli: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
