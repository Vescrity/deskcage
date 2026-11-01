"""In-sandbox process agent.

The agent is PID 1 inside the bubblewrap namespace.  It listens on a unix
socket that the host daemon connects to, spawns processes on request, reports
their exit status, and keeps the sandbox alive until told otherwise.

It only uses the Python standard library so it can run against the /usr that
the sandbox already has.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)


class Client:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.lock = threading.Lock()
        self.alive = True

    def send(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        with self.lock:
            if not self.alive:
                return False
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                self.alive = False
                return False

    def close(self) -> None:
        with self.lock:
            self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class Agent:
    def __init__(self, socket_path: str, log_dir: str, keep: bool):
        self.socket_path = socket_path
        self.log_dir = log_dir
        self.keep = keep
        self.children: dict[int, dict] = {}
        self.exited: dict[int, int] = {}
        self.clients: list[Client] = []
        self.clients_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.events: queue.Queue = queue.Queue()
        self.exit_event = threading.Event()
        self.ever_spawned = False
        self.shutting_down = False
        self.server: socket.socket | None = None
        self._log_seq = 0

    # ------------------------------------------------------------- lifecycle

    def run(self) -> int:
        os.umask(0o077)
        os.makedirs(self.log_dir, exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.server.listen(8)
        # A short timeout keeps the main thread running Python bytecode, so
        # SIGTERM/SIGINT handlers are not stuck behind a blocking accept().
        self.server.settimeout(0.5)

        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)

        threading.Thread(target=self._broadcaster, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()

        while not self.shutting_down:
            try:
                conn, _address = self.server.accept()
            except socket.timeout:
                continue
            except InterruptedError:
                continue
            except OSError:
                break
            client = Client(conn)
            with self.clients_lock:
                self.clients.append(client)
            threading.Thread(
                target=self._serve_client, args=(client,), daemon=True
            ).start()

        self._shutdown(grace=0.5)
        return 0

    def _on_term(self, signum, frame) -> None:
        self.exit_event.set()

    def _reaper(self) -> None:
        """Reap children without relying on SIGCHLD delivery.

        Signals are process directed and may land on any thread; a worker
        thread receiving SIGCHLD does not interrupt the main thread's
        accept(), so a signal handler is not a reliable reaper here.
        """
        while True:
            try:
                pid, status = os.waitpid(-1, 0)
            except ChildProcessError:
                time.sleep(0.1)
                continue
            except OSError:
                if self.shutting_down:
                    return
                time.sleep(0.1)
                continue
            code = os.waitstatus_to_exitcode(status)
            with self.state_lock:
                info = self.children.pop(pid, None)
                self.exited[pid] = code
            if info is None:
                continue  # orphan re-parented to PID 1
            self.events.put(
                {"event": "exit", "pid": pid, "status": code, "argv": info["argv"]}
            )
            self._maybe_exit()

    def _maybe_exit(self) -> None:
        with self.state_lock:
            if self.shutting_down or self.keep or not self.ever_spawned:
                return
            if self.children:
                return
        self.exit_event.set()

    def _monitor(self) -> None:
        while not self.exit_event.wait(0.25):
            self._maybe_exit()
        self._shutdown(grace=1.0)

    def _shutdown(self, grace: float = 2.0) -> None:
        with self.state_lock:
            self.shutting_down = True
            pids = list(self.children)
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self.state_lock:
                if not self.children:
                    break
            time.sleep(0.05)
        with self.state_lock:
            pids = list(self.children)
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        # Give the broadcaster a moment to flush pending exit events before
        # the socket disappears with us.
        deadline = time.monotonic() + 0.5
        while not self.events.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        # PID 1 exiting tears down the whole namespace anyway.
        os._exit(0)

    # --------------------------------------------------------------- service

    def _broadcaster(self) -> None:
        while True:
            event = self.events.get()
            with self.clients_lock:
                clients = list(self.clients)
            for client in clients:
                if not client.send(event):
                    self._drop_client(client)

    def _drop_client(self, client: Client) -> None:
        with self.clients_lock:
            if client in self.clients:
                self.clients.remove(client)
        client.close()

    def _serve_client(self, client: Client) -> None:
        buffer = b""
        try:
            while not self.shutting_down:
                chunk = client.sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        request = json.loads(line)
                    except json.JSONDecodeError:
                        client.send({"ok": False, "error": "invalid json"})
                        continue
                    self._handle(client, request)
        except OSError:
            pass
        finally:
            self._drop_client(client)

    def _handle(self, client: Client, request: dict) -> None:
        request_id = request.get("id")
        command = request.get("cmd")

        def reply(**fields) -> None:
            client.send({"id": request_id, "ok": True, **fields})

        try:
            if command == "ping":
                reply()
            elif command == "list":
                with self.state_lock:
                    children = [
                        {"pid": pid, **info} for pid, info in self.children.items()
                    ]
                reply(children=children)
            elif command == "spawn":
                argv = request.get("argv") or []
                if not isinstance(argv, list) or not argv or not all(
                    isinstance(item, str) for item in argv
                ):
                    client.send({"id": request_id, "ok": False, "error": "invalid argv"})
                    return
                env = request.get("env") or {}
                if not isinstance(env, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in env.items()
                ):
                    client.send({"id": request_id, "ok": False, "error": "invalid env"})
                    return
                cwd = request.get("cwd") or os.environ.get("HOME", "/")
                pid, log = self._spawn(argv, env, cwd)
                reply(pid=pid, log=log)
            elif command == "set_keep":
                self.keep = bool(request.get("keep"))
                reply(keep=self.keep)
                self._maybe_exit()
            elif command == "shutdown":
                reply()
                threading.Thread(target=self._shutdown, daemon=True).start()
            else:
                client.send(
                    {"id": request_id, "ok": False, "error": f"unknown command {command}"}
                )
        except Exception as exc:  # noqa: BLE001 - keep the agent alive
            client.send({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # ---------------------------------------------------------------- spawn

    def _spawn(self, argv: list[str], env: dict[str, str], cwd: str) -> tuple[int, str]:
        self._log_seq += 1
        name = f"{time.time_ns()}-{self._log_seq}.log"
        host_log = os.path.join(self.log_dir, name)
        log_fd = os.open(host_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        devnull = os.open(os.devnull, os.O_RDONLY)

        child_env = dict(os.environ)
        child_env.update(env)

        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            try:
                os.setsid()
                os.dup2(devnull, 0)
                os.dup2(log_fd, 1)
                os.dup2(log_fd, 2)
                os.close(log_fd)
                os.close(devnull)
                os.chdir(cwd)
                os.execvpe(argv[0], argv, child_env)
            except BaseException as exc:  # noqa: BLE001
                try:
                    os.write(2, f"deskcage-agent: cannot execute {argv[0]}: {exc}\n".encode())
                finally:
                    os._exit(127)

        os.close(log_fd)
        os.close(devnull)
        with self.state_lock:
            self.children[pid] = {
                "argv": list(argv),
                "started": time.time(),
                "log": host_log,
            }
            self.ever_spawned = True
            # The reaper may have already collected this child.
            early = self.exited.pop(pid, None)
        if early is not None:
            with self.state_lock:
                self.children.pop(pid, None)
            self.events.put(
                {"event": "exit", "pid": pid, "status": early, "argv": list(argv)}
            )
            self._maybe_exit()
        return pid, name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deskcage-agent")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--keep", type=int, default=1)
    args = parser.parse_args(argv)
    agent = Agent(args.socket, args.log_dir, bool(args.keep))
    return agent.run()


if __name__ == "__main__":
    sys.exit(main())
