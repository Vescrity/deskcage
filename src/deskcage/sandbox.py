"""Translate a profile into bwrap arguments and the sandbox environment."""

from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .profile import ENV_GROUPS, Profile


@dataclass
class Layout:
    """Host-side paths that the sandbox needs to see.

    ``env`` is the environment of the process that asked for the sandbox
    (``dcage``, ``dcage-gui`` or a test), not the daemon's own environment:
    the daemon usually outlives the shell that started it, so its copy goes
    stale.  Everything the sandbox inherits from the host — presets, display
    variables and path expansion — is resolved against this snapshot.
    """

    instance_id: str
    instance_dir: Path
    run_dir: Path
    log_dir: Path
    agent_source: Path
    home: str
    runtime_dir: str
    python: str
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    @classmethod
    def create(
        cls,
        instance_id: str,
        instance_dir: Path,
        agent_source: Path,
        log_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "Layout":
        host = dict(os.environ if env is None else env)
        home = host.get("HOME") or os.path.expanduser("~")
        runtime = host.get("XDG_RUNTIME_DIR", "")
        python = "/usr/bin/python3"
        if not os.path.exists(python):
            python = shutil.which("python3") or "python3"
        return cls(
            instance_id=instance_id,
            instance_dir=instance_dir,
            run_dir=instance_dir / "run",
            log_dir=log_dir or instance_dir / "run" / "logs",
            agent_source=agent_source,
            home=home,
            runtime_dir=runtime,
            python=python,
            env=host,
        )


def snapshot_environ() -> dict[str, str]:
    """The calling process's environment, limited to what D-Bus can carry.

    Environment entries are bytes; D-Bus strings must be valid UTF-8.  A value
    with a surrogate escape (a non-UTF-8 byte) cannot be sent, so it is left
    out rather than failing the whole launch.
    """

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        try:
            key.encode("utf-8")
            value.encode("utf-8")
        except UnicodeEncodeError:
            continue
        env[key] = value
    return env


_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expand_vars(value: str, env: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))

    return _VAR.sub(replace, value)


def expand_path(value: str, env: Mapping[str, str] | None = None) -> str:
    """Expand ``~`` and ``$VAR`` from ``env``, then make the path absolute."""

    host = os.environ if env is None else env
    text = value
    if text == "~" or text.startswith("~/"):
        home = host.get("HOME")
        text = home + text[1:] if home else os.path.expanduser(text)
    elif text.startswith("~"):
        text = os.path.expanduser(text)
    return os.path.abspath(_expand_vars(text, host))


def _runtime_socket(layout: Layout, name: str) -> str:
    return os.path.join(layout.runtime_dir, name)


def _resolv_target(path: str = "/etc/resolv.conf") -> str | None:
    """The host file behind a symlinked resolv.conf, which /run does not carry."""

    if not os.path.islink(path):
        return None
    target = os.path.realpath(path)
    return target if os.path.exists(target) else None


def _xauthority_path(layout: Layout) -> str:
    return layout.env.get("XAUTHORITY") or os.path.join(layout.home, ".Xauthority")


def environment(profile: Profile, layout: Layout, bus_address: str | None) -> dict[str, str]:
    host = layout.env
    env: dict[str, str] = {
        "HOME": layout.home,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }

    if layout.runtime_dir:
        env["XDG_RUNTIME_DIR"] = layout.runtime_dir

    if profile.x11 and host.get("DISPLAY"):
        env["DISPLAY"] = host["DISPLAY"]
        xauthority = _xauthority_path(layout)
        if os.path.exists(xauthority):
            env["XAUTHORITY"] = xauthority

    if profile.wayland:
        display = host.get("WAYLAND_DISPLAY") or "wayland-0"
        env["WAYLAND_DISPLAY"] = display

    if bus_address:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus_address

    if profile.pulse and host.get("PULSE_SERVER"):
        env["PULSE_SERVER"] = host["PULSE_SERVER"]

    if profile.pipewire:
        for key in ("PIPEWIRE_REMOTE", "PIPEWIRE_RUNTIME_DIR"):
            if host.get(key):
                env[key] = host[key]

    for group in profile.env_presets:
        for key in ENV_GROUPS[group]:
            if key in host:
                env[key] = host[key]

    for key, value in profile.env.items():
        if value is True:
            if key in host:
                env[key] = host[key]
        else:
            env[key] = value
    return env


