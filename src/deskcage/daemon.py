"""dcaged: per-user daemon exposing ``org.deskcage.Manager1`` on the session bus."""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import shutil
import signal
import sys
import threading
from pathlib import Path

from gi.repository import Gio, GLib

from . import config
from .instance import Instance, InstanceError, new_id
from .profile import ProfileError

log = logging.getLogger("dcaged")

BUS_NAME = "org.deskcage.Manager1"
OBJECT_PATH = "/org/deskcage/Manager1"
INTERFACE = "org.deskcage.Manager1"
ERROR_NAME = "org.deskcage.Manager1.Error"

XML = f"""<node>
  <interface name="{INTERFACE}">
    <method name="Ping"/>
    <method name="ListProfiles">
      <arg type="a(ss)" name="profiles" direction="out"/>
    </method>
    <method name="GetProfile">
      <arg type="s" name="id" direction="in"/>
      <arg type="s" name="toml" direction="out"/>
    </method>
    <method name="SaveProfile">
      <arg type="s" name="id" direction="in"/>
      <arg type="s" name="toml" direction="in"/>
      <arg type="s" name="saved" direction="out"/>
    </method>
    <method name="DeleteProfile">
      <arg type="s" name="id" direction="in"/>
      <arg type="b" name="deleted" direction="out"/>
    </method>
    <method name="DuplicateProfile">
      <arg type="s" name="source" direction="in"/>
      <arg type="s" name="id" direction="in"/>
      <arg type="s" name="created" direction="out"/>
    </method>
    <method name="RenameProfile">
      <arg type="s" name="source" direction="in"/>
      <arg type="s" name="id" direction="in"/>
      <arg type="s" name="renamed" direction="out"/>
    </method>
    <method name="Launch">
      <arg type="s" name="profile" direction="in"/>
      <arg type="s" name="id" direction="in"/>
      <arg type="b" name="keep" direction="in"/>
      <arg type="as" name="argv" direction="in"/>
      <arg type="a{{ss}}" name="env" direction="in"/>
      <arg type="s" name="cwd" direction="in"/>
      <arg type="b" name="reuse" direction="in"/>
      <arg type="s" name="instance" direction="out"/>
    </method>
    <method name="ListInstances">
      <arg type="a(ssssbi)" name="instances" direction="out"/>
    </method>
    <method name="GetInstance">
      <arg type="s" name="id" direction="in"/>
      <arg type="a{{sv}}" name="instance" direction="out"/>
    </method>
    <method name="Spawn">
      <arg type="s" name="id" direction="in"/>
      <arg type="as" name="argv" direction="in"/>
      <arg type="a{{ss}}" name="env" direction="in"/>
      <arg type="s" name="cwd" direction="in"/>
      <arg type="i" name="pid" direction="out"/>
    </method>
    <method name="StopInstance">
      <arg type="s" name="id" direction="in"/>
      <arg type="b" name="stopped" direction="out"/>
    </method>
    <method name="SetKeepAlive">
      <arg type="s" name="id" direction="in"/>
      <arg type="b" name="keep" direction="in"/>
      <arg type="b" name="current" direction="out"/>
    </method>
    <method name="Shutdown"/>
    <signal name="InstanceChanged">
      <arg type="s" name="id"/>
      <arg type="s" name="state"/>
    </signal>
    <signal name="ProcessExited">
      <arg type="s" name="instance"/>
      <arg type="i" name="pid"/>
      <arg type="i" name="status"/>
    </signal>
  </interface>
</node>
"""


def _iso(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp).astimezone().isoformat(
        timespec="seconds"
    )


