from __future__ import annotations

import ipaddress
import re
from datetime import datetime


IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def now() -> datetime:
    return datetime.now()


def normalize_public_ipv4(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if ip.version != 4:
        return None
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or ip.is_link_local
    ):
        return None
    return str(ip)


def first_public_ipv4(text: str | None) -> str | None:
    for match in IPV4_RE.findall(text or ""):
        ip = normalize_public_ipv4(match)
        if ip:
            return ip
    return None


def parse_int(value, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_rate_bps(value: str | None) -> int | None:
    raw = (value or "").strip().upper().replace(" ", "")
    if not raw:
        return None
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)([KMG])?(?:IB|/S|BPS)?", raw)
    if not match:
        return parse_int(raw)
    number = float(match.group(1))
    suffix = match.group(2)
    multiplier = 1
    if suffix == "K":
        multiplier = 1024
    elif suffix == "M":
        multiplier = 1024 * 1024
    elif suffix == "G":
        multiplier = 1024 * 1024 * 1024
    return int(number * multiplier)


def format_ago(value: datetime | None, current: datetime | None = None) -> str:
    if value is None:
        return "NEVER"
    delta = (current or now()) - value
    secs = int(delta.total_seconds())
    if secs < 0:
        return "NOW"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"
