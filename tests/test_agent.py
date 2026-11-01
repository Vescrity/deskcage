"""In-sandbox agent protocol, run directly on the host."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import harness

from deskcage.instance import AgentLink

ROOT = harness.ROOT


def wait_event(events: list[dict], predicate, timeout: float = 10.0) -> dict | None:
    harness.wait_until(lambda: any(predicate(e) for e in events), timeout)
    for event in events:
        if predicate(event):
            return event
    return None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="deskcage-agent-"))
    sock = tmp / "control.sock"
    logdir = tmp / "logs"
    logdir.mkdir()
    proc = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "src" / "deskcage" / "agent.py"),
            "--socket",
            str(sock),
            "--log-dir",
            str(logdir),
            "--keep",
            "1",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    events: list[dict] = []
    link = AgentLink(sock, events.append)
    try:
        link.connect(proc, timeout=5)
        assert link.request({"cmd": "ping"})["ok"] is True

        reply = link.request(
            {
                "cmd": "spawn",
                "argv": ["sh", "-c", "echo hello-agent; exit 7"],
                "env": {"AGENT_TEST": "yes"},
                "cwd": str(tmp),
            }
        )
        pid = int(reply["pid"])
        assert not os.path.isabs(reply["log"]), reply["log"]

        event = wait_event(events, lambda e: e.get("pid") == pid)
        assert event is not None and event["status"] == 7, events
        log = logdir / reply["log"]
        harness.wait_until(lambda: "hello-agent" in log.read_text(), 5)
        assert "hello-agent" in log.read_text()

        # Keep-alive means the agent survives the last child.
        time.sleep(0.3)
        assert proc.poll() is None
        assert link.request({"cmd": "list"})["children"] == []

        # A child may exit before the spawn reply is handled; the reaper must
        # still report it instead of losing the event.
        fast: list[dict] = []
        link.on_event = fast.append
        pid2 = int(
            link.request({"cmd": "spawn", "argv": ["true"], "env": {}, "cwd": str(tmp)})[
                "pid"
            ]
        )
        assert wait_event(fast, lambda e: e.get("pid") == pid2) is not None

        # keep=false lets the agent exit once the last child is gone.
        assert link.request({"cmd": "set_keep", "keep": False})["keep"] is False
        proc.wait(timeout=10)

        # A second agent: explicit shutdown answers before exiting.
        sock2 = tmp / "control2.sock"
        proc2 = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "src" / "deskcage" / "agent.py"),
                "--socket",
                str(sock2),
                "--log-dir",
                str(logdir),
                "--keep",
                "1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        link2 = AgentLink(sock2)
        try:
            link2.connect(proc2, timeout=5)
            assert link2.request({"cmd": "shutdown"})["ok"] is True
            proc2.wait(timeout=10)
        finally:
            link2.close()
            if proc2.poll() is None:
                proc2.kill()
                proc2.wait()
    finally:
        link.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    print("agent: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
