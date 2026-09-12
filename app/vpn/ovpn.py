from __future__ import annotations

import re
from dataclasses import dataclass

from .util import first_public_ipv4, normalize_public_ipv4


ALLOWED_DIRECTIVES = {
    "allow-compression",
    "auth",
    "auth-nocache",
    "auth-retry",
    "auth-user-pass",
    "bind",
    "block-ipv6",
    "cipher",
    "client",
    "comp-lzo",
    "compress",
    "connect-retry",
    "connect-retry-max",
    "connect-timeout",
    "data-ciphers",
    "data-ciphers-fallback",
    "dev",
    "dev-type",
    "dhcp-option",
    "explicit-exit-notify",
    "fast-io",
    "float",
    "fragment",
    "hand-window",
    "ignore-unknown-option",
    "inactive",
    "keepalive",
    "key-direction",
    "keysize",
    "mssfix",
    "mute",
    "mute-replay-warnings",
    "nobind",
    "ns-cert-type",
    "persist-key",
    "persist-local-ip",
    "persist-remote-ip",
    "persist-tun",
    "ping",
    "ping-restart",
    "ping-timer-rem",
    "port",
    "proto",
    "proto-force",
    "providers",
    "pull",
    "pull-filter",
    "rcvbuf",
    "redirect-gateway",
    "remote",
    "remote-cert-eku",
    "remote-cert-ku",
    "remote-cert-tls",
    "remote-random",
    "reneg-sec",
    "replay-window",
    "resolv-retry",
    "route",
    "route-delay",
    "route-metric",
    "rport",
    "server-poll-timeout",
    "setenv",
    "sndbuf",
    "socket-flags",
    "tls-cipher",
    "tls-ciphersuites",
    "tls-client",
    "tls-timeout",
    "tls-version-min",
    "topology",
    "tun-mtu",
    "tun-mtu-extra",
    "verb",
    "verify-x509-name",
}

DENIED_DIRECTIVES = {
    "askpass",
    "cd",
    "chroot",
    "client-connect",
    "client-config-dir",
    "client-disconnect",
    "config",
    "daemon",
    "down",
    "down-pre",
    "echo",
    "engine",
    "extra-certs",
    "group",
    "ifconfig-pool-persist",
    "ipchange",
    "learn-address",
    "log",
    "log-append",
    "management",
    "plugin",
    "replay-persist",
    "route-pre-down",
    "route-up",
    "script-security",
    "secret",
    "status",
    "syslog",
    "tls-crypt-v2-file",
    "tls-export-cert",
    "tls-verify",
    "tmp-dir",
    "up",
    "up-delay",
    "up-restart",
    "user",
    "writepid",
}

INLINE_BLOCKS = {
    "ca",
    "cert",
    "key",
    "tls-auth",
    "tls-crypt",
    "tls-crypt-v2",
    "extra-certs",
}

REMOTE_RE = re.compile(
    r"^remote\s+(\S+)(?:\s+(\d+))?(?:\s+(udp|tcp|udp4|tcp4|udp6|tcp6))?$",
    re.I,
)
PROTO_RE = re.compile(r"^proto\s+(\S+)$", re.I)


@dataclass
class SanitizedOvpn:
    text: str
    proto: str | None
    remote_host: str | None
    remote_port: int | None
    uses_ip_remote: bool


class OvpnError(ValueError):
    pass


def _normalize_proto(value: str | None) -> str | None:
    if not value:
        return None
    token = value.strip().lower()
    if token.startswith("udp"):
        return "udp"
    if token.startswith("tcp"):
        return "tcp"
    return None


def parse_remote(config_text: str) -> tuple[str | None, int | None, str | None]:
    proto = None
    host = None
    port = None
    for raw in (config_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        match = PROTO_RE.match(line)
        if match:
            proto = _normalize_proto(match.group(1)) or proto
            continue
        match = REMOTE_RE.match(line)
        if match:
            host = match.group(1)
            if match.group(2):
                port = int(match.group(2))
            proto = _normalize_proto(match.group(3)) or proto
    return host, port, proto


def config_uses_ip_remote(config_text: str, fallback_ip: str | None = None) -> bool:
    host, _, _ = parse_remote(config_text)
    if host and normalize_public_ipv4(host):
        return True
    if fallback_ip and host == fallback_ip:
        return True
    return False


def _is_denied(directive: str) -> bool:
    return directive in DENIED_DIRECTIVES or directive.startswith("ifconfig-ipv6")


def sanitize_ovpn(config_text: str, *, require_ca: bool = True) -> SanitizedOvpn:
    text = (config_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise OvpnError("empty OpenVPN configuration")
    if "\x00" in text:
        raise OvpnError("OpenVPN configuration contains NUL bytes")

    kept: list[str] = []
    inline: str | None = None
    inline_lines: list[str] = []
    has_ca = False
    remotes = 0
    denied_found: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if inline:
            inline_lines.append(line)
            if stripped.lower() == f"</{inline}>":
                kept.extend(inline_lines)
                if inline == "ca":
                    has_ca = True
                inline = None
                inline_lines = []
            continue
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith(";"):
            continue
        lower = stripped.lower()
        if lower.startswith("<") and lower.endswith(">") and not lower.startswith("</"):
            name = lower[1:-1].split()[0]
            if name not in INLINE_BLOCKS:
                raise OvpnError(f"rejected OpenVPN inline block: {name}")
            inline = name
            inline_lines = [stripped]
            continue

        directive = re.split(r"\s+", lower, 1)[0]
        if _is_denied(directive):
            denied_found.append(directive)
            continue
        if directive not in ALLOWED_DIRECTIVES:
            continue
        if directive == "auth-user-pass":
            kept.append("auth-user-pass")
            continue
        if directive == "remote":
            match = REMOTE_RE.match(stripped)
            if not match:
                raise OvpnError("invalid remote directive")
            remotes += 1
        kept.append(stripped)

    if inline:
        raise OvpnError("unterminated OpenVPN inline block")
    if denied_found:
        raise OvpnError(
            "rejected dangerous OpenVPN directives: " + ", ".join(sorted(set(denied_found)))
        )
    if remotes < 1:
        raise OvpnError("OpenVPN configuration is missing a remote")
    if require_ca and not has_ca:
        raise OvpnError("OpenVPN configuration is missing a CA certificate")

    if not any(line.lower().startswith("client") for line in kept):
        kept.insert(0, "client")
    if not any(line.lower().startswith("dev ") or line.lower() == "dev" for line in kept):
        kept.insert(1, "dev tun")
    if not any(line.lower().startswith("auth-nocache") for line in kept):
        kept.append("auth-nocache")
    if not any(line.lower().startswith("data-ciphers") for line in kept):
        kept.append(
            "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-256-CBC:AES-128-CBC"
        )
        kept.append("data-ciphers-fallback AES-128-CBC")

    sanitized = "\n".join(kept).strip() + "\n"
    host, port, proto = parse_remote(sanitized)
    uses_ip = bool(host and normalize_public_ipv4(host))
    if not uses_ip:
        uses_ip = bool(first_public_ipv4(host or ""))
    return SanitizedOvpn(
        text=sanitized,
        proto=proto,
        remote_host=host,
        remote_port=port,
        uses_ip_remote=uses_ip,
    )
