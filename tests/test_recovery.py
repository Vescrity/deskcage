"""Recovery: a SIGKILLed daemon must not leave sandboxes or buses behind."""

from __future__ import annotations

import os
import signal
import sys
import time

import harness


def main() -> int:
    with harness.TestEnv() as env:
        env.cli("profile", "create", "b", "--bus", "bridge", "--command", "sleep 60")
        instance = env.cli("start", "b").stdout.strip()
        instance_dir = env.runtime_dir / "dcage" / "instances" / instance
        pid_file = instance_dir / "run" / "bus.pid"
        assert pid_file.is_file(), pid_file
        bus_pid = int(pid_file.read_text())
        assert os.path.isdir(f"/proc/{bus_pid}")

        daemon_pid = int(env.daemon_pid())
        os.kill(daemon_pid, signal.SIGKILL)
        harness.wait_until(lambda: not os.path.isdir(f"/proc/{daemon_pid}"), 5)

        # bwrap dies with the daemon; the bus process (a direct child of the
        # daemon) survives a SIGKILL and must be cleaned by the next daemon.
        env.cli("doctor")
        assert harness.wait_until(
            lambda: not os.path.isdir(f"/proc/{bus_pid}"), 10
        ), "leftover bus process was not killed"
        assert harness.wait_until(lambda: not instance_dir.exists(), 10)
    print("recovery: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
