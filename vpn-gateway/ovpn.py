from __future__ import annotations

import re


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


def sanitize_ovpn(config_text: str) -> str:
    text = (config_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() or "\x00" in text:
        raise ValueError("invalid OpenVPN configuration")

    kept: list[str] = []
    inline = None
    inline_lines: list[str] = []
    has_ca = False
    remotes = 0
    denied: list[str] = []

    for raw in text.splitlines():
        stripped = raw.strip()
        if inline:
            inline_lines.append(raw.rstrip())
            if stripped.lower() == f"</{inline}>":
                kept.extend(inline_lines)
                if inline == "ca":
                    has_ca = True
                inline = None
                inline_lines = []
            continue
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        lower = stripped.lower()
        if lower.startswith("<") and lower.endswith(">") and not lower.startswith("</"):
            name = lower[1:-1].split()[0]
            if name not in INLINE_BLOCKS:
                raise ValueError(f"rejected inline block: {name}")
            inline = name
            inline_lines = [stripped]
            continue
        directive = re.split(r"\s+", lower, 1)[0]
        if directive in DENIED_DIRECTIVES:
            denied.append(directive)
            continue
        if directive not in ALLOWED_DIRECTIVES:
            continue
        if directive == "auth-user-pass":
            kept.append("auth-user-pass")
            continue
        if directive == "remote":
            if not REMOTE_RE.match(stripped):
                raise ValueError("invalid remote directive")
            remotes += 1
        kept.append(stripped)

    if inline:
        raise ValueError("unterminated OpenVPN inline block")
    if denied:
        raise ValueError("rejected dangerous OpenVPN directives: " + ", ".join(sorted(set(denied))))
    if remotes < 1 or not has_ca:
        raise ValueError("OpenVPN configuration is missing remote or CA")
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
    if not any(line.lower().startswith("script-security") for line in kept):
        kept.append("script-security 1")
    return "\n".join(kept).strip() + "\n"
