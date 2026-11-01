"""D-Bus client for ``org.deskcage.Manager1`` with daemon autostart."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from gi.repository import Gio, GLib

from . import config
from .daemon import BUS_NAME, INTERFACE, OBJECT_PATH


class ManagerError(RuntimeError):
    pass


def _reply(name: str) -> GLib.VariantType:
    return GLib.VariantType.new(name)


def variant_dict(value: GLib.Variant) -> dict:
    out: dict = {}
    for index in range(value.n_children()):
        entry = value.get_child_value(index)
        key = entry.get_child_value(0).get_string()
        out[key] = entry.get_child_value(1).get_variant().unpack()
    return out


def _daemon_argv() -> list[str]:
    override = os.environ.get("DESKCAGE_DAEMON")
    if override:
        return [override]
    found = shutil.which("dcaged")
    if found:
        return [found]
    sibling = Path(sys.executable).with_name("dcaged")
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    return [sys.executable, "-m", "deskcage.daemon"]


def name_has_owner(connection: Gio.DBusConnection) -> bool:
    try:
        reply = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (BUS_NAME,)),
            _reply("(b)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
    except GLib.Error as exc:
        raise ManagerError(f"cannot reach the session bus: {exc.message}") from exc
    return bool(reply.unpack()[0])


def _log_tail(limit: int = 1200) -> str:
    path = config.daemon_log_path()
    if path == Path(os.devnull):
        return "(daemon log is discarded; set DESKCAGE_DAEMON_LOG to a path to keep it)"
    try:
        data = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "(no daemon log)"
    return data[-limit:] if data else "(empty daemon log)"


def wait_name(
    connection: Gio.DBusConnection,
    appeared: bool,
    timeout: float = 10.0,
) -> bool:
    """Block until ``BUS_NAME`` appears (or vanishes).  No polling."""

    result: dict[str, Any] = {"value": None, "interrupted": False}
    loop = GLib.MainLoop()

    def finish(value: Any) -> None:
        if result["value"] is None:
            result["value"] = value
            loop.quit()

    def on_appeared(*_args) -> None:
        if appeared:
            finish(True)

    def on_vanished(*_args) -> None:
        if not appeared:
            finish(True)

    watcher = Gio.bus_watch_name_on_connection(
        connection,
        BUS_NAME,
        Gio.BusNameWatcherFlags.NONE,
        on_appeared,
        on_vanished,
    )
    armed: dict[str, int | None] = {"timer": None, "interrupt": None}

    def on_timeout() -> bool:
        armed["timer"] = None
        finish(False)
        return GLib.SOURCE_REMOVE

    def on_sigint() -> bool:
        armed["interrupt"] = None
        result["interrupted"] = True
        finish(False)
        return GLib.SOURCE_REMOVE

    armed["timer"] = GLib.timeout_add(int(timeout * 1000), on_timeout)
    armed["interrupt"] = GLib.unix_signal_add(
        GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint
    )
    try:
        loop.run()
    finally:
        for key in ("timer", "interrupt"):
            source = armed[key]
            if source is not None:
                GLib.source_remove(source)
        Gio.bus_unwatch_name(watcher)
    if result["interrupted"]:
        raise KeyboardInterrupt
    return result["value"] is True


def start_daemon(connection: Gio.DBusConnection, timeout: float = 10.0) -> None:
    if name_has_owner(connection):
        return
    log_path = config.daemon_log_path()
    if log_path != Path(os.devnull):
        log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    try:
        subprocess.Popen(
            [*_daemon_argv(), "--log-level", os.environ.get("DESKCAGE_LOG", "INFO")],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            cwd=str(Path.home()),
        )
    except OSError as exc:
        raise ManagerError(f"cannot start dcaged: {exc}") from exc
    finally:
        log.close()

    if not wait_name(connection, True, timeout):
        where = (
            "daemon logging is disabled"
            if log_path == Path(os.devnull)
            else f"see {log_path}"
        )
        raise ManagerError(f"dcaged did not start ({where}):\n{_log_tail()}")


class ManagerClient:
    def __init__(self, connection: Gio.DBusConnection):
        self.connection = connection

    @classmethod
    def connect(cls, autostart: bool = True) -> "ManagerClient":
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            raise ManagerError(
                "no session bus; dcage needs DBUS_SESSION_BUS_ADDRESS or XDG_RUNTIME_DIR"
            ) from exc
        if autostart and not name_has_owner(connection):
            start_daemon(connection)
        return cls(connection)

    # ------------------------------------------------------------------- core

    def call(
        self,
        method: str,
        params: GLib.Variant | None,
        reply: str | None,
        timeout: int = 60000,
    ) -> GLib.Variant | None:
        try:
            return self.connection.call_sync(
                BUS_NAME,
                OBJECT_PATH,
                INTERFACE,
                method,
                params,
                _reply(reply) if reply else None,
                Gio.DBusCallFlags.NONE,
                timeout,
                None,
            )
        except GLib.Error as exc:
            message = exc.message
            marker = f"{INTERFACE}.Error:"
            if marker in message:
                message = message.split(marker, 1)[1].strip()
            raise ManagerError(message) from exc

    def ping(self) -> None:
        self.call("Ping", None, None, timeout=5000)

    # ---------------------------------------------------------------- signals

    def subscribe(self, name: str, callback: Callable) -> int:
        return self.connection.signal_subscribe(
            BUS_NAME,
            INTERFACE,
            name,
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            callback,
        )

    def unsubscribe(self, subscription: int) -> None:
        self.connection.signal_unsubscribe(subscription)

    def wait_signal(
        self,
        names: str | list[str],
        handler: Callable[[str, GLib.Variant], Any],
        timeout: float | None = None,
        description: str = "",
        initial: Callable[[], Any] | None = None,
    ) -> Any:
        """Run a local main loop until ``handler`` sees the wanted signal.

        ``names`` is one signal name or a list of them; ``handler`` gets the
        signal name and its parameters and returns the value to hand back, or
        ``None`` to keep waiting.  ``initial`` is checked once after the
        subscriptions are in place, so a signal that fired just before we
        subscribed is not missed.  Ctrl-C interrupts the wait instead of the
        whole process.
        """

        wanted = [names] if isinstance(names, str) else list(names)
        label = description or wanted[0]
        result: dict[str, Any] = {"value": None, "done": False, "interrupted": False}
        armed: dict[str, int | None] = {"timer": None, "interrupt": None}
        loop = GLib.MainLoop()

        def on_signal(_conn, _sender, _path, _iface, name, params):
            value = handler(name, params)
            if value is not None:
                result["value"] = value
                result["done"] = True
                loop.quit()

        def on_timeout() -> bool:
            armed["timer"] = None
            loop.quit()
            return GLib.SOURCE_REMOVE

        def on_sigint() -> bool:
            armed["interrupt"] = None
            result["interrupted"] = True
            loop.quit()
            return GLib.SOURCE_REMOVE

        subscriptions = [self.subscribe(name, on_signal) for name in wanted]
        try:
            if initial is not None:
                early = initial()
                if early is not None:
                    result["value"] = early
                    result["done"] = True
            if timeout:
                armed["timer"] = GLib.timeout_add(int(timeout * 1000), on_timeout)
            armed["interrupt"] = GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint
            )
            if not result["done"]:
                loop.run()
        finally:
            for key in ("timer", "interrupt"):
                source = armed[key]
                if source is not None:
                    GLib.source_remove(source)
            for subscription in subscriptions:
                self.unsubscribe(subscription)
        if result["done"]:
            return result["value"]
        if result["interrupted"]:
            raise KeyboardInterrupt
        raise ManagerError(f"timed out waiting for {label}")

    def wait_process_exit(
        self, instance: str, pid: int, timeout: float | None = None
    ) -> int:
        """Block until ``pid`` exits and return its status.

        The wait also ends when the instance is stopped, reaped or the daemon
        goes away, so a lost process can never hang the caller.
        """

        ended = {"exited", "failed", "stopped"}
        lost = object()
        timed_out = object()
        interrupted = object()
        result: dict[str, Any] = {"value": None}
        armed: dict[str, int | None] = {"timer": None, "grace": None, "interrupt": None}
        loop = GLib.MainLoop()

        def finish(value: Any) -> None:
            if result["value"] is None:
                result["value"] = value
                loop.quit()

        def status_now() -> int | None:
            found, status = self.process_status(instance, pid)
            return status if found else None

        def on_signal(_conn, _sender, _path, _iface, name, params):
            if name == "ProcessExited":
                signal_instance, signal_pid, status = params.unpack()
                if signal_instance == instance and int(signal_pid) == int(pid):
                    finish(int(status))
                return
            signal_instance, state = params.unpack()
            if signal_instance != instance or state not in ended:
                return
            status = status_now()
            if status is not None:
                finish(status)
            elif armed["grace"] is None:
                # The matching ProcessExited is emitted in the same batch;
                # give it a moment before calling the pid lost.
                armed["grace"] = GLib.timeout_add(200, on_grace)

        def on_grace() -> bool:
            armed["grace"] = None
            status = status_now()
            finish(status if status is not None else lost)
            return GLib.SOURCE_REMOVE

        def on_timeout() -> bool:
            armed["timer"] = None
            finish(timed_out)
            return GLib.SOURCE_REMOVE

        def on_sigint() -> bool:
            armed["interrupt"] = None
            finish(interrupted)
            return GLib.SOURCE_REMOVE

        def on_daemon_gone(*_args) -> None:
            finish(lost)

        subscriptions = [
            self.subscribe("ProcessExited", on_signal),
            self.subscribe("InstanceChanged", on_signal),
        ]
        watcher = Gio.bus_watch_name_on_connection(
            self.connection,
            BUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            None,
            on_daemon_gone,
        )
        try:
            found, status = self.process_status(instance, pid)
            if not found:
                raise ManagerError(f"instance {instance} is gone")
            if status is not None:
                return status
            if timeout:
                armed["timer"] = GLib.timeout_add(int(timeout * 1000), on_timeout)
            armed["interrupt"] = GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint
            )
            loop.run()
        finally:
            for key in ("timer", "grace", "interrupt"):
                source = armed[key]
                if source is not None:
                    GLib.source_remove(source)
            for subscription in subscriptions:
                self.unsubscribe(subscription)
            Gio.bus_unwatch_name(watcher)
        if result["value"] is interrupted:
            raise KeyboardInterrupt
        if result["value"] is timed_out:
            raise ManagerError(f"timed out waiting for pid {pid} to exit")
        if result["value"] is lost:
            raise ManagerError(f"instance {instance} went away before pid {pid} exited")
        return int(result["value"])

    def processes(self, instance: str) -> list[dict]:
        try:
            info = self.get_instance(instance)
        except ManagerError:
            return []
        processes = info.get("processes", [])
        if isinstance(processes, str):
            try:
                processes = json.loads(processes)
            except json.JSONDecodeError:
                return []
        return processes or []

    def process_status(self, instance: str, pid: int) -> tuple[bool, int | None]:
        """``(known, status)``; ``known`` is False when the pid is not listed.

        ``status`` is None for a process that is still running.  The two
        cases must stay apart: an instance that is already gone looks like a
        running process if both collapse into None.
        """

        for process in self.processes(instance):
            if int(process.get("pid", -1)) == int(pid):
                if process.get("status") is None:
                    return True, None
                return True, int(process["status"])
        return False, None

    def wait_instance_end(
        self, instance: str, timeout: float | None = None
    ) -> str:
        def handler(_name: str, params: GLib.Variant):
            signal_instance, state = params.unpack()
            if signal_instance == instance and state in (
                "exited",
                "failed",
                "stopped",
            ):
                return str(state)
            return None

        return self.wait_signal(
            "InstanceChanged", handler, timeout, f"instance {instance} to end"
        )

    # ---------------------------------------------------------------- profiles

    def list_profiles(self) -> list[tuple[str, str]]:
        reply = self.call("ListProfiles", None, "(a(ss))")
        assert reply is not None
        return [tuple(item) for item in reply.unpack()[0]]

    def get_profile(self, profile_id: str) -> str:
        reply = self.call("GetProfile", GLib.Variant("(s)", (profile_id,)), "(s)")
        assert reply is not None
        return reply.unpack()[0]

    def save_profile(self, profile_id: str, text: str) -> str:
        reply = self.call(
            "SaveProfile",
            GLib.Variant("(ss)", (profile_id, text)),
            "(s)",
        )
        assert reply is not None
        return reply.unpack()[0]

    def delete_profile(self, profile_id: str) -> bool:
        reply = self.call("DeleteProfile", GLib.Variant("(s)", (profile_id,)), "(b)")
        assert reply is not None
        return bool(reply.unpack()[0])

    def duplicate_profile(self, source: str, new_id: str) -> str:
        reply = self.call(
            "DuplicateProfile", GLib.Variant("(ss)", (source, new_id)), "(s)"
        )
        assert reply is not None
        return reply.unpack()[0]

    def rename_profile(self, source: str, new_id: str) -> str:
        reply = self.call(
            "RenameProfile", GLib.Variant("(ss)", (source, new_id)), "(s)"
        )
        assert reply is not None
        return reply.unpack()[0]

    # --------------------------------------------------------------- instances

    def launch(
        self,
        profile: str,
        instance: str = "",
        keep: bool = True,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str = "",
        reuse: bool = False,
    ) -> str:
        reply = self.call(
            "Launch",
            GLib.Variant(
                "(ssbasa{ss}sb)",
                (
                    profile,
                    instance,
                    keep,
                    list(argv or []),
                    dict(env or {}),
                    cwd,
                    reuse,
                ),
            ),
            "(s)",
        )
        assert reply is not None
        return reply.unpack()[0]

    def list_instances(self, timeout: int = 60000) -> list[tuple]:
        reply = self.call("ListInstances", None, "(a(ssssbi))", timeout=timeout)
        assert reply is not None
        return [tuple(item) for item in reply.unpack()[0]]

    def get_instance(self, instance: str) -> dict:
        reply = self.call(
            "GetInstance", GLib.Variant("(s)", (instance,)), "(a{sv})"
        )
        assert reply is not None
        return variant_dict(reply.get_child_value(0))

    def spawn(
        self,
        instance: str,
        argv: list[str],
        env: dict[str, str] | None = None,
        cwd: str = "",
    ) -> int:
        reply = self.call(
            "Spawn",
            GLib.Variant("(sasa{ss}s)", (instance, argv, dict(env or {}), cwd)),
            "(i)",
        )
        assert reply is not None
        return int(reply.unpack()[0])

    def stop_instance(self, instance: str) -> bool:
        reply = self.call(
            "StopInstance", GLib.Variant("(s)", (instance,)), "(b)"
        )
        assert reply is not None
        return bool(reply.unpack()[0])

    def set_keep(self, instance: str, keep: bool) -> bool:
        reply = self.call(
            "SetKeepAlive", GLib.Variant("(sb)", (instance, keep)), "(b)"
        )
        assert reply is not None
        return bool(reply.unpack()[0])

    def shutdown(self) -> None:
        self.call("Shutdown", None, None, timeout=5000)
