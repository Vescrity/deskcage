"""Session bus lifecycle for a sandbox instance.

Three modes are supported:

``none``
    No session bus at all.  ``DBUS_SESSION_BUS_ADDRESS`` is left unset, so
    anything that needs D-Bus simply fails.

``isolated``
    A private dbus-daemon owned by the instance.  Sandbox processes can talk
    to each other, but nothing crosses to the host.

``bridge``
    A sibling ``bus-bridge serve`` process: a private bus plus the selective
    desktop integration (notifications, tray, MPRIS).

In every mode the socket is placed inside the instance run directory, which
is bind mounted at ``/run/dcage``.  Sandbox processes therefore always see the
same address: ``unix:path=/run/dcage/bus``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

IN_SANDBOX_SOCKET = "/run/dcage/bus"
IN_SANDBOX_ADDRESS = f"unix:path={IN_SANDBOX_SOCKET}"

_DBUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class BusError(RuntimeError):
    pass


@dataclass
class BusInfo:
    mode: str
    address: str | None
    host_address: str | None
    socket: Path | None
    pid: int | None


class BusProcess:
    """Owns the bus process (if any) for one instance."""

    def __init__(
        self,
        mode: str,
        run_dir: Path,
        host_bus_address: str | None = None,
        pid_hint: bool = False,
        log_path: Path | None = None,
    ):
        if mode not in ("none", "isolated", "bridge"):
            raise BusError(f"unknown bus mode: {mode!r}")
        self.mode = mode
        self.run_dir = Path(run_dir)
        self.host_bus_address = host_bus_address
        self.pid_hint = pid_hint
        self.log_path = Path(log_path) if log_path else self.run_dir / "bus.log"
        self.socket_path = self.run_dir / "bus"
        self.pid_path = self.run_dir / "bus.pid"
        self._proc: subprocess.Popen | None = None
        self._log = None

    # ------------------------------------------------------------------ start

    def start(self) -> BusInfo:
        if self.mode == "none":
            return BusInfo("none", None, None, None, None)

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._unlink_socket()
        self._log = open(self.log_path, "ab")
        try:
            if self.mode == "isolated":
                self._start_isolated()
            else:
                self._start_bridge()
            self._wait_socket()
        except BaseException:
            self.stop()
            raise
        if self._proc is not None:
            try:
                self.pid_path.write_text(str(self._proc.pid), encoding="utf-8")
            except OSError:
                pass
        return BusInfo(
            mode=self.mode,
            address=IN_SANDBOX_ADDRESS,
            host_address=f"unix:path={self.socket_path}",
            socket=self.socket_path,
            pid=self._proc.pid if self._proc else None,
        )

    def _start_isolated(self) -> None:
        dbus_daemon = shutil.which("dbus-daemon")
        if not dbus_daemon:
            raise BusError("dbus-daemon is not installed")
        config_path = self.run_dir / "bus.conf"
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_DBUS_CONFIG.format(socket=self.socket_path))
        self._proc = subprocess.Popen(
            [dbus_daemon, "--config-file", str(config_path), "--nofork"],
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=self._log,
            close_fds=True,
        )

    def _start_bridge(self) -> None:
        launcher = config.find_bus_bridge()
        if not launcher:
            raise BusError(
                "bus-bridge not found; install it, set DESKCAGE_BUS_BRIDGE, or add "
                "[bus] bridge = \"...\" to ~/.config/dcage/config.toml"
            )
        if not self.host_bus_address:
            raise BusError("bus = \"bridge\" needs a host session bus")
        argv = [
            launcher,
            "serve",
            "--state-dir",
            str(self.run_dir),
            "--host",
            self.host_bus_address,
            "--log-level",
            os.environ.get("DESKCAGE_BUS_LOG", "WARNING"),
        ]
        if self.pid_hint:
            argv.append("--pid-hint")
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=self._log,
            close_fds=True,
        )

    def _wait_socket(self, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self._proc is not None
            if self._proc.poll() is not None:
                tail = self._log_tail()
                raise BusError(
                    f"{self.mode} bus exited during startup "
                    f"(code {self._proc.returncode}): {tail}"
                )
            if self.socket_path.exists():
                # Give the daemon a moment to finish binding before clients
                # race in; the file appears after bind() but listen() is next.
                time.sleep(0.02)
                return
            time.sleep(0.02)
        raise BusError(f"{self.mode} bus did not create {self.socket_path}")

    # ------------------------------------------------------------------- stop

    def stop(self, timeout: float = 5.0) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        self._unlink_socket()
        try:
            self.pid_path.unlink()
        except OSError:
            pass

    def _unlink_socket(self) -> None:
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if self.mode == "isolated":
            try:
                (self.run_dir / "bus.conf").unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ intro

    def _log_tail(self, limit: int = 800) -> str:
        if self._log is not None:
            try:
                self._log.flush()
            except OSError:
                pass
        try:
            data = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no log)"
        data = data.strip()
        return data[-limit:] if data else "(empty log)"
