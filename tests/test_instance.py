"""Real bubblewrap instances: homes, exit codes and bus modes."""

from __future__ import annotations

import sys
import time

import harness

from deskcage import config
from deskcage.instance import Instance, InstanceError
from deskcage.profile import Profile


def wait_status(instance: Instance, pid: int, timeout: float = 20.0) -> int:
    harness.wait_until(lambda: instance.process(pid).status is not None, timeout)
    info = instance.process(pid)
    assert info is not None and info.status is not None, "process did not exit"
    return info.status


def log_of(instance: Instance, pid: int) -> str:
    info = instance.process(pid)
    assert info is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return open(info.log, encoding="utf-8", errors="replace").read()
        except OSError:
            time.sleep(0.02)
    return ""


def wait_state(instance: Instance, states: tuple[str, ...], timeout: float = 20.0) -> None:
    harness.wait_until(lambda: instance.state in states, timeout)
    assert instance.state in states, f"state is {instance.state}"


def test_none(env: harness.TestEnv) -> None:
    profile = Profile(id="none", home="tmp", bus="none", keep_alive=False)
    instance = Instance("none-1", profile, env.address)
    instance.start()
    try:
        script = (
            "echo home=$HOME; "
            "test -w $HOME && echo home-writable; "
            "test -d /run/dcage && echo control-ok; "
            "test -S /run/dcage/control.sock && echo agent-socket; "
            "test -z \"$DBUS_SESSION_BUS_ADDRESS\" && echo no-bus; "
            "python3 -c 'import pty; pty.openpty()' && echo pty-ok; "
            "exit 9"
        )
        info = instance.spawn(["sh", "-c", script])
        assert wait_status(instance, info.pid) == 9
        text = log_of(instance, info.pid)
        assert "home-writable" in text, text
        assert "control-ok" in text, text
        assert "no-bus" in text, text
        assert "pty-ok" in text, text
        assert instance.bus is not None and instance.bus.address is None
        # keep_alive=False: the whole sandbox goes away after the last child.
        wait_state(instance, ("exited", "failed"))
        assert instance.proc is not None
        assert instance.proc.poll() is not None
    finally:
        instance.stop()


def test_isolated(env: harness.TestEnv) -> None:
    profile = Profile(id="iso", home="tmp", bus="isolated", keep_alive=True)
    instance = Instance("iso-1", profile, env.address)
    instance.start()
    try:
        info = instance.spawn(
            [
                "sh",
                "-c",
                "dbus-send --session --print-reply --dest=org.freedesktop.DBus "
                "/org/freedesktop/DBus org.freedesktop.DBus.ListNames; exit 0",
            ]
        )
        assert wait_status(instance, info.pid) == 0
        text = log_of(instance, info.pid)
        assert "org.freedesktop.DBus" in text, text
        assert "org.freedesktop.Notifications" not in text, text
    finally:
        instance.stop()
    assert not (instance.run_dir / "bus").exists()


def test_bridge(env: harness.TestEnv) -> None:
    if not config.find_bus_bridge():
        print("bridge: skipped (no bus-bridge)")
        return
    profile = Profile(id="br", home="tmp", bus="bridge", keep_alive=True)
    instance = Instance("br-1", profile, env.address)
    instance.start()
    try:
        info = instance.spawn(
            [
                "sh",
                "-c",
                "dbus-send --session --print-reply --dest=org.freedesktop.DBus "
                "/org/freedesktop/DBus org.freedesktop.DBus.ListNames; exit 0",
            ]
        )
        assert wait_status(instance, info.pid) == 0
        text = log_of(instance, info.pid)
        assert "org.freedesktop.DBus" in text, text
        assert "org.freedesktop.Notifications" in text, text
        assert "org.kde.StatusNotifierWatcher" in text, text
    finally:
        instance.stop()


def test_tmpbind_strict(env: harness.TestEnv) -> None:
    profile = Profile(
        id="strict",
        bus="none",
        home="tmp",
        tmpbind=["/no/such/tmpbind/path"],
        tmpbind_strict=True,
    )
    instance = Instance("strict-1", profile, env.address)
    try:
        instance.start()
    except InstanceError as exc:
        assert "tmpbind_strict" in str(exc), exc
    else:
        raise AssertionError("strict tmpbind profile started")


def test_release(env: harness.TestEnv) -> None:
    """An exited instance frees its name and runtime dir but keeps its logs."""

    profile = Profile(id="release", home="tmp", bus="isolated", keep_alive=False)
    instance = Instance("release-1", profile, env.address)
    instance.start()
    info = instance.spawn(["sh", "-c", "echo released-line; exit 3"])
    assert wait_status(instance, info.pid) == 3
    assert harness.wait_until(lambda: instance.released, 20), "instance was not released"
    exits = [payload for kind, payload in instance.take_events() if kind == "exit"]
    assert exits == [(info.pid, 3)], exits
    assert instance.take_events() == [], "an exit was announced twice"
    assert not instance.dir.exists(), instance.dir
    assert instance.log_dir.is_dir(), instance.log_dir
    assert (instance.log_dir / "bus.log").is_file()
    assert "released-line" in log_of(instance, info.pid)


def main() -> int:
    with harness.TestEnv() as env:
        test_none(env)
        test_isolated(env)
        test_bridge(env)
        test_tmpbind_strict(env)
        test_release(env)
    print("instance: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
