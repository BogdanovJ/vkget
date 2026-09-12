from __future__ import annotations

import re


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
DENIED_KEYS = {"PostUp", "PostDown", "PreUp", "PreDown", "SaveConfig"}
KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42,44}={0,2}$")
ENDPOINT_RE = re.compile(r"^[^:\s]+:\d{1,5}$")


def sanitize_wireguard(config_text: str) -> str:
    text = (config_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() or "\x00" in text:
        raise ValueError("invalid WireGuard configuration")

    sections: list[tuple[str, dict[str, str]]] = []
    current = None
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
            raise ValueError("WireGuard config is missing [Interface]")
        if "=" not in line:
            raise ValueError("invalid WireGuard configuration")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    if current is not None:
        sections.append((current, values))

    interfaces = [item for item in sections if item[0].lower() == "interface"]
    peers = [item for item in sections if item[0].lower() == "peer"]
    if len(interfaces) != 1:
        raise ValueError("WireGuard config must have exactly one [Interface] section")
    if not peers:
        raise ValueError("WireGuard config is missing [Peer]")

    interface = interfaces[0][1]
    for required in INTERFACE_REQUIRED:
        if not interface.get(required):
            raise ValueError(f"WireGuard config is missing [Interface] {required}")
    for key in interface:
        if key in DENIED_KEYS:
            raise ValueError(f"WireGuard config cannot use {key}")
        if key not in ALLOWED_INTERFACE:
            raise ValueError(f"unsupported WireGuard [Interface] key: {key}")
    if not KEY_RE.fullmatch(interface["PrivateKey"]):
        raise ValueError("WireGuard [Interface] PrivateKey is invalid")

    kept = ["[Interface]"]
    for key in ("PrivateKey", "Address", "ListenPort", "MTU", "DNS", "Table", "FwMark"):
        if key in interface:
            kept.append(f"{key} = {interface[key]}")

    for _name, peer in peers:
        for required in PEER_REQUIRED:
            if not peer.get(required):
                raise ValueError(f"WireGuard config is missing [Peer] {required}")
        for key in peer:
            if key in DENIED_KEYS:
                raise ValueError(f"WireGuard config cannot use {key}")
            if key not in ALLOWED_PEER:
                raise ValueError(f"unsupported WireGuard [Peer] key: {key}")
        if not KEY_RE.fullmatch(peer["PublicKey"]):
            raise ValueError("WireGuard [Peer] PublicKey is invalid")
        if not ENDPOINT_RE.fullmatch(peer["Endpoint"]):
            raise ValueError("WireGuard [Peer] Endpoint must look like host:port")
        kept.append("")
        kept.append("[Peer]")
        for key in ("PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"):
            if key in peer:
                kept.append(f"{key} = {peer[key]}")
    return "\n".join(kept).strip() + "\n"
