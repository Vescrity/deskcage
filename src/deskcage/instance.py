"""One sandbox instance: bwrap process, agent link and child processes."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, sandbox
from .bus import BusInfo, BusProcess
from .profile import Profile


class InstanceError(RuntimeError):
    pass


def new_id(profile_id: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{profile_id}-{stamp}-{secrets.token_hex(2)}"


@dataclass
class ProcessInfo:
    pid: int
    argv: list[str]
    started: float
    log: str
    status: int | None = None

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "argv": list(self.argv),
            "started": self.started,
            "log": self.log,
            "status": self.status,
        }

    @property
    def command(self) -> str:
        return " ".join(self.argv)


class AgentLink:
    """Line-delimited JSON protocol with the in-sandbox agent."""

    def __init__(
        self,
        path: Path,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.path = Path(path)
        self.on_event = on_event
        self.sock: socket.socket | None = None
        self._buffer = b""
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._connected = False
        self._reader: threading.Thread | None = None

    # ---------------------------------------------------------------- connect

    def connect(self, proc: subprocess.Popen, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise InstanceError(
                    f"sandbox exited during startup (code {proc.returncode})"
                )
            if self.path.exists():
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.connect(str(self.path))
                except OSError as exc:
                    last_error = exc
                    sock.close()
                else:
                    sock.settimeout(None)
                    self.sock = sock
                    self._connected = True
                    self._reader = threading.Thread(
                        target=self._read_loop, daemon=True
                    )
                    self._reader.start()
                    return
            time.sleep(0.05)
        detail = f": {last_error}" if last_error else ""
        raise InstanceError(f"agent did not answer on {self.path}{detail}")

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------------------------------------------------------------- request

    def request(self, payload: dict, timeout: float = 20.0) -> dict:
        if not self._connected or self.sock is None:
            raise InstanceError("agent is not connected")
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            waiter = {"event": threading.Event(), "reply": None}
            self._pending[request_id] = waiter
        message = dict(payload)
        message["id"] = request_id
        data = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        try:
            self.sock.sendall(data)
        except OSError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise InstanceError(f"cannot talk to agent: {exc}") from exc
        if not waiter["event"].wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise InstanceError(f"agent timed out on {payload.get('cmd')}")
        reply = waiter["reply"]
        if reply is None:
            raise InstanceError("agent connection closed")
        if not reply.get("ok", False):
            raise InstanceError(reply.get("error", "agent error"))
        return reply

    # ----------------------------------------------------------------- reader

    def _read_loop(self) -> None:
        assert self.sock is not None
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._buffer += chunk
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if message.get("event"):
                        if self.on_event is not None:
                            try:
                                self.on_event(message)
                            except Exception:  # noqa: BLE001
                                traceback.print_exc()
                        continue
                    with self._lock:
                        waiter = self._pending.get(message.get("id"), None)
                    if waiter is not None:
                        waiter["reply"] = message
                        waiter["event"].set()
        except OSError:
            pass
        finally:
            self._connected = False
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for waiter in pending:
                waiter["reply"] = None
                waiter["event"].set()

    def close(self) -> None:
        self._connected = False
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


@dataclass
class ExitRecord:
    at: float
    code: int | None
    reason: str = ""


class Instance:
    """A running (or exited) sandbox instance owned by the daemon."""

    def __init__(
        self,
        instance_id: str,
        profile: Profile,
        host_bus_address: str | None,
        keep: bool | None = None,
        on_change: Callable[["Instance"], None] | None = None,
        caller_env: Mapping[str, str] | None = None,
    ):
        self.id = instance_id
        self.profile = profile
        self.keep = profile.keep_alive if keep is None else keep
        self.on_change = on_change
        self.dir = config.runtime_root() / "instances" / instance_id
        self.run_dir = self.dir / "run"
        self.log_dir = config.logs_root() / instance_id
        self.layout = sandbox.Layout.create(
            instance_id,
            self.dir,
            Path(__file__).resolve().with_name("agent.py"),
            log_dir=self.log_dir,
            env=caller_env,
        )
        self.host_bus_address = host_bus_address
        self.bus: BusInfo | None = None
        self.proc: subprocess.Popen | None = None
        self.link: AgentLink | None = None
        self.state = "new"
        self.started_at = time.time()
        self.exit: ExitRecord | None = None
        self.error = ""
        self.processes: dict[int, ProcessInfo] = {}
        self._bus_process: BusProcess | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._released = threading.Event()
        # What the daemon has already announced.  Keeping this per instance
        # means a name that is reused starts with a clean slate and a late
        # notification can never be repeated or leak into the next instance.
        self._announced_states: set[str] = set()
        self._announced_status: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------ start

    def start(self) -> "Instance":
        missing = sandbox.tmpbind_missing(self.profile, self.layout.env)
        if missing and self.profile.tmpbind_strict:
            raise InstanceError(
                "tmpbind source(s) do not exist and files.tmpbind_strict is on: "
                + ", ".join(missing)
            )
        if sandbox.uses_tmpbind(self.profile) and not sandbox.overlay_supported():
            raise InstanceError(
                "tmpbind needs overlayfs, but the kernel does not provide it to "
                "user namespaces on this host (try: modprobe overlay)"
            )

        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        # A recreated instance starts with a clean log directory, so two runs
        # under the same name never mix.
        shutil.rmtree(self.log_dir, ignore_errors=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.log_dir, 0o700)
        # Mount point for layouts that do not bind a dedicated log directory;
        # real instances bind the global log dir over it.
        (self.run_dir / "logs").mkdir(exist_ok=True)

        self._bus_process = BusProcess(
            mode=self.profile.bus,
            run_dir=self.run_dir,
            host_bus_address=self.host_bus_address,
            pid_hint=self.profile.bus_pid_hint,
            log_path=self.log_dir / "bus.log",
        )
        try:
            self.bus = self._bus_process.start()
            argv = sandbox.build(self.profile, self.layout, self.bus.address, self.keep)
            log = open(self.log_dir / "sandbox.log", "ab")
            try:
                for path in missing:
                    log.write(
                        f"dcage: skipping missing tmpbind source: {path}\n".encode()
                    )
                log.flush()
                self.proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    close_fds=True,
                )
            finally:
                log.close()
            link = AgentLink(self.run_dir / "control.sock", self._on_event)
            try:
                link.connect(self.proc, timeout=30.0)
                link.request({"cmd": "ping"}, timeout=5.0)
            except BaseException:
                link.close()
                raise
            self.link = link
        except BaseException as exc:
            self.error = str(exc)
            proc, self.proc = self.proc, None
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            self._teardown()
            raise InstanceError(str(exc)) from exc

        self.state = "running"
        self._notify()
        threading.Thread(target=self._watch, daemon=True).start()
        if self.keep != self.profile.keep_alive:
            self.set_keep(self.keep)
        return self

    # ------------------------------------------------------------------ child

    def spawn(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        cwd: str = "",
    ) -> ProcessInfo:
        if self.state != "running":
            raise InstanceError(f"instance {self.id} is {self.state}")
        assert self.link is not None
        reply = self.link.request(
            {"cmd": "spawn", "argv": list(argv), "env": env or {}, "cwd": cwd or ""}
        )
        info = ProcessInfo(
            pid=int(reply["pid"]),
            argv=list(argv),
            started=time.time(),
            log=self._host_log(reply.get("log", "")),
        )
        with self._lock:
            self.processes[info.pid] = info
        self._notify()
        return info

    def take_events(self) -> list[tuple[str, tuple]]:
        """State changes and exits the daemon has not announced yet.

        The bookkeeping lives on the instance, so a reaped instance can never
        announce the same exit twice and a fresh instance with a recycled name
        starts with an empty history.
        """

        events: list[tuple[str, tuple]] = []
        with self._lock:
            if self.state not in self._announced_states:
                self._announced_states.add(self.state)
                events.append(("state", (self.state,)))
            for pid, info in self.processes.items():
                if info.status is None:
                    continue
                key = (pid, int(info.status))
                if key in self._announced_status:
                    continue
                self._announced_status.add(key)
                events.append(("exit", (pid, key[1])))
        return events

    def process(self, pid: int) -> ProcessInfo | None:
        with self._lock:
            return self.processes.get(pid)

    def _host_log(self, value: str) -> str:
        """The agent reports paths inside the sandbox; map them back."""
        if not value:
            return ""
        prefix = "/run/dcage/logs/"
        if value.startswith(prefix):
            return str(self.log_dir / value[len(prefix):])
        if value.startswith("/run/dcage/"):
            return str(self.run_dir / value[len("/run/dcage/"):])
        if os.path.isabs(value):
            return value
        return str(self.log_dir / value)

    # ------------------------------------------------------------------- keep

    def set_keep(self, keep: bool) -> bool:
        if self.link is not None and self.link.connected:
            reply = self.link.request({"cmd": "set_keep", "keep": bool(keep)})
            self.keep = bool(reply.get("keep", keep))
        else:
            self.keep = bool(keep)
        self._notify()
        return self.keep

    # ------------------------------------------------------------------ watch

    def _on_event(self, event: dict) -> None:
        if event.get("event") != "exit":
            return
        pid = int(event.get("pid", -1))
        status = event.get("status")
        with self._lock:
            info = self.processes.get(pid)
            if info is not None:
                info.status = int(status) if status is not None else None
        self._notify()

    def _watch(self) -> None:
        assert self.proc is not None
        code = self.proc.wait()
        with self._lock:
            self.exit = ExitRecord(at=time.time(), code=code)
            if self.state in ("starting", "running"):
                self.state = "exited" if code == 0 else "failed"
        self._notify()
        self._teardown()
        # Second notification: the daemon reaps the instance once its runtime
        # directory is gone.
        self._notify()

    # ------------------------------------------------------------------- stop

    def stop(self, grace: float = 3.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        link = self.link
        if link is not None and link.connected:
            try:
                link.request({"cmd": "shutdown"}, timeout=2.0)
            except InstanceError:
                pass
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        pass
        if link is not None:
            link.close()
        if self.state in ("new", "starting", "running"):
            self.state = "stopped"
            if self.exit is None:
                self.exit = ExitRecord(at=time.time(), code=None, reason="stopped")
        self._stop_bus()
        self._notify()

    def _teardown(self) -> None:
        link, self.link = self.link, None
        if link is not None:
            link.close()
        self._stop_bus()
        # The name and the runtime directory are free as soon as the sandbox
        # is gone; only the log directory survives.
        shutil.rmtree(self.dir, ignore_errors=True)
        self._released.set()

    @property
    def released(self) -> bool:
        return self._released.is_set()

    def wait_released(self, timeout: float = 10.0) -> bool:
        return self._released.wait(timeout)

    def _stop_bus(self) -> None:
        bus, self._bus_process = self._bus_process, None
        if bus is not None:
            try:
                bus.stop()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ intro

    def snapshot(self) -> dict:
        with self._lock:
            processes = [info.to_dict() for info in self.processes.values()]
            exit_record = None
            if self.exit is not None:
                exit_record = {
                    "at": self.exit.at,
                    "code": self.exit.code,
                    "reason": self.exit.reason,
                }
        return {
            "id": self.id,
            "profile": self.profile.id,
            "state": self.state,
            "keep": self.keep,
            "pid": self.proc.pid if self.proc is not None else 0,
            "dir": str(self.dir),
            "bus": self.bus.address if self.bus else "",
            "bus_mode": self.profile.bus,
            "started": self.started_at,
            "exit": exit_record,
            "error": self.error,
            "processes": processes,
        }

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
