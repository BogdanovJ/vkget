from __future__ import annotations

import re
from dataclasses import dataclass


class WireGuardError(ValueError):
    pass


INTERFACE_REQUIRED = ("PrivateKey", "Address")
PEER_REQUIRED = ("PublicKey", "Endpoint", "AllowedIPs")
ALLOWED_INTERFACE = {
    "PrivateKey",
    "Address",
    "ListenPort",
    "MTU",
    "DNS",
    "Table",
    "FwMark",
}
ALLOWED_PEER = {
    "PublicKey",
    "PresharedKey",
    "Endpoint",
    "AllowedIPs",
    "PersistentKeepalive",
}
DENIED_KEYS = {
    "PostUp",
    "PostDown",
    "PreUp",
    "PreDown",
    "SaveConfig",
}

KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42,44}={0,2}$")
ENDPOINT_RE = re.compile(r"^[^:\s]+:\d{1,5}$")


@dataclass
class SanitizedWireGuard:
    text: str
    endpoint: str | None
    address: str | None


def _friendly(message: str) -> WireGuardError:
    return WireGuardError(message)


def sanitize_wireguard(config_text: str) -> SanitizedWireGuard:
    text = (config_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise _friendly("WireGuard config is empty")
    if "\x00" in text:
        raise _friendly("WireGuard config contains invalid characters")

    sections: list[tuple[str, dict[str, str]]] = []
    current: str | None = None
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if current is not None:
                sections.append((current, values))
            current = line[1:-1].strip()
            values = {}
            continue
        if current is None:
            raise _friendly("WireGuard config must start with [Interface]")
        if "=" not in line:
            raise _friendly(f"WireGuard config has an invalid line: {line}")
        key, value = line.split("=", 1)
        name = key.strip()
        if name in DENIED_KEYS:
            raise _friendly(
                f"WireGuard config cannot use {name} (scripts and extra hooks are blocked)"
            )
        values[name] = value.strip()
    if current is not None:
        sections.append((current, values))

    interfaces = [item for item in sections if item[0].lower() == "interface"]
    peers = [item for item in sections if item[0].lower() == "peer"]
    if not interfaces:
        raise _friendly("WireGuard config is missing [Interface]")
    if len(interfaces) != 1:
        raise _friendly("WireGuard config must have exactly one [Interface] section")
    if not peers:
        raise _friendly("WireGuard config is missing [Peer]")

    interface = interfaces[0][1]
    for required in INTERFACE_REQUIRED:
        if not interface.get(required):
            raise _friendly(f"WireGuard config is missing [Interface] {required}")
    for key in interface:
        if key not in ALLOWED_INTERFACE:
            raise _friendly(f"WireGuard [Interface] contains unsupported key: {key}")
    if not KEY_RE.fullmatch(interface["PrivateKey"]):
        raise _friendly("WireGuard [Interface] PrivateKey does not look like a valid key")

    kept = ["[Interface]"]
    for key in ("PrivateKey", "Address", "ListenPort", "MTU", "DNS", "Table", "FwMark"):
        if key in interface:
            kept.append(f"{key} = {interface[key]}")

    endpoint = None
    for _name, peer in peers:
        for required in PEER_REQUIRED:
            if not peer.get(required):
                raise _friendly(f"WireGuard config is missing [Peer] {required}")
        for key in peer:
            if key not in ALLOWED_PEER:
                raise _friendly(f"WireGuard [Peer] contains unsupported key: {key}")
        if not KEY_RE.fullmatch(peer["PublicKey"]):
            raise _friendly("WireGuard [Peer] PublicKey does not look like a valid key")
        if not ENDPOINT_RE.fullmatch(peer["Endpoint"]):
            raise _friendly("WireGuard [Peer] Endpoint must look like host:port")
        endpoint = peer["Endpoint"]
        kept.append("")
        kept.append("[Peer]")
        for key in ("PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"):
            if key in peer:
                kept.append(f"{key} = {peer[key]}")

    return SanitizedWireGuard(
        text="\n".join(kept).strip() + "\n",
        endpoint=endpoint,
        address=interface.get("Address"),
    )