def build(
    profile: Profile,
    layout: Layout,
    bus_address: str | None,
    keep: bool | None = None,
) -> list[str]:
    """Return the full bwrap command line, ending with the in-sandbox agent.

    Mount order is part of the contract and is documented in README.md:

    1. namespace/session flags;
    2. base skeleton (/dev, /proc, /tmp, /var, /usr, /etc, /run);
    3. home directory;
    4. runtime directory and desktop sockets;
    5. X11;
    6. devices;
    7. profile file mounts (readonly, readwrite, tmpfs, tmpbind);
    8. the control channel and its log directory, always last — and in that
       order, because ``/run/dcage/logs`` lives inside ``/run/dcage``;
    9. ``extra_args``, then environment and the agent.
    """
    args: list[str] = ["bwrap"]

    args += [
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
        "--hostname",
        profile.hostname or f"deskcage-{profile.id}",
        "--dev",
        "/dev",
        "--mqueue",
        "/dev/mqueue",
        "--dev-bind-try",
        "/dev/ntsync",
        "/dev/ntsync",
        "--tmpfs",
        "/dev/shm",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/var",
        "--symlink",
        "../run",
        "/var/run",
        "--dir",
        "/var/tmp",
        "--chmod",
        "1777",
        "/var/tmp",
    ]
    if not profile.network:
        args += ["--unshare-net"]

    args += [
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--ro-bind",
        "/etc",
        "/etc",
        "--ro-bind-try",
        "/opt",
        "/opt",
        "--ro-bind-try",
        "/sys/module",
        "/sys/module",
        "--tmpfs",
        "/run",
    ]

    resolv = _resolv_target()
    if resolv:
        args += ["--ro-bind-try", resolv, resolv]

    for mount in all_mounts(profile, layout):
        args += mount.argv()

    # Control channel, the agent itself and the shared log directory.  This is
    # always the last mounts, so a profile entry can never shadow /run/dcage.
    args += [
        "--bind",
        str(layout.run_dir),
        "/run/dcage",
        "--bind",
        str(layout.log_dir),
        "/run/dcage/logs",
        "--ro-bind",
        str(layout.agent_source),
        "/run/dcage/agent.py",
    ]

    args += list(profile.extra_args)

    # Environment.
    args += ["--clearenv"]
    for key, value in environment(profile, layout, bus_address).items():
        args += ["--setenv", key, value]

    args += ["--chdir", layout.home]

    args += [
        "--",
        layout.python,
        "/run/dcage/agent.py",
        "--socket",
        "/run/dcage/control.sock",
        "--log-dir",
        "/run/dcage/logs",
        "--keep",
        "1" if (profile.keep_alive if keep is None else keep) else "0",
    ]
    return args


# --------------------------------------------------------------------- mounts

#: Mount kinds that compete for the same destination.  ``dir`` and ``chmod``
#: are setup operations, not mounts, so they are never deduplicated.
_MOUNT_KINDS = frozenset({"ro", "rw", "dev", "tmpfs", "overlay"})


@dataclass(frozen=True)
class Mount:
    """One bwrap mount, rendered in list order."""

    kind: str
    dest: str
    source: str = ""

    def argv(self) -> list[str]:
        if self.kind == "ro":
            return ["--ro-bind-try", self.source, self.dest]
        if self.kind == "rw":
            return ["--bind-try", self.source, self.dest]
        if self.kind == "dev":
            return ["--dev-bind-try", self.source, self.dest]
        if self.kind == "tmpfs":
            return ["--tmpfs", self.dest]
        if self.kind == "overlay":
            # --overlay-src only applies to the overlay option after it, so the
            # pair must stay adjacent.
            return ["--overlay-src", self.source, "--tmp-overlay", self.dest]
        if self.kind == "dir":
            return ["--dir", self.dest]
        if self.kind == "chmod":
            return ["--chmod", self.source, self.dest]
        raise ValueError(f"unknown mount kind: {self.kind!r}")


