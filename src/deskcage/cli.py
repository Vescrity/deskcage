"""Command line interface for deskcage."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from gi.repository import Gio, GLib

from . import __version__, config, logs, sandbox
from .client import (
    BUS_NAME,
    ManagerClient,
    ManagerError,
    name_has_owner,
    start_daemon,
    wait_name,
)
from .profile import Profile, ProfileError


# --------------------------------------------------------------------- helpers


def fail(message: str) -> int:
    print(f"dcage: {message}", file=sys.stderr)
    return 1


def parse_env(values: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in values or []:
        if "=" in item:
            key, value = item.split("=", 1)
        else:
            key = item
            if key not in os.environ:
                raise ManagerError(f"invalid --env {item!r}: not set on the host")
            value = os.environ[key]
        if not key:
            raise ManagerError(f"invalid --env {item!r}: empty key")
        env[key] = value
    return env


def split_command(values: list[str] | None) -> list[str]:
    """``-- cmd arg`` gives a list already; also accept a single shell string."""
    if not values:
        return []
    if len(values) == 1 and " " in values[0]:
        return shlex.split(values[0])
    return list(values)


def check_tmpbind(profile: Profile) -> str | None:
    """Warn about skipped tmpbind paths; return an error for strict profiles."""

    if not sandbox.uses_tmpbind(profile):
        return None
    missing = sandbox.tmpbind_missing(profile)
    if missing and profile.tmpbind_strict:
        return (
            "tmpbind source(s) do not exist and files.tmpbind_strict is on: "
            + ", ".join(missing)
        )
    if missing:
        print(
            "dcage: warning: skipping missing tmpbind source(s): "
            + ", ".join(missing),
            file=sys.stderr,
        )
    if not sandbox.overlay_supported():
        return (
            "tmpbind needs overlayfs, but the kernel does not provide it to "
            "user namespaces on this host (try: modprobe overlay)"
        )
    return None


def resolve_instance(client: ManagerClient, token: str) -> str:
    rows = client.list_instances()
    ids = [row[0] for row in rows]
    if token in ids:
        return token
    matches = [item for item in ids if item.startswith(token)]
    running = [
        row[0] for row in rows
        if row[0].startswith(token) and row[2] in ("new", "starting", "running")
    ]
    if len(running) == 1:
        return running[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ManagerError(f"unknown instance: {token}")
    raise ManagerError(
        f"ambiguous instance {token!r}: {', '.join(sorted(matches))}"
    )


def parse_processes(info: dict) -> list[dict]:
    processes = info.get("processes", [])
    if isinstance(processes, str):
        try:
            processes = json.loads(processes)
        except json.JSONDecodeError:
            processes = []
    return processes


def print_logs(instances: list[str], stream=None) -> None:
    stream = stream or sys.stdout
    for instance in instances:
        for name, path in logs.files(instance):
            text = logs.read(path)
            if not text:
                continue
            header = f"--- {instance}: {name} ---"
            print(header, file=sys.stderr if stream is sys.stdout else stream)
            stream.write(text if text.endswith("\n") else text + "\n")
            stream.flush()


def resolve_log_instance(client: ManagerClient | None, token: str) -> str:
    """Resolve an instance name against running instances and logs on disk."""

    live: list[str] = []
    if client is not None:
        try:
            live = [row[0] for row in client.list_instances()]
        except ManagerError:
            live = []
    names = sorted(set(live) | set(logs.instances()))
    if token in names:
        return token
    matches = [name for name in names if name.startswith(token)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ManagerError(f"unknown instance: {token}")
    raise ManagerError(f"ambiguous instance {token!r}: {', '.join(matches)}")


def follow_logs(client: ManagerClient | None, instance: str | None) -> int:
    """Follow logs until the instance is gone and nothing new is written."""

    offsets: dict[Path, int] = {}
    monitors: dict[str, Gio.FileMonitor] = {}
    inodes: dict[str, int] = {}
    shown: dict[str, str | None] = {"file": None}
    state: dict = {
        "interrupt": None,
        "interrupted": False,
        "broken": False,
        "run": True,
    }
    loop = GLib.MainLoop()
    ended = ("exited", "failed", "stopped")

    def drain(name: str) -> None:
        for filename, path in logs.files(name):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = offsets.get(path, 0)
            if size < offset:
                offset = 0
            if size <= offset:
                continue
            try:
                with open(path, "rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
            except OSError:
                continue
            offsets[path] = offset + len(data)
            key = f"{name}:{filename}"
            try:
                if shown["file"] != key:
                    shown["file"] = key
                    print(f"--- {name}: {filename} ---", file=sys.stderr)
                    sys.stderr.flush()
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except OSError:
                # The reader went away (``| head``); stop instead of
                # printing a traceback from a GLib callback.
                state["broken"] = True
                loop.quit()
                return

    def live_instances() -> list[str]:
        if client is None:
            return []
        try:
            return [row[0] for row in client.list_instances()]
        except ManagerError:
            return []

    def drop(name: str) -> None:
        monitor = monitors.pop(name, None)
        inodes.pop(name, None)
        if monitor is not None:
            monitor.cancel()

    def watch(name: str) -> None:
        directory = logs.directory(name)
        try:
            inode = directory.stat().st_ino
        except OSError:
            # Released without logs, or between removal and recreation.
            drop(name)
            return
        if monitors.get(name) is not None and inodes.get(name) == inode:
            drain(name)
            return
        # A recycled name gets a fresh log directory; the monitor holds the
        # old inode, so start over and re-read the files from the beginning.
        drop(name)
        for path in [path for path in offsets if path.parent == directory]:
            del offsets[path]
        shown["file"] = None
        monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        monitor.connect("changed", lambda *_args, name=name: drain(name))
        monitors[name] = monitor
        inodes[name] = inode
        drain(name)

    def refresh() -> None:
        names = [instance] if instance is not None else logs.instances()
        for name in names:
            watch(name)

    def on_directory(_monitor, _file, _other, _event) -> None:
        refresh()

    def on_instance(_conn, _sender, _path, _iface, _signal, params) -> None:
        changed, signal_state = params.unpack()
        if signal_state not in ended:
            return
        if instance is not None and changed != instance:
            return
        refresh()
        if instance is not None or not live_instances():
            loop.quit()

    def on_daemon_gone(*_args) -> None:
        # No daemon means no more signals and no more output: print what is
        # there and stop instead of waiting forever.
        refresh()
        loop.quit()

    def on_sigint() -> bool:
        state["interrupt"] = None
        state["interrupted"] = True
        loop.quit()
        return GLib.SOURCE_REMOVE

    root = config.logs_root()
    root.mkdir(parents=True, exist_ok=True)
    root_monitor = Gio.File.new_for_path(str(root)).monitor_directory(
        Gio.FileMonitorFlags.NONE, None
    )
    root_monitor.connect("changed", on_directory)

    subscription = None
    name_watcher = None
    if client is not None:
        subscription = client.subscribe("InstanceChanged", on_instance)
        name_watcher = Gio.bus_watch_name_on_connection(
            client.connection,
            BUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            None,
            on_daemon_gone,
        )
    state["interrupt"] = GLib.unix_signal_add(
        GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint
    )
    refresh()
    if instance is not None and instance not in live_instances():
        # Nothing will ever signal again: print what is there and stop.
        drain(instance)
        state["run"] = False
    elif instance is None and not live_instances():
        state["run"] = False
    try:
        if state["run"]:
            loop.run()
    finally:
        if state["interrupt"] is not None:
            GLib.source_remove(state["interrupt"])
        root_monitor.cancel()
        for monitor in monitors.values():
            monitor.cancel()
        if name_watcher is not None:
            Gio.bus_unwatch_name(name_watcher)
        if subscription is not None and client is not None:
            client.unsubscribe(subscription)
    if state["broken"]:
        # Avoid a second BrokenPipeError when the interpreter flushes stdout.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except OSError:
            pass
    return 130 if state["interrupted"] else 0


# -------------------------------------------------------------------- commands


def cmd_daemon(args) -> int:
    if args.stop:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if not name_has_owner(connection):
            print("dcaged is not running")
            return 1
        ManagerClient(connection).shutdown()
        if wait_name(connection, appeared=False, timeout=15):
            print("dcaged stopped")
            return 0
        return fail("dcaged did not stop")
    if args.detach:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if args.replace:
            from .daemon import _ask_existing_shutdown

            _ask_existing_shutdown()
        start_daemon(connection)
        print("dcaged is running")
        return 0
    from .daemon import main as daemon_main

    argv = ["--log-level", args.log_level]
    if args.replace:
        argv.append("--replace")
    return daemon_main(argv)


def cmd_profile_list(args) -> int:
    client = ManagerClient.connect()
    rows = client.list_profiles()
    if not rows:
        print("no profiles")
        return 0
    width = max(len(profile_id) for profile_id, _ in rows)
    for profile_id, description in rows:
        print(f"{profile_id:<{width}}  {description}")
    return 0


def cmd_profile_show(args) -> int:
    client = ManagerClient.connect()
    sys.stdout.write(client.get_profile(args.id))
    return 0


def cmd_profile_path(args) -> int:
    print(config.Store().path(args.id))
    return 0


def cmd_profile_command(args) -> int:
    client = ManagerClient.connect()
    profile = Profile.from_toml(args.id, client.get_profile(args.id))
    argv = sandbox.preview_argv(profile)
    if args.json:
        print(json.dumps(argv))
    elif args.one_line:
        print(shlex.join(argv))
    else:
        print(sandbox.format_argv(argv))
    return 0


def cmd_profile_create(args) -> int:
    client = ManagerClient.connect()
    existing = {profile_id for profile_id, _ in client.list_profiles()}
    if args.id in existing:
        return fail(f"profile already exists: {args.id}")
    command = split_command(args.command)
    if args.from_profile:
        text = client.get_profile(args.from_profile)
        profile = Profile.from_toml(args.id, text)
        profile.id = args.id
    else:
        profile = Profile(id=args.id)
    if args.description:
        profile.description = args.description
    if args.bus:
        profile.bus = args.bus
    if args.home:
        profile.home = args.home
    if args.no_network:
        profile.network = False
    if args.keep is not None:
        profile.keep_alive = args.keep
    if args.nvidia:
        profile.nvidia = True
    if args.a11y:
        profile.a11y = True
    if command:
        profile.command = command
    client.save_profile(args.id, profile.to_toml())
    print(args.id)
    return 0


def cmd_profile_duplicate(args) -> int:
    client = ManagerClient.connect()
    new_id = args.new_id or ""
    created = client.duplicate_profile(args.id, new_id)
    print(created)
    return 0


def cmd_profile_rename(args) -> int:
    client = ManagerClient.connect()
    print(client.rename_profile(args.id, args.new_id))
    return 0


def _edit_text(text: str, suffix: str = ".toml") -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    config.ensure_runtime_dir()
    path = config.runtime_root() / f"edit-{os.getpid()}{suffix}"
    path.write_text(text, encoding="utf-8")
    try:
        try:
            subprocess.run([*shlex.split(editor), str(path)], check=False)
        except OSError as exc:
            raise ManagerError(f"cannot run editor {editor!r}: {exc}") from exc
        return path.read_text(encoding="utf-8")
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def cmd_profile_edit(args) -> int:
    client = ManagerClient.connect()
    text = client.get_profile(args.id)
    edited = _edit_text(text)
    if edited.strip() == text.strip():
        print(f"{args.id}: unchanged")
        return 0
    client.save_profile(args.id, edited)
    print(f"{args.id}: saved")
    return 0


def cmd_profile_delete(args) -> int:
    client = ManagerClient.connect()
    client.delete_profile(args.id)
    print(f"{args.id}: deleted")
    return 0


def cmd_start(args) -> int:
    client = ManagerClient.connect()
    command = split_command(args.command)
    env = parse_env(args.env)
    profile = Profile.from_toml(args.profile, client.get_profile(args.profile))
    problem = check_tmpbind(profile)
    if problem:
        return fail(problem)
    keep = profile.keep_alive if args.keep is None else args.keep
    target = args.instance or ""
    reused = False
    if args.reuse and target:
        reused = any(row[0] == target for row in live_rows(client.list_instances()))
    instance = client.launch(
        args.profile,
        target,
        keep,
        command,
        env,
        args.cwd or "",
        reuse=args.reuse,
    )
    print(instance)
    if reused:
        print(f"  reused instance {instance}", file=sys.stderr)
        return 0
    try:
        info = client.get_instance(instance)
    except ManagerError:
        return 0
    for process in parse_processes(info):
        print(f"  pid {process['pid']}: {' '.join(process['argv'])}", file=sys.stderr)
    return 0


LIVE_STATES = ("new", "starting", "running")


def live_rows(rows: list[tuple]) -> list[tuple]:
    """Instances that are still alive; exited ones are on their way out."""

    return [row for row in rows if row[2] in LIVE_STATES]


def cmd_ps(args) -> int:
    client = ManagerClient.connect()
    rows = live_rows(client.list_instances())
    if not rows:
        print("no instances")
        return 0
    print(f"{'INSTANCE':<34} {'PROFILE':<16} {'STATE':<9} {'KEEP':<5} {'PID':>7}  STARTED")
    for instance_id, profile_id, state, started, keep, pid in rows:
        print(
            f"{instance_id:<34} {profile_id:<16} {state:<9} "
            f"{'yes' if keep else 'no':<5} {pid:>7}  {started}"
        )
    return 0


def cmd_inspect(args) -> int:
    client = ManagerClient.connect()
    instance = resolve_instance(client, args.instance)
    info = client.get_instance(instance)
    info["processes"] = parse_processes(info)
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0


def cmd_exec(args) -> int:
    client = ManagerClient.connect()
    instance = resolve_instance(client, args.instance)
    command = split_command(args.command)
    if not command:
        return fail("exec needs a command after --")
    env = parse_env(args.env)
    pid = client.spawn(instance, command, env, args.cwd or "")
    if args.detach:
        print(pid)
        return 0
    log_path = ""
    for process in client.processes(instance):
        if int(process.get("pid", -1)) == pid:
            log_path = str(process.get("log", ""))
    try:
        status = client.wait_process_exit(instance, pid)
    except KeyboardInterrupt:
        return 130
    except ManagerError as exc:
        return fail(str(exc))
    if not log_path:
        for process in client.processes(instance):
            if int(process.get("pid", -1)) == pid:
                log_path = str(process.get("log", ""))
    if not log_path:
        print(f"dcage: log for pid {pid} is not available", file=sys.stderr)
    else:
        text = logs.read(Path(log_path))
        if text:
            print(f"--- pid {pid} ---", file=sys.stderr)
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            sys.stdout.flush()
    return status


def cmd_stop(args) -> int:
    client = ManagerClient.connect()
    if args.all:
        rows = client.list_instances()
        stopped = 0
        for row in rows:
            if row[2] in ("new", "starting", "running"):
                try:
                    client.stop_instance(row[0])
                except ManagerError:
                    continue
                print(row[0])
                stopped += 1
        if not stopped:
            print("no running instances")
        return 0
    instance = resolve_instance(client, args.instance)
    client.stop_instance(instance)
    print(f"{instance}: stopped")
    return 0


def cmd_keep(args) -> int:
    client = ManagerClient.connect()
    instance = resolve_instance(client, args.instance)
    keep = args.value.lower() in ("on", "yes", "true", "1")
    current = client.set_keep(instance, keep)
    print(f"{instance}: keep_alive={'on' if current else 'off'}")
    return 0


def cmd_logs(args) -> int:
    client: ManagerClient | None
    try:
        client = ManagerClient.connect()
    except ManagerError:
        client = None
    if args.instance:
        instance: str | None = resolve_log_instance(client, args.instance)
    else:
        instance = None
    if args.follow:
        return follow_logs(client, instance)
    if instance is not None:
        if not logs.files(instance):
            return fail(f"no logs for instance {instance}")
        print_logs([instance])
        return 0
    names = logs.instances()
    if not names:
        print("no logs")
        return 0
    print_logs(names)
    return 0


def cmd_completion(args) -> int:
    from . import complete

    sys.stdout.write(complete.script(args.shell))
    return 0


def run_completion(argv: list[str]) -> int:
    """The hidden ``dcage _complete <cword> <words...>`` helper.

    It is dispatched before argparse sees the arguments, because a literal
    ``--`` on the line would otherwise be swallowed by the parser instead of
    reaching the completion engine.  It runs on every Tab press, so it never
    prints a traceback and never starts the daemon just to suggest a word.
    """

    from . import complete

    if not argv:
        return 2
    try:
        cword = int(argv[0])
    except ValueError:
        return 2
    words = list(argv[1:])
    try:
        candidates = complete.candidates(words, cword)
    except Exception:  # noqa: BLE001 - completion must never fail loudly
        return 0
    for candidate in candidates:
        print(candidate)
    return 0


def cmd_doctor(args) -> int:
    import shutil

    print(f"dcage                {__version__}")
    print(f"python               {sys.version.split()[0]}")
    print(f"bwrap                {shutil.which('bwrap') or 'MISSING'}")
    print(f"dbus-daemon          {shutil.which('dbus-daemon') or 'MISSING'}")
    print(f"bus-bridge           {config.find_bus_bridge() or 'MISSING'}")
    overlay = "available" if sandbox.overlay_supported() else "MISSING (tmpbind unusable)"
    print(f"overlayfs            {overlay}")
    session = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not session:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and (Path(runtime) / "bus").exists():
            session = f"unix:path={runtime}/bus"
    print(f"session bus          {session or 'MISSING'}")
    print(f"profiles             {config.profiles_dir()}")
    print(f"runtime              {config.runtime_root()}")
    print(f"logs                 {config.logs_root()}")
    print(f"daemon log           {config.daemon_log_path()}")
    status = 0
    try:
        client = ManagerClient.connect()
        profiles = client.list_profiles()
        instances = client.list_instances()
        print("dcaged               running")
        print(f"  profiles           {len(profiles)}")
        print(f"  instances          {len(instances)}")
    except ManagerError as exc:
        print(f"dcaged               FAILED: {exc}")
        status = 1
    return status


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dcage",
        description="Profile-based bubblewrap sandboxes with long-lived instances.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("daemon", help="run the manager daemon")
    p.add_argument("--detach", action="store_true", help="start it in the background")
    p.add_argument("--stop", action="store_true", help="ask a running daemon to stop")
    p.add_argument("--replace", action="store_true", help="replace a running daemon")
    p.add_argument("--log-level", default=os.environ.get("DESKCAGE_LOG", "INFO"))
    p.set_defaults(func=cmd_daemon)

    profile = sub.add_parser("profile", help="manage sandbox profiles")
    psub = profile.add_subparsers(dest="profile_command", required=True)

    p = psub.add_parser("list", help="list profiles")
    p.set_defaults(func=cmd_profile_list)

    p = psub.add_parser("show", help="print a profile as TOML")
    p.add_argument("id")
    p.set_defaults(func=cmd_profile_show)

    p = psub.add_parser("path", help="print the profile file path")
    p.add_argument("id")
    p.set_defaults(func=cmd_profile_path)

    p = psub.add_parser("command", help="print the bwrap command a profile would run")
    p.add_argument("id")
    p.add_argument("--json", action="store_true", help="print the argv array as JSON")
    p.add_argument(
        "--one-line",
        action="store_true",
        help="print the command on a single line for copy-paste",
    )
    p.set_defaults(func=cmd_profile_command)

    p = psub.add_parser("create", help="create a profile")
    p.add_argument("id")
    p.add_argument("--description")
    p.add_argument("--from", dest="from_profile", metavar="PROFILE")
    p.add_argument("--command", action="append", metavar="CMD")
    p.add_argument("--bus", choices=["none", "isolated", "bridge"])
    p.add_argument("--home", choices=["tmp", "bind", "ro", "tmpbind"])
    p.add_argument("--no-network", action="store_true")
    p.add_argument("--keep", dest="keep", action="store_true", default=None)
    p.add_argument("--no-keep", dest="keep", action="store_false")
    p.add_argument("--nvidia", action="store_true", help="pass through /dev/nvidia*")
    p.add_argument("--a11y", action="store_true", help="bind the at-spi bus")
    p.set_defaults(func=cmd_profile_create)

    p = psub.add_parser("duplicate", help="copy a profile")
    p.add_argument("id")
    p.add_argument("new_id", nargs="?", default="")
    p.set_defaults(func=cmd_profile_duplicate)

    p = psub.add_parser("rename", help="rename a profile")
    p.add_argument("id")
    p.add_argument("new_id")
    p.set_defaults(func=cmd_profile_rename)

    p = psub.add_parser("edit", help="edit a profile in $EDITOR")
    p.add_argument("id")
    p.set_defaults(func=cmd_profile_edit)

    p = psub.add_parser("delete", help="delete a profile")
    p.add_argument("id")
    p.set_defaults(func=cmd_profile_delete)

    p = sub.add_parser("start", help="launch a long-lived instance")
    p.add_argument("profile")
    p.add_argument("--instance")
    p.add_argument("--env", action="append", metavar="KEY[=VALUE]")
    p.add_argument("--cwd", default="")
    p.add_argument("--keep", dest="keep", action="store_true", default=None)
    p.add_argument("--no-keep", dest="keep", action="store_false")
    p.add_argument(
        "--reuse",
        action="store_true",
        help="run the command in an already running instance with this name",
    )
    p.add_argument("command", nargs="*")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("ps", help="list instances")
    p.set_defaults(func=cmd_ps)

    p = sub.add_parser("inspect", help="show instance details as JSON")
    p.add_argument("instance")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("exec", help="start a process inside a running instance")
    p.add_argument("instance")
    p.add_argument("--env", action="append", metavar="KEY[=VALUE]")
    p.add_argument("--cwd", default="")
    p.add_argument("--detach", action="store_true", help="do not wait for exit")
    p.add_argument("command", nargs="*")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("stop", help="stop an instance")
    p.add_argument("instance", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("keep", help="turn keep-alive on or off")
    p.add_argument("instance")
    p.add_argument("value", choices=["on", "off", "yes", "no", "true", "false", "1", "0"])
    p.set_defaults(func=cmd_keep)

    p = sub.add_parser("logs", help="show process output")
    p.add_argument("instance", nargs="?", default="")
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("doctor", help="check the environment")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("completion", help="print a shell completion script")
    p.add_argument("shell", choices=["bash", "zsh"], help="shell to print it for")
    p.set_defaults(func=cmd_completion)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Shell completion runs before argparse: ``--`` must survive verbatim.
    if raw[:1] == ["_complete"]:
        try:
            return run_completion(raw[1:])
        except Exception:  # noqa: BLE001 - never fail a Tab press
            return 0
    parser = build_parser()
    head, tail = raw, None
    if raw and raw[0] in ("start", "exec") and "--" in raw:
        index = raw.index("--")
        head, tail = raw[:index], raw[index + 1 :]
    args = parser.parse_args(head)
    if tail is not None:
        args.command = tail
    try:
        return args.func(args)
    except (ManagerError, ProfileError, config.ConfigError) as exc:
        return fail(str(exc))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
