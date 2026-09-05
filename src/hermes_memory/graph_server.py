"""Viz API process entrypoints (serve / daemon / CLI)."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, Optional

from .graph_http import Handler
from .graph_runtime import Runtime, _load_dotenv_files  # noqa: F401
from .graph_view import DEFAULT_HOST, DEFAULT_PORT, validate_bind_host

logger = logging.getLogger("hermes_memory.graph_api")

PID_PATH = Path.home() / ".hermes" / "run" / "hermes-memory-api.pid"

RUNTIME: Optional[Runtime] = None

def wait_ready(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 8.0) -> bool:
    import urllib.request

    url = f"http://{host}:{port}/api/librarian/graph/stats?fresh=1"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200 and len(resp.read()) > 20:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> Optional[int]:
    try:
        return int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def write_pid(pid: int) -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(pid) + "\n")


def stop_daemon() -> bool:
    pid = read_pid()
    if pid and pid_is_alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not pid_is_alive(pid):
                break
            time.sleep(0.1)
        if pid_is_alive(pid):
            os.kill(pid, signal.SIGKILL)
    # Only the serve listener — never pkill start/stop/status argv.
    try:
        import subprocess

        subprocess.run(["pkill", "-f", "hermes_memory.graph_api serve"], capture_output=True)
    except Exception:
        pass
    try:
        PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    global RUNTIME
    host = validate_bind_host(host)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    RUNTIME = Runtime()
    httpd = ThreadingHTTPServer((host, port), Handler)
    write_pid(os.getpid())
    logger.info("graph api listening on http://%s:%s", host, port)

    def _shutdown(*_args: Any) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if RUNTIME is not None:
            RUNTIME.close()
            RUNTIME = None
        try:
            PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass


def start_daemon(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Detach a listener. Used by hermes-memory-install after pip install -e."""
    host = validate_bind_host(host)
    if wait_ready(host, port, timeout=1.0):
        logger.info("graph api already up")
        return
    stop_daemon()
    log_path = Path("/tmp/librarian_api.log")
    cmd = [sys.executable, "-m", "hermes_memory.graph_api", "serve",
           "--host", host, "--port", str(port)]
    import subprocess

    proc = subprocess.Popen(
        cmd,
        stdout=open(log_path, "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    write_pid(proc.pid)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-memory-api")
    sub = parser.add_subparsers(dest="cmd")
    serve_p = sub.add_parser("serve", help="foreground listener (default)")
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    start_p = sub.add_parser("start", help="daemonize")
    start_p.add_argument("--host", default=DEFAULT_HOST)
    start_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("stop")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    cmd = args.cmd or "serve"
    if cmd == "serve":
        host = getattr(args, "host", DEFAULT_HOST)
        port = getattr(args, "port", DEFAULT_PORT)
        try:
            host = validate_bind_host(host)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        serve(host, port)
        return 0
    if cmd == "start":
        try:
            host = validate_bind_host(args.host)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        start_daemon(host, args.port)
        if wait_ready(args.host, args.port, timeout=8.0):
            print(f"graph api up on http://{args.host}:{args.port}")
            return 0
        print("graph api failed to become ready; see /tmp/librarian_api.log", file=sys.stderr)
        return 1
    if cmd == "stop":
        stop_daemon()
        print("graph api stopped")
        return 0
    if cmd == "status":
        ready = wait_ready(timeout=1.5)
        pid = read_pid()
        print("up" if ready else "down", f"pid={pid or '-'}")
        return 0 if ready else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