def uses_tmpbind(profile: Profile) -> bool:
    return profile.home == "tmpbind" or bool(profile.tmpbind)


def overlay_supported() -> bool:
    """Whether the host kernel can mount overlayfs in a user namespace.

    The module may be available but not loaded; mounting inside a user
    namespace cannot trigger module autoloading, so only the loaded filesystem
    counts.
    """

    try:
        text = Path("/proc/filesystems").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.split()[-1:] == ["overlay"] for line in text.splitlines())


def tmpbind_missing(
    profile: Profile, env: Mapping[str, str] | None = None
) -> list[str]:
    """Expanded tmpbind sources that do not exist on this host."""

    return [
        path
        for path in (expand_path(value, env) for value in profile.tmpbind)
        if not os.path.exists(path)
    ]


def _home_mounts(profile: Profile, layout: Layout) -> list[Mount]:
    if profile.home == "tmp":
        return [Mount("tmpfs", layout.home)]
    if profile.home == "ro":
        return [Mount("ro", layout.home, layout.home)]
    if profile.home == "tmpbind":
        return [Mount("overlay", layout.home, layout.home)]
    return [Mount("rw", layout.home, layout.home)]


def _runtime_mounts(profile: Profile, layout: Layout) -> list[Mount]:
    if not layout.runtime_dir:
        return []
    mounts = [
        Mount("dir", layout.runtime_dir),
        Mount("chmod", layout.runtime_dir, "0700"),
    ]
    # Sockets only need connect(); a read-only bind keeps them working while
    # preventing the sandbox from replacing or unlinking them.
    if profile.wayland:
        display = layout.env.get("WAYLAND_DISPLAY") or "wayland-0"
        socket = display if os.path.isabs(display) else _runtime_socket(layout, display)
        mounts.append(Mount("ro", socket, socket))
    if profile.pipewire:
        for name in ("pipewire-0", "pipewire-0-manager"):
            socket = _runtime_socket(layout, name)
            mounts.append(Mount("ro", socket, socket))
    if profile.pulse:
        pulse = _runtime_socket(layout, "pulse")
        mounts.append(Mount("ro", pulse, pulse))
    if profile.a11y:
        atspi = _runtime_socket(layout, "at-spi")
        mounts.append(Mount("ro", atspi, atspi))
    return mounts


def _x11_mounts(profile: Profile, layout: Layout) -> list[Mount]:
    if not profile.x11:
        return []
    xauthority = layout.env.get("XAUTHORITY") or os.path.join(
        layout.home, ".Xauthority"
    )
    return [
        Mount("ro", "/tmp/.X11-unix", "/tmp/.X11-unix"),
        Mount("ro", xauthority, xauthority),
    ]


def nvidia_devices() -> list[str]:
    return sorted(set(glob.glob("/dev/nvidia*")))


def _nvidia_mounts(profile: Profile) -> list[Mount]:
    if not profile.nvidia:
        return []
    return [Mount("dev", path, path) for path in nvidia_devices()]


def _device_mounts(profile: Profile) -> list[Mount]:
    mounts: list[Mount] = []
    if profile.dri:
        mounts += [
            Mount("dev", "/dev/dri", "/dev/dri"),
            Mount("dev", "/dev/udmabuf", "/dev/udmabuf"),
            Mount("ro", "/sys/dev/char", "/sys/dev/char"),
            Mount("ro", "/sys/devices", "/sys/devices"),
        ]
    if profile.snd:
        mounts.append(Mount("dev", "/dev/snd", "/dev/snd"))
    if profile.video:
        candidates = sorted(set(["/dev/video0", "/dev/video1", *glob.glob("/dev/video*")]))
        for path in candidates:
            mounts.append(Mount("dev", path, path))
    return mounts + _nvidia_mounts(profile)