class Manager:
    def __init__(self, store: config.Store):
        self.store = store
        self.instances: dict[str, Instance] = {}
        self.connection: Gio.DBusConnection | None = None
        self.loop: GLib.MainLoop | None = None
        self._lock = threading.Lock()
        self.shutting_down = False

    # ------------------------------------------------------------- profiles

    def list_profiles(self) -> list[tuple[str, str]]:
        result = []
        for profile_id in self.store.list_ids():
            try:
                profile = self.store.load(profile_id)
            except ProfileError as exc:
                log.warning("skipping profile %s: %s", profile_id, exc)
                continue
            result.append((profile.id, profile.description))
        return result

    def get_profile(self, profile_id: str) -> str:
        return self.store.load(profile_id).to_toml()

    def save_profile(self, profile_id: str, text: str) -> str:
        from .profile import Profile

        profile = Profile.from_toml(profile_id, text)
        profile.id = profile_id
        self.store.save(profile)
        return profile_id

    def delete_profile(self, profile_id: str) -> bool:
        if not self.store.exists(profile_id):
            raise ProfileError(f"unknown profile: {profile_id}")
        self.store.delete(profile_id)
        return True

    def duplicate_profile(self, source: str, new: str) -> str:
        profile = self.store.load(source)
        profile.id = self.store.new_id(new or source) if not new else self.store.validate_id(new)
        if self.store.exists(profile.id):
            raise ProfileError(f"profile already exists: {profile.id}")
        self.store.save(profile)
        return profile.id

    def rename_profile(self, source: str, new: str) -> str:
        renamed = self.store.rename(source, new)
        # Running instances keep the Profile object they were started with;
        # update it so ``ps`` and ``--reuse`` speak about the profile by its
        # current name instead of the name it had at launch time.
        with self._lock:
            instances = list(self.instances.values())
        for instance in instances:
            if instance.profile.id == source:
                instance.profile.id = renamed
        return renamed

    # ------------------------------------------------------------ instances

    def _host_bus(self) -> str:
        return config.host_bus_address()

    def launch(
        self,
        profile_id: str,
        instance_id: str,
        keep: bool,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        reuse: bool = False,
    ) -> str:
        if self.shutting_down:
            raise InstanceError("daemon is shutting down")
        profile = self.store.load(profile_id)
        instance_id = instance_id or new_id(profile_id)
        try:
            instance_id = config.Store.validate_id(instance_id)
        except config.ConfigError as exc:
            raise InstanceError(f"invalid instance id: {instance_id!r}") from exc
        command = list(argv) or list(profile.command)
        with self._lock:
            existing = self.instances.get(instance_id)
        if existing is not None:
            if not reuse:
                raise InstanceError(f"instance already exists: {instance_id}")
            if existing.profile.id != profile_id:
                raise InstanceError(
                    f"instance {instance_id} belongs to profile "
                    f"{existing.profile.id!r}, not {profile_id!r}"
                )
            if existing.state != "running":
                raise InstanceError(
                    f"instance {instance_id} is {existing.state}, cannot reuse it"
                )
            if command:
                existing.spawn(command, env, cwd)
            return instance_id
        if not command and not keep:
            raise InstanceError(
                "no command to run; pass one after -- or enable keep_alive"
            )
        instance = Instance(
            instance_id,
            profile,
            self._host_bus(),
            keep=keep,
            on_change=self._on_instance_change,
        )
        with self._lock:
            self.instances[instance_id] = instance
        try:
            instance.start()
            if command:
                instance.spawn(command, env, cwd)
        except BaseException:
            with self._lock:
                self.instances.pop(instance_id, None)
            instance.stop()
            raise
        return instance_id

    def instance(self, instance_id: str) -> Instance:
        with self._lock:
            instance = self.instances.get(instance_id)
        if instance is None:
            raise InstanceError(f"unknown instance: {instance_id}")
        return instance

    def stop_instance(self, instance_id: str) -> bool:
        instance = self.instance(instance_id)
        instance.stop()
        # Only free the name once the runtime directory is really gone: a
        # second instance with the same name would otherwise lose its run
        # directory when the old one finally tears down.
        if not instance.wait_released():
            raise InstanceError(f"instance {instance_id} did not stop in time")
        self._reap(instance)
        return True

    def _reap(self, instance: Instance) -> None:
        """Forget an instance: the id is free and only its logs remain."""

        with self._lock:
            if self.instances.get(instance.id) is instance:
                del self.instances[instance.id]

    def set_keep(self, instance_id: str, keep: bool) -> bool:
        return self.instance(instance_id).set_keep(keep)

    def list_instances(self) -> list[tuple]:
        with self._lock:
            instances = list(self.instances.values())
        return [
            (
                item.id,
                item.profile.id,
                item.state,
                _iso(item.started_at),
                item.keep,
                item.proc.pid if item.proc is not None else 0,
            )
            for item in instances
        ]

    def get_instance(self, instance_id: str) -> dict:
        return self.instance(instance_id).snapshot()

    # -------------------------------------------------------------- signals

    def _emit(self, name: str, params: GLib.Variant | None) -> None:
        if self.connection is None:
            return
        try:
            self.connection.emit_signal(None, OBJECT_PATH, INTERFACE, name, params)
        except Exception:  # noqa: BLE001
            log.debug("cannot emit %s", name, exc_info=True)

    def _on_instance_change(self, instance: Instance) -> None:
        GLib.idle_add(self._instance_changed_idle, instance)

    def _instance_changed_idle(self, instance: Instance) -> bool:
        # A stopped instance can already be reaped, and its name can already
        # belong to a fresh instance; never announce stale events for that.
        current = self.instances.get(instance.id)
        if current is not None and current is not instance:
            return False
        for kind, payload in instance.take_events():
            if kind == "state":
                self._emit(
                    "InstanceChanged",
                    GLib.Variant("(ss)", (instance.id, payload[0])),
                )
            else:
                pid, status = payload
                self._emit(
                    "ProcessExited",
                    GLib.Variant("(sii)", (instance.id, pid, status)),
                )
        if instance.released:
            self._reap(instance)
        return False

    # ------------------------------------------------------------- dispatch

    def dispatch(self, method: str, params: GLib.Variant) -> GLib.Variant | None:
        if method == "Ping":
            return None
        if method == "ListProfiles":
            return GLib.Variant("(a(ss))", (self.list_profiles(),))
        if method == "GetProfile":
            profile_id = params.get_child_value(0).get_string()
            return GLib.Variant("(s)", (self.get_profile(profile_id),))
        if method == "SaveProfile":
            profile_id = params.get_child_value(0).get_string()
            text = params.get_child_value(1).get_string()
            return GLib.Variant("(s)", (self.save_profile(profile_id, text),))
        if method == "DeleteProfile":
            profile_id = params.get_child_value(0).get_string()
            return GLib.Variant("(b)", (self.delete_profile(profile_id),))
        if method == "DuplicateProfile":
            source = params.get_child_value(0).get_string()
            new = params.get_child_value(1).get_string()
            return GLib.Variant("(s)", (self.duplicate_profile(source, new),))
        if method == "RenameProfile":
            source = params.get_child_value(0).get_string()
            new = params.get_child_value(1).get_string()
            return GLib.Variant("(s)", (self.rename_profile(source, new),))
        if method == "Launch":
            profile_id = params.get_child_value(0).get_string()
            instance_id = params.get_child_value(1).get_string()
            keep = params.get_child_value(2).get_boolean()
            argv = list(params.get_child_value(3).unpack())
            env = dict(params.get_child_value(4).unpack())
            cwd = params.get_child_value(5).get_string()
            reuse = params.get_child_value(6).get_boolean()
            return GLib.Variant(
                "(s)",
                (self.launch(profile_id, instance_id, keep, argv, env, cwd, reuse),),
            )
        if method == "ListInstances":
            return GLib.Variant("(a(ssssbi))", (self.list_instances(),))
        if method == "GetInstance":
            instance_id = params.get_child_value(0).get_string()
            return GLib.Variant("(a{sv})", (dict_to_variant(self.get_instance(instance_id)),))
        if method == "Spawn":
            instance_id = params.get_child_value(0).get_string()
            argv = list(params.get_child_value(1).unpack())
            env = dict(params.get_child_value(2).unpack())
            cwd = params.get_child_value(3).get_string()
            if not argv:
                raise InstanceError("spawn needs a command")
            info = self.instance(instance_id).spawn(argv, env, cwd)
            return GLib.Variant("(i)", (info.pid,))
        if method == "StopInstance":
            instance_id = params.get_child_value(0).get_string()
            return GLib.Variant("(b)", (self.stop_instance(instance_id),))
        if method == "SetKeepAlive":
            instance_id = params.get_child_value(0).get_string()
            keep = params.get_child_value(1).get_boolean()
            return GLib.Variant("(b)", (self.set_keep(instance_id, keep),))
        if method == "Shutdown":
            self.request_shutdown()
            return None
        raise InstanceError(f"unknown method {method}")

    def request_shutdown(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        threading.Thread(target=self._shutdown, daemon=True).start()

    def _shutdown(self) -> None:
        with self._lock:
            instances = list(self.instances.values())
        for instance in instances:
            try:
                instance.stop()
            except Exception:  # noqa: BLE001
                log.warning("cannot stop %s", instance.id, exc_info=True)
        GLib.idle_add(self._quit)

    def _quit(self) -> bool:
        if self.loop is not None:
            self.loop.quit()
        return False


def dict_to_variant(data: dict) -> dict[str, GLib.Variant]:
    out: dict[str, GLib.Variant] = {}
    for key, value in data.items():
        if isinstance(value, bool):
            out[key] = GLib.Variant("b", value)
        elif isinstance(value, int):
            out[key] = GLib.Variant("i", value)
        elif isinstance(value, float):
            out[key] = GLib.Variant("d", value)
        elif isinstance(value, str):
            out[key] = GLib.Variant("s", value)
        elif value is None:
            continue
        else:
            out[key] = GLib.Variant("s", json.dumps(value))
    return out


def sweep_runtime() -> None:
    """Remove leftovers from a previous daemon.

    bwrap sandboxes die with their parent, but a bus process can survive a
    SIGKILL of the daemon.  Only kill a pid whose command line still names
    this instance's directory, so a recycled pid is never touched.
    """
    root = config.runtime_root() / "instances"
    if not root.is_dir():
        return
    for entry in root.iterdir():
        pid_file = entry / "run" / "bus.pid"
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        text = cmdline.replace(b"\0", b" ").decode("utf-8", "replace")
        if str(entry) not in text:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            log.info("killed leftover bus process %s", pid)
        except OSError:
            pass
    shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dcaged", description="dcage manager daemon (org.deskcage.Manager1)"
    )
    parser.add_argument("--log-level", default=os.environ.get("DESKCAGE_LOG", "INFO"))
    parser.add_argument("--log-file", default=None)
    parser.add_argument(
        "--replace", action="store_true", help="take over an already running daemon"
    )
    args = parser.parse_args(argv)

    handlers: list[logging.Handler] = []
    if args.log_file:
        try:
            os.makedirs(os.path.dirname(args.log_file), exist_ok=True)
            handlers.append(logging.FileHandler(args.log_file))
        except OSError:
            handlers.append(logging.StreamHandler(sys.stderr))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )

    config.ensure_runtime_dir()
    store = config.Store()
    manager = Manager(store)

    loop = GLib.MainLoop()
    manager.loop = loop
    state = {"owned": False, "lost": False}

    def on_bus_acquired(connection: Gio.DBusConnection, name: str) -> None:
        log.info("connected to the session bus (want %s)", name)
        manager.connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        interface = node.interfaces[0]

        def on_method_call(
            conn,
            sender,
            object_path,
            interface_name,
            method_name,
            params,
            invocation,
        ):
            try:
                reply = manager.dispatch(method_name, params)
            except Exception as exc:  # noqa: BLE001
                log.info("%s failed: %s", method_name, exc)
                invocation.return_dbus_error(ERROR_NAME, str(exc))
                return
            invocation.return_value(reply)

        connection.register_object(
            OBJECT_PATH, interface, on_method_call, None, None
        )

    def on_name_acquired(connection, name: str) -> None:
        # Only now is it safe to delete leftovers from a previous daemon:
        # nobody else can own the name while we do.
        state["owned"] = True
        try:
            config.daemon_pid_path().write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        try:
            sweep_runtime()
        except Exception:  # noqa: BLE001 - leftovers must never break startup
            log.warning("cannot clean previous runtime", exc_info=True)
        log.info("acquired %s", name)

    def on_name_lost(connection, name: str) -> None:
        if manager.shutting_down:
            return
        state["lost"] = True
        log.error("cannot own %s: another dcaged is already running", name)
        GLib.idle_add(lambda: (loop.quit(), False)[1])

    if args.replace:
        _ask_existing_shutdown()

    owner_id = Gio.bus_own_name(
        Gio.BusType.SESSION,
        BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        on_name_acquired,
        on_name_lost,
    )

    def on_signal(signum) -> bool:
        log.info("signal %s: shutting down", signum)
        manager.request_shutdown()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_signal, signal.SIGTERM)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_signal, signal.SIGINT)

    try:
        loop.run()
    finally:
        Gio.bus_unown_name(owner_id)
        try:
            pid_path = config.daemon_pid_path()
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
        log.info("dcaged stopped")
    return 1 if state["lost"] else 0


def _ask_existing_shutdown() -> None:
    """Best effort: ask a previous daemon to go away before we claim the name."""
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if not _name_has_owner(connection):
            return
        connection.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "Shutdown",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        # The old daemon owns the name until it stopped its instances; wait for
        # that instead of sleeping in a loop.
        from .client import wait_name

        wait_name(connection, appeared=False, timeout=30)
    except GLib.Error:
        return


def _name_has_owner(connection: Gio.DBusConnection) -> bool:
    try:
        reply = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (BUS_NAME,)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


if __name__ == "__main__":
    sys.exit(main())
