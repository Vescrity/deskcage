"""Shared helpers for the deskcage tests: temp XDG dirs and a private session bus."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from deskcage import config  # noqa: E402

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
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


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    return False


class TestEnv:
    def __init__(self, isolated_bridge: bool = False):
        self.dir = Path(tempfile.mkdtemp(prefix="deskcage-test-"))
        self.config_home = self.dir / "config"
        self.state_home = self.dir / "state"
        self.runtime_dir = self.dir / "run"
        self.bus_dir = self.dir / "bus"
        for path in (self.config_home, self.state_home, self.bus_dir):
            path.mkdir(parents=True)
        self.runtime_dir.mkdir(mode=0o700)
        self._bus: subprocess.Popen | None = None
        self._saved: dict[str, str | None] = {}
        self.isolated_bridge = isolated_bridge
        bridge_src = ROOT.parent / "bus-bridge" / "src"
        self.bus_bridge = ""
        if (bridge_src / "bus_bridge").is_dir():
            launcher = self.dir / "bus-bridge"
            launcher.write_text(
                "#!/bin/sh\n"
                f"PYTHONPATH={shlex.quote(str(bridge_src))} "
                f'exec {shlex.quote(sys.executable)} -m bus_bridge "$@"\n'
            )
            launcher.chmod(0o755)
            self.bus_bridge = str(launcher)

    # ------------------------------------------------------------------- bus

    def start_bus(self) -> str:
        socket = self.bus_dir / "bus"
        conf = self.bus_dir / "bus.conf"
        conf.write_text(BUS_CONFIG.format(socket=socket), encoding="utf-8")
        self._bus = subprocess.Popen(
            ["dbus-daemon", "--config-file", str(conf), "--nofork"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_until(lambda: socket.exists()):
            raise RuntimeError("test bus did not start")
        return f"unix:path={socket}"

    def env(self) -> dict[str, str]:
        values = dict(os.environ)
        values.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_RUNTIME_DIR": str(self.runtime_dir),
                "DBUS_SESSION_BUS_ADDRESS": self.address,
                "DESKCAGE_LOG": "DEBUG",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(ROOT / "src"),
            }
        )
        if self.bus_bridge:
            values["DESKCAGE_BUS_BRIDGE"] = self.bus_bridge
        return values

    # -------------------------------------------------------------- lifecycle

    def __enter__(self) -> "TestEnv":
        self.address = self.start_bus()
        for key in (
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
            "DESKCAGE_BUS_BRIDGE",
        ):
            self._saved[key] = os.environ.get(key)
        os.environ.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_RUNTIME_DIR": str(self.runtime_dir),
                "DBUS_SESSION_BUS_ADDRESS": self.address,
            }
        )
        if self.bus_bridge:
            os.environ["DESKCAGE_BUS_BRIDGE"] = self.bus_bridge
        return self

    def __exit__(self, *exc) -> None:
        self.stop_daemon()
        if self._bus is not None:
            self._bus.terminate()
            try:
                self._bus.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._bus.kill()
                self._bus.wait(timeout=5)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def stop_daemon(self) -> None:
        try:
            from deskcage.client import ManagerClient, name_has_owner

            client = ManagerClient.connect(autostart=False)
            client.shutdown()
            wait_until(lambda: not name_has_owner(client.connection), 15)
        except Exception:
            pass

    # --------------------------------------------------------------- helpers

    def store(self) -> config.Store:
        return config.Store(self.config_home / "dcage" / "profiles")

    def cli(self, *args: str, check: bool = True, timeout: int = 120):
        proc = subprocess.run(
            [sys.executable, "-m", "deskcage", *args],
            env=self.env(),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"dcage {' '.join(args)} failed ({proc.returncode})\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return proc

    def daemon_log(self) -> str:
        try:
            return config.daemon_log_path().read_text(errors="replace")
        except OSError:
            return ""

    def daemon_pid(self) -> str:
        path = config.daemon_pid_path()
        assert path.is_file(), f"no daemon pid file at {path}"
        return path.read_text(encoding="utf-8").strip()
