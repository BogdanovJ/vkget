from __future__ import annotations

from ..config import settings


ELIGIBLE_HINTS = (
    "403",
    "451",
    "forbidden",
    "access denied",
    "geo",
    "region",
    "unavailable in your",
    "blocked",
    "okcdn",
    "cdn",
    "timed out",
    "timeout",
    "connection reset",
    "remote end closed",
    "fragment",
    "unable to download",
    "http error 403",
    "http error 451",
    "redirect",
    "throttl",
    "too slow",
    "vpn_slow_rate",
    "challenge",
    "cloudflare",
    "unusual traffic",
    "rate-limit",
    "too many requests",
    "429",
)

INELIGIBLE_HINTS = (
    "malformed url",
    "unsupported url",
    "is not a valid url",
    "video has been deleted",
    "video is deleted",
    "this video has been removed",
    "does not exist",
    "not found",
    "404",
    "no video formats",
    "unsupported url",
    "disk full",
    "no space left",
    "read-only file system",
    "destination not writable",
    "permission denied",
    "database error",
    "operationalerror",
    "integrityerror",
)


def vpn_mode() -> str:
    raw = getattr(settings, "vk_vpn_mode", "auto")
    if not isinstance(raw, str):
        return "auto"
    mode = raw.strip().lower()
    if mode not in {"off", "auto", "always"}:
        return "auto"
    return mode


def vpn_eligible_failure(error: str | None) -> bool:
    text = (error or "").lower()
    if not text:
        return False
    if any(hint in text for hint in INELIGIBLE_HINTS):
        if any(hint in text for hint in ("403", "451", "forbidden", "okcdn", "vpn_slow_rate")):
            return True
        return False
    return any(hint in text for hint in ELIGIBLE_HINTS)