def _file_mounts(profile: Profile, layout: Layout) -> list[Mount]:
    mounts: list[Mount] = []
    for value in profile.readonly:
        path = expand_path(value, layout.env)
        mounts.append(Mount("ro", path, path))
    for value in profile.readwrite:
        path = expand_path(value, layout.env)
        mounts.append(Mount("rw", path, path))
    for value in profile.tmpfs:
        mounts.append(Mount("tmpfs", expand_path(value, layout.env)))
    for value in profile.tmpbind:
        path = expand_path(value, layout.env)
        # --overlay-src has no -try variant.  A missing source means there is
        # nothing to overlay, so skipping it is the portable behaviour.
        if not os.path.exists(path):
            continue
        mounts.append(Mount("overlay", path, path))
    return mounts


def _dedupe(mounts: list[Mount]) -> list[Mount]:
    """Keep only the last mount for a destination (later stages win)."""

    last = {
        mount.dest: index
        for index, mount in enumerate(mounts)
        if mount.kind in _MOUNT_KINDS
    }
    return [
        mount
        for index, mount in enumerate(mounts)
        if mount.kind not in _MOUNT_KINDS or last[mount.dest] == index
    ]


def all_mounts(profile: Profile, layout: Layout) -> list[Mount]:
    """Every ordered mount, with home first and profile entries last."""

    return _dedupe(
        _home_mounts(profile, layout)
        + _runtime_mounts(profile, layout)
        + _x11_mounts(profile, layout)
        + _device_mounts(profile)
        + _file_mounts(profile, layout)
    )


def _lines(mounts: list[Mount]) -> list[str]:
    return [shlex.join(mount.argv()) for mount in mounts]


def option_sections(profile: Profile, layout: Layout) -> list[tuple[str, list[str]]]:
    """The expanded arguments of each encapsulated option, for the GUI preview."""

    sections: list[tuple[str, list[str]]] = []

    sections.append(("Home", _lines(_home_mounts(profile, layout))))

    bus = [f"mode = {profile.bus}"]
    if profile.bus_pid_hint:
        bus.append("pid_hint = true")
    if profile.bus == "bridge":
        bus.append("host = notifications, tray, MPRIS")
    sections.append(("Session bus", bus))

    if profile.dri:
        sections.append(
            (
                "GPU / DRI",
                _lines(
                    [
                        Mount("dev", "/dev/dri", "/dev/dri"),
                        Mount("dev", "/dev/udmabuf", "/dev/udmabuf"),
                        Mount("ro", "/sys/dev/char", "/sys/dev/char"),
                        Mount("ro", "/sys/devices", "/sys/devices"),
                    ]
                ),
            )
        )
    if profile.snd:
        sections.append(("Sound", _lines([Mount("dev", "/dev/snd", "/dev/snd")])))
    if profile.video:
        sections.append(
            (
                "Camera",
                _lines(
                    [
                        Mount("dev", path, path)
                        for path in sorted(
                            set(["/dev/video0", "/dev/video1", *glob.glob("/dev/video*")])
                        )
                    ]
                ),
            )
        )
    if profile.nvidia:
        lines = _lines(_nvidia_mounts(profile))
        sections.append(("NVIDIA", lines or ["# no /dev/nvidia* on this host"]))
    runtime = [mount for mount in _runtime_mounts(profile, layout) if mount.kind == "ro"]

    def runtime_lines(needle: str) -> list[str]:
        lines = _lines([m for m in runtime if needle in m.source])
        return lines or ["# XDG_RUNTIME_DIR is not set"]

    if profile.wayland:
        sections.append(("Wayland", runtime_lines("wayland")))
    if profile.pipewire:
        sections.append(("PipeWire", runtime_lines("pipewire")))
    if profile.pulse:
        sections.append(("PulseAudio", runtime_lines("pulse")))
    if profile.a11y:
        sections.append(("Accessibility", runtime_lines("at-spi")))
    if profile.x11:
        sections.append(("X11", _lines(_x11_mounts(profile, layout))))

    for title, values, kind in (
        ("Read-only", profile.readonly, "ro"),
        ("Read-write", profile.readwrite, "rw"),
        ("Tmpfs", profile.tmpfs, "tmpfs"),
    ):
        if values:
            mounts = []
            for value in values:
                path = expand_path(value, layout.env)
                mounts.append(Mount(kind, path, path))
            sections.append((title, _lines(mounts)))
    if profile.tmpbind:
        lines: list[str] = []
        for value in profile.tmpbind:
            path = expand_path(value, layout.env)
            if os.path.exists(path):
                lines.append(shlex.join(Mount("overlay", path, path).argv()))
            else:
                lines.append(f"# skipped (missing): {path}")
        sections.append(("Tmpbind", lines))
    return sections


