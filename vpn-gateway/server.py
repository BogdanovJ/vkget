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
from wg import sanitize_wireguard


LISTEN_HOST = os.getenv("VPN_GATEWAY_LISTEN", "0.0.0.0")
LISTEN_PORT = int(os.getenv("VPN_GATEWAY_PORT", "8081"))
CONNECT_TIMEOUT = int(os.getenv("VPN_CONNECT_TIMEOUT", "15"))
OPENVPN_BIN = os.getenv("OPENVPN_BIN", "openvpn")
WG_QUICK_BIN = os.getenv("WG_QUICK_BIN", "wg-quick")
DEBUG = os.getenv("VPN_GATEWAY_DEBUG", "").strip().lower() in {"1", "true", "yes"}

_lock = threading.Lock()
_state = {
    "connected": False,
    "type": None,
    "endpoint_ip": None,
    "detail": "disconnected",
    "workdir": None,
    "proc": None,
    "iface": None,
}


def _log(message: str) -> None:
    print(f"vpn-gateway: {message}", flush=True)


def write_http_body(write, body: bytes) -> bool:
    try:
        write(body)
        return True
    except (BrokenPipeError, ConnectionResetError) as exc:
        _log(f"client disconnected before response write: {type(exc).__name__}")
        return False


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wg_down(workdir: str | None) -> None:
    if not workdir:
        return
    conf = Path(workdir) / "wg0.conf"
    if not conf.exists():
        return
    subprocess.run(
        [WG_QUICK_BIN, "down", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )


def _cleanup_locked() -> None:
    proc = _state.get("proc")
    workdir = _state.get("workdir")
    vpn_type = _state.get("type")
    _terminate(proc)
    if vpn_type == "wireguard":
        _wg_down(workdir)
    if workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    _state.update({
        "connected": False,
        "type": None,
        "endpoint_ip": None,
        "detail": "disconnected",
        "workdir": None,
        "proc": None,
        "iface": None,
    })


def disconnect() -> None:
    with _lock:
        was_connected = bool(_state.get("connected") or _state.get("proc") or _state.get("workdir"))
        _cleanup_locked()
    if was_connected:
        _log("disconnected")


def _wait_for_openvpn(proc: subprocess.Popen, timeout: int) -> tuple[bool, str]:
    deadline = time.time() + timeout
    lines: list[str] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            rest = ""
            if proc.stdout:
                rest = proc.stdout.read() or ""
            lines.append(rest)
            return False, "OpenVPN failed: " + " | ".join(
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
        lower = line.lower()
        if "tls handshake failed" in lower or "tls-error" in lower:
            _terminate(proc)
            return False, "OpenVPN failed: TLS handshake timeout"
        if "auth_failed" in lower or "auth failed" in lower:
            _terminate(proc)
            return False, "OpenVPN failed: authentication failed"
    _terminate(proc)
    return False, "OpenVPN failed: connect timed out"


def _connect_openvpn(payload: dict) -> tuple[int, dict]:
    config = payload.get("config") or ""
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    try:
        sanitized = sanitize_ovpn(config)
    except ValueError as exc:
        return 400, {"ok": False, "detail": str(exc)}

    workdir = tempfile.mkdtemp(prefix="vkget-ovpn-")
    os.chmod(workdir, 0o700)
    config_path = Path(workdir) / "client.ovpn"
    config_path.write_text(sanitized, encoding="utf-8")
    os.chmod(config_path, 0o600)

    args = [
        OPENVPN_BIN,
        "--config", str(config_path),
        "--verb", "4" if DEBUG else "1",
        "--connect-timeout", str(CONNECT_TIMEOUT),
        "--auth-nocache",
        "--script-security", "1",
    ]
    if "auth-user-pass" in sanitized:
        auth_path = Path(workdir) / "auth"
        auth_path.write_text(f"{username}\n{password}\n", encoding="utf-8")
        os.chmod(auth_path, 0o600)
        args += ["--auth-user-pass", str(auth_path)]

    _log("connecting profile type=openvpn")
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
        return 500, {"ok": False, "detail": f"failed to start OpenVPN: {exc}"}

    ok, detail = _wait_for_openvpn(proc, CONNECT_TIMEOUT)
    if not ok:
        shutil.rmtree(workdir, ignore_errors=True)
        with _lock:
            _state["detail"] = detail
        _log(detail)
        return 502, {"ok": False, "detail": detail}
    with _lock:
        _state.update({
            "connected": True,
            "type": "openvpn",
            "endpoint_ip": payload.get("endpoint_ip"),
            "detail": "connected",
            "workdir": workdir,
            "proc": proc,
            "iface": None,
        })
    _log("OpenVPN connected")
    _log("SOCKS routing ready")
    return 200, {"ok": True, "connected": True, "type": "openvpn"}


def _connect_wireguard(payload: dict) -> tuple[int, dict]:
    config = payload.get("config") or ""
    try:
        sanitized = sanitize_wireguard(config)
    except ValueError as exc:
        return 400, {"ok": False, "detail": str(exc)}

    workdir = tempfile.mkdtemp(prefix="vkget-wg-")
    os.chmod(workdir, 0o700)
    config_path = Path(workdir) / "wg0.conf"
    config_path.write_text(sanitized, encoding="utf-8")
    os.chmod(config_path, 0o600)

    _log("connecting profile type=wireguard")
    try:
        result = subprocess.run(
            [WG_QUICK_BIN, "up", str(config_path)],
            capture_output=True,
            text=True,
            timeout=CONNECT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        detail = f"WireGuard failed: {exc}"
        _log(detail)
        return 502, {"ok": False, "detail": detail}
    if result.returncode != 0:
        _wg_down(workdir)
        shutil.rmtree(workdir, ignore_errors=True)
        detail = "WireGuard failed: " + (
            (result.stderr or result.stdout or "wg-quick up failed").strip()[-400:]
        )
        _log(detail)
        return 502, {"ok": False, "detail": detail}

    show = subprocess.run(
        ["wg", "show", "wg0"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if show.returncode != 0:
        _wg_down(workdir)
        shutil.rmtree(workdir, ignore_errors=True)
        detail = "WireGuard failed: interface was not created"
        _log(detail)
        return 502, {"ok": False, "detail": detail}

    with _lock:
        _state.update({
            "connected": True,
            "type": "wireguard",
            "endpoint_ip": payload.get("endpoint_ip"),
            "detail": "connected",
            "workdir": workdir,
            "proc": None,
            "iface": "wg0",
        })
    _log("WireGuard connected")
    _log("SOCKS routing ready")
    return 200, {"ok": True, "connected": True, "type": "wireguard"}


def connect(payload: dict) -> tuple[int, dict]:
    vpn_type = str(payload.get("type") or "openvpn").strip().lower()
    if vpn_type in {"wg", "wireguard"}:
        vpn_type = "wireguard"
    elif vpn_type in {"ovpn", "openvpn"}:
        vpn_type = "openvpn"
    else:
        return 400, {"ok": False, "detail": "type must be openvpn or wireguard"}
    if vpn_type == "wireguard":
        return _connect_wireguard(payload)
    return _connect_openvpn(payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        _log(fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            write_http_body(self.wfile.write, body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            _log(f"client disconnected before response write: {type(exc).__name__}")

    def do_GET(self):
        if self.path in {"/healthz", "/"}:
            self._send(200, {"ok": True})
            return
        if self.path == "/status":
            with _lock:
                proc = _state.get("proc")
                connected = bool(_state["connected"])
                if proc is not None:
                    connected = connected and proc.poll() is None
                payload = {
                    "connected": connected,
                    "type": _state["type"] if connected else None,
                    "protocol": _state["type"] if connected else None,
                    "endpoint_ip": _state["endpoint_ip"] if connected else None,
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
    _log("tunnel disconnected")
    try:
        server.serve_forever()
    finally:
        disconnect()
        server.server_close()


if __name__ == "__main__":
    main()
