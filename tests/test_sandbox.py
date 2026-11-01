"""bwrap argument builder."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import harness  # noqa: F401 - sets sys.path

from deskcage import sandbox
from deskcage.profile import Profile


def layout(tmp: Path) -> sandbox.Layout:
    run = tmp / "run"
    run.mkdir()
    base = sandbox.Layout.create("test-1", tmp, Path("/tmp/agent.py"))
    base.runtime_dir = str(run)
    return base


def arg_value(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag and i + 1 < len(argv)]


def index_of(argv: list[str], flag: str, value: str) -> int:
    for index in range(len(argv) - 1):
        if argv[index] == flag and argv[index + 1] == value:
            return index
    raise AssertionError(f"{flag} {value} not found")


def mount_order() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="deskcage-order-"))
    base = layout(tmp)
    overlay = tmp / "overlay"
    overlay.mkdir()
    missing = tmp / "missing"
    display = os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    wayland = display if os.path.isabs(display) else os.path.join(base.runtime_dir, display)

    profile = Profile(
        id="order",
        home="tmp",
        bus="none",
        readonly=["/tmp/ro", "/tmp/same"],
        readwrite=["/tmp/rw", "/tmp/same"],
        tmpfs=["/tmp/tmpfs"],
        tmpbind=[str(overlay), str(missing)],
        wayland=True,
        pipewire=True,
        pulse=True,
        a11y=True,
        x11=True,
        dri=True,
        snd=True,
        video=True,
        nvidia=True,
    )
    real_glob = sandbox.glob.glob
    sandbox.glob.glob = lambda pattern: (
        ["/dev/nvidia0", "/dev/nvidiactl"]
        if pattern == "/dev/nvidia*"
        else real_glob(pattern)
    )
    try:
        argv = sandbox.build(profile, base, None)
        sections = dict(sandbox.option_sections(profile, base))
    finally:
        sandbox.glob.glob = real_glob

    # bwrap creates its own devpts for --dev; binding the host one breaks ptys.
    assert not any(
        argv[index] == "--dev-bind-try" and argv[index + 1] == "/dev/pts"
        for index in range(len(argv) - 1)
    ), argv

    assert index_of(argv, "--ro-bind-try", "/opt") < argv.index("--setenv")
    assert index_of(argv, "--ro-bind-try", "/sys/module") < argv.index("--setenv")
    assert arg_value(argv, "--mqueue") == ["/dev/mqueue"]
    assert index_of(argv, "--dev-bind-try", "/dev/udmabuf") > index_of(
        argv, "--dev-bind-try", "/dev/dri"
    )
    assert any("udmabuf" in line for line in sections["GPU / DRI"]), sections

    atspi = os.path.join(base.runtime_dir, "at-spi")
    assert index_of(argv, "--ro-bind-try", atspi) > index_of(argv, "--ro-bind-try", wayland)
    assert index_of(argv, "--dev-bind-try", "/dev/nvidia0") > index_of(
        argv, "--dev-bind-try", "/dev/dri"
    )
    assert "--ro-bind-try" in sections["Accessibility"][0], sections
    assert any("nvidia" in line for line in sections["NVIDIA"]), sections
    assert "nvidia" not in str(sandbox.option_sections(Profile(id="d"), base))

    # --overlay-src only applies to the next overlay option.
    index = argv.index("--overlay-src")
    assert argv[index + 1] == str(overlay), argv[index : index + 4]
    assert argv[index + 2] == "--tmp-overlay", argv[index : index + 4]
    assert argv[index + 3] == str(overlay), argv[index : index + 4]
    # Missing sources have no -try variant and are skipped.
    assert str(missing) not in argv

    # Later stages win for the same destination (readwrite beats readonly).
    indices = [i for i, item in enumerate(argv) if item == "/tmp/same"]
    assert len(indices) == 2, indices
    assert argv[indices[0] - 1] == "--bind-try", argv[indices[0] - 3 : indices[0] + 2]

    # Sockets are read-only bind mounts and still connectable.
    assert index_of(argv, "--ro-bind-try", wayland) < index_of(
        argv, "--ro-bind-try", "/tmp/.X11-unix"
    )
    assert "--bind-try" not in [
        argv[i - 1] for i, item in enumerate(argv) if item == wayland
    ]

    stages = [
        index_of(argv, "--tmpfs", sandbox.expand_path("~")),
        index_of(argv, "--ro-bind-try", wayland),
        index_of(argv, "--ro-bind-try", "/tmp/.X11-unix"),
        index_of(argv, "--dev-bind-try", "/dev/dri"),
        index_of(argv, "--ro-bind-try", "/tmp/ro"),
        argv.index("--overlay-src"),
        index_of(argv, "--bind", str(base.run_dir)),
    ]
    assert stages == sorted(stages), stages

    sections = dict(sandbox.option_sections(profile, base))
    assert any("--overlay-src" in line for line in sections["Tmpbind"])
    assert any("skipped (missing)" in line for line in sections["Tmpbind"])
    assert "Wayland" in sections and "--ro-bind-try" in sections["Wayland"][0]

    whole = Profile(id="whole", home="tmpbind", bus="none")
    argv = sandbox.build(whole, base, None)
    index = argv.index("--overlay-src")
    assert argv[index + 1] == sandbox.expand_path("~"), argv[index : index + 4]
    assert sandbox.preview_argv(whole)[0] == "bwrap"
    assert sandbox.uses_tmpbind(whole)
    assert sandbox.uses_tmpbind(profile)

    # The command view groups every option with its values, one group per line.
    lines = sandbox.command_lines(argv)
    assert lines[0] == "bwrap", lines[:3]
    assert any(
        line.startswith("--setenv HOME ") and line.count(" ") >= 2 for line in lines
    ), [line for line in lines if line.startswith("--setenv")][:2]
    assert any(
        line.startswith("--overlay-src ") and "--tmp-overlay" in line
        for line in lines
    ), lines
    assert any(line.startswith("--chmod 1777 /var/tmp") for line in lines), lines
    text = sandbox.format_argv(argv)
    assert text.startswith("bwrap \\\n  --"), text[:60]
    assert text.count("\\\n") == len(lines) - 1, text[:200]


def resolv_symlink() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="deskcage-resolv-"))
    target = tmp / "stub-resolv.conf"
    target.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    link = tmp / "resolv.conf"
    link.symlink_to(target)
    assert sandbox._resolv_target(str(link)) == str(target)

    plain = tmp / "plain-resolv.conf"
    plain.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
    assert sandbox._resolv_target(str(plain)) is None
    link.unlink()
    link.symlink_to(tmp / "missing")
    assert sandbox._resolv_target(str(link)) is None


def main() -> int:
    mount_order()
    resolv_symlink()
    tmp = Path(tempfile.mkdtemp(prefix="deskcage-argv-"))
    base = layout(tmp)
    profile = Profile(
        id="demo",
        home="bind",
        network=False,
        bus="isolated",
        readonly=["~/ro"],
        readwrite=["~/rw"],
        tmpfs=["/tmp/scratch"],
        x11=False,
        wayland=True,
        pipewire=False,
        pulse=False,
        extra_args=["--unshare-uts"],
        env={"EXTRA": "1"},
    )
    argv = sandbox.build(profile, base, "unix:path=/run/dcage/bus", keep=False)
    assert argv[0] == "bwrap"
    assert "--unshare-net" in argv
    assert "--unshare-user" in argv and "--unshare-pid" in argv
    assert "--die-with-parent" in argv
    assert "demo" in arg_value(argv, "--hostname")[0]
    assert os.path.expanduser("~") in arg_value(argv, "--bind-try")
    for expected in ("~/ro", "~/rw", "/tmp/scratch"):
        assert sandbox.expand_path(expected) in argv, expected
    assert "--unshare-uts" in argv
    assert arg_value(argv, "--keep") == ["0"]
    assert arg_value(argv, "--setenv")[0] == "HOME"
    assert "EXTRA" in argv and "1" in argv[argv.index("EXTRA") :]

    env = sandbox.environment(profile, base, "unix:path=/run/dcage/bus")
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/dcage/bus"
    assert env["EXTRA"] == "1"
    assert env["HOME"] == base.home
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "TMPDIR" not in env and "USER" not in env and "LOGNAME" not in env
    assert "DISPLAY" not in env
    assert env["WAYLAND_DISPLAY"] == (
        os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    )

    # ``true`` inherits from the host, a string is used verbatim, and a
    # variable the host does not define is simply not passed.
    os.environ["DESKCAGE_TEST_PASS"] = "host-value"
    try:
        inherited = Profile(id="inherit", env={"DESKCAGE_TEST_PASS": True})
        resolved = sandbox.environment(inherited, base, None)
        assert resolved["DESKCAGE_TEST_PASS"] == "host-value"
        missing = sandbox.environment(
            Profile(id="missing", env={"DESKCAGE_TEST_MISSING": True}), base, None
        )
        assert "DESKCAGE_TEST_MISSING" not in missing
        literal = sandbox.environment(
            Profile(id="literal", env={"DESKCAGE_TEST_PASS": "own"}), base, None
        )
        assert literal["DESKCAGE_TEST_PASS"] == "own"
    finally:
        os.environ.pop("DESKCAGE_TEST_PASS", None)

    saved = {key: os.environ.get(key) for key in ("DISPLAY", "XAUTHORITY")}
    try:
        authority = tmp / "Xauthority"
        authority.write_text("", encoding="utf-8")
        os.environ["DISPLAY"] = ":0"
        os.environ["XAUTHORITY"] = str(authority)
        x11 = Profile(id="x11", x11=True)
        assert sandbox.environment(x11, base, None)["XAUTHORITY"] == str(authority)
        assert index_of(
            sandbox.build(x11, base, None), "--ro-bind-try", str(authority)
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    notmp = Profile(id="tmp", home="tmp")
    argv = sandbox.build(notmp, base, None, keep=True)
    assert arg_value(argv, "--keep") == ["1"]
    assert os.path.expanduser("~") in arg_value(argv, "--tmpfs")
    assert "DBUS_SESSION_BUS_ADDRESS" not in sandbox.environment(notmp, base, None)

    # Explicit extra args come after our own mounts so callers can add more.
    print("sandbox: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
