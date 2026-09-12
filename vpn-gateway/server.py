#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ovpn import sanitize_ovpn


LISTEN_HOST = os.getenv("VPN_GATEWAY_LISTEN", "0.0.0.0")
LISTEN_PORT = int(os.getenv("VPN_GATEWAY_PORT", "8081"))
CONNECT_TIMEOUT = int(os.getenv("VPN_CONNECT_TIMEOUT", "20"))
OPENVPN_BIN = os.getenv("OPENVPN_BIN", "openvpn")
DEBUG = os.getenv("VPN_GATEWAY_DEBUG", "").strip().lower() in {"1", "true", "yes"}

_lock = threading.Lock()
_state = {
    "connected": False,
    "endpoint_ip": None,
    "protocol": None,
    "detail": "disconnected",
    "workdir": None,
    "proc": None,
}


def _log(message: str) -> None:
    print(f"vpn-gateway: {message}", flush=True)


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _cleanup_locked() -> None:
    proc = _state.get("proc")
    _terminate(proc)
    workdir = _state.get("workdir")
    if workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    _state.update({
        "connected": False,
        "endpoint_ip": None,
        "protocol": None,
        "detail": "disconnected",
        "workdir": None,
        "proc": None,
    })


def disconnect() -> None:
    with _lock:
        _cleanup_locked()
    _log("disconnected")


def _wait_for_connect(proc: subprocess.Popen, timeout: int) -> tuple[bool, str]:
    deadline = time.time() + timeout
    lines: list[str] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            rest = ""
            if proc.stdout:
                rest = proc.stdout.read() or ""
            lines.append(rest)
            return False, "openvpn exited before connect: " + " | ".join(
                line.strip() for line in "".join(lines).splitlines() if line.strip()
            )[-400:]
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.1)
            continue
        lines.append(line)
        if DEBUG:
            _log(line.rstrip())
        if "Initialization Sequence Completed" in line:
            return True, "connected"
    _terminate(proc)
    return False, "openvpn connect timed out"


def connect(payload: dict) -> tuple[int, dict]:
    config = payload.get("config") or ""
    protocol = str(payload.get("protocol") or "udp")
    endpoint_ip = payload.get("endpoint_ip")
    username = str(payload.get("username") or "vpn")
    password = str(payload.get("password") or "vpn")
    try:
        sanitized = sanitize_ovpn(config)
    except ValueError as exc:
        return 400, {"ok": False, "detail": str(exc)}

    workdir = tempfile.mkdtemp(prefix="vkget-ovpn-")
    os.chmod(workdir, 0o700)
    config_path = Path(workdir) / "client.ovpn"
    auth_path = Path(workdir) / "auth"
    config_path.write_text(sanitized, encoding="utf-8")
    os.chmod(config_path, 0o600)
    auth_path.write_text(f"{username}\n{password}\n", encoding="utf-8")
    os.chmod(auth_path, 0o600)

    args = [
        OPENVPN_BIN,
        "--config", str(config_path),
        "--auth-user-pass", str(auth_path),
        "--verb", "4" if DEBUG else "1",
        "--connect-timeout", str(CONNECT_TIMEOUT),
        "--auth-nocache",
        "--script-security", "1",
    ]
    _log(f"starting openvpn protocol={protocol}")
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=workdir,
        )
    except OSError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return 500, {"ok": False, "detail": f"failed to start openvpn: {exc}"}

    ok, detail = _wait_for_connect(proc, CONNECT_TIMEOUT)
    if not ok:
        shutil.rmtree(workdir, ignore_errors=True)
        with _lock:
            _state["detail"] = detail
        _log(detail)
        return 502, {"ok": False, "detail": detail}
    with _lock:
        _state.update({
            "connected": True,
            "endpoint_ip": endpoint_ip,
            "protocol": protocol,
            "detail": "connected",
            "workdir": workdir,
            "proc": proc,
        })
    _log("connected")
    return 200, {
        "ok": True,
        "connected": True,
        "endpoint_ip": endpoint_ip,
        "protocol": protocol,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        _log(fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {"/healthz", "/"}:
            self._send(200, {"ok": True})
            return
        if self.path == "/status":
            with _lock:
                proc = _state.get("proc")
                connected = bool(_state["connected"] and proc is not None and proc.poll() is None)
                payload = {
                    "connected": connected,
                    "endpoint_ip": _state["endpoint_ip"] if connected else None,
                    "protocol": _state["protocol"] if connected else None,
                    "detail": _state["detail"] if connected else "disconnected",
                }
            self._send(200, payload)
            return
        self._send(404, {"ok": False, "detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "detail": "invalid JSON"})
            return
        if self.path == "/disconnect":
            disconnect()
            self._send(200, {"ok": True, "connected": False})
            return
        if self.path == "/connect":
            if not isinstance(payload, dict):
                self._send(400, {"ok": False, "detail": "invalid payload"})
                return
            disconnect()
            code, body = connect(payload)
            self._send(code, body)
            return
        self._send(404, {"ok": False, "detail": "not found"})


def main() -> None:
    def _stop(signum, _frame):
        _log(f"signal {signum}, shutting down")
        disconnect()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    _log(f"listening on {LISTEN_HOST}:{LISTEN_PORT}")
    try:
        server.serve_forever()
    finally:
        disconnect()
        server.server_close()


if __name__ == "__main__":
    main()