def preview_layout(instance_id: str = "preview") -> Layout:
    """A Layout with realistic paths that never touches the filesystem."""

    from . import config

    return Layout.create(
        instance_id,
        config.runtime_root() / "instances" / instance_id,
        Path(__file__).with_name("agent.py"),
        log_dir=config.logs_root() / instance_id,
    )


def preview_argv(profile: Profile, instance_id: str = "preview") -> list[str]:
    """The command ``deskcage`` would run for this profile, without launching it."""

    layout = preview_layout(instance_id)
    address = (
        None
        if profile.bus == "none"
        else f"unix:path={layout.run_dir / 'bus'}"
    )
    return build(profile, layout, address, keep=profile.keep_alive)


#: How many values each bwrap option takes.  Options that are not listed take
#: no value; unknown options are kept on their own line.
_BWRAP_ARITY = {
    "--args": 1,
    "--argv0": 1,
    "--userns": 1,
    "--userns2": 1,
    "--pidns": 1,
    "--uid": 1,
    "--gid": 1,
    "--hostname": 1,
    "--chdir": 1,
    "--setenv": 2,
    "--unsetenv": 1,
    "--lock-file": 1,
    "--sync-fd": 1,
    "--bind": 2,
    "--bind-try": 2,
    "--dev-bind": 2,
    "--dev-bind-try": 2,
    "--ro-bind": 2,
    "--ro-bind-try": 2,
    "--bind-fd": 2,
    "--ro-bind-fd": 2,
    "--remount-ro": 1,
    "--overlay-src": 1,
    "--overlay": 3,
    "--tmp-overlay": 1,
    "--ro-overlay": 1,
    "--exec-label": 1,
    "--file-label": 1,
    "--proc": 1,
    "--dev": 1,
    "--tmpfs": 1,
    "--mqueue": 1,
    "--dir": 1,
    "--file": 2,
    "--bind-data": 2,
    "--ro-bind-data": 2,
    "--symlink": 2,
    "--seccomp": 1,
    "--add-seccomp-fd": 1,
    "--block-fd": 1,
    "--userns-block-fd": 1,
    "--info-fd": 1,
    "--json-status-fd": 1,
    "--cap-add": 1,
    "--cap-drop": 1,
    "--perms": 1,
    "--size": 1,
    "--chmod": 2,
}


def _overlay_group(argv: list[str], index: int) -> tuple[list[str], int]:
    """Keep --overlay-src chains and their overlay target in one group."""

    group = [argv[index], argv[index + 1]]
    index += 2
    while index + 1 < len(argv) and argv[index] == "--overlay-src":
        group += [argv[index], argv[index + 1]]
        index += 2
    if index < len(argv) and argv[index] in ("--tmp-overlay", "--ro-overlay"):
        group += [argv[index], argv[index + 1]]
        index += 2
    elif index < len(argv) and argv[index] == "--overlay":
        group += argv[index : index + 4]
        index += 4
    return group, index


def command_lines(argv: list[str]) -> list[str]:
    """Split a bwrap argv into one shell-quoted line per option group."""

    if not argv:
        return []
    lines: list[str] = []
    index = 0
    if argv[0] == "bwrap":
        lines.append("bwrap")
        index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            lines.append(shlex.join(argv[index:]))
            break
        if token == "--overlay-src" and index + 1 < len(argv):
            group, index = _overlay_group(argv, index)
            lines.append(shlex.join(group))
            continue
        if token.startswith("-"):
            take = _BWRAP_ARITY.get(token, 0)
            group = argv[index : index + 1 + take]
            lines.append(shlex.join(group))
            index += len(group)
        else:
            lines.append(shlex.join([token]))
            index += 1
    return lines


def format_argv(argv: list[str], indent: str = "  ") -> str:
    """A runnable multiline rendering: one option group per line."""

    lines = command_lines(argv)
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    return " \\\n".join([lines[0]] + [indent + line for line in lines[1:]])


