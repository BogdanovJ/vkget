from __future__ import annotations

from datetime import datetime, timedelta


def is_manual(endpoint) -> bool:
    if getattr(endpoint, "is_manual", None) and callable(endpoint.is_manual):
        return bool(endpoint.is_manual())
    if getattr(endpoint, "source", "") == "manual":
        return True
    sources = getattr(endpoint, "sources", "") or ""
    return "manual" in {part.strip() for part in sources.split(",") if part.strip()}


def is_known_good(endpoint, current: datetime | None = None) -> bool:
    if getattr(endpoint, "verified_country", None) != "RU":
        return False
    if getattr(endpoint, "consecutive_failures", 0):
        return False
    if not getattr(endpoint, "last_verified_at", None) and not getattr(
        endpoint, "last_success_at", None
    ):
        return False
    return True


# score =
#   recent_success + verified_bonus + advertised_bonus + udp_ip_bonus
#   + latency + uptime + modest_speed + manual_priority
#   - sessions - consecutive_failures - recent_timeout/verify
def compute_score(endpoint, current: datetime | None = None) -> int:
    when = current or datetime.now()
    score = 0

    if is_manual(endpoint):
        priority = getattr(endpoint, "priority", None)
        score += int(priority) if isinstance(priority, int) and priority else 50
    else:
        priority = getattr(endpoint, "priority", None)
        if isinstance(priority, int):
            score += priority

    last_success = getattr(endpoint, "last_success_at", None)
    last_verified = getattr(endpoint, "last_verified_at", None)
    if last_success and when - last_success <= timedelta(hours=24):
        score += 40
    if getattr(endpoint, "verified_country", None) == "RU" and last_verified:
        score += 25
    if not getattr(endpoint, "is_stale", False):
        score += 15

    if getattr(endpoint, "openvpn_udp_config", None) and getattr(
        endpoint, "udp_config_is_ip", False
    ):
        score += 20
    elif getattr(endpoint, "openvpn_udp_config", None) or getattr(
        endpoint, "openvpn_udp_ddns_config", None
    ):
        score += 8

    latency = getattr(endpoint, "measured_latency_ms", None)
    if latency is not None:
        score += max(0, 25 - int(latency) // 20)
    else:
        ping = getattr(endpoint, "reported_ping_ms", None)
        if ping is not None:
            score += max(0, 12 - int(ping) // 40)

    uptime_ms = getattr(endpoint, "reported_uptime_ms", None) or 0
    if uptime_ms > 0:
        score += min(int(uptime_ms / (24 * 60 * 60 * 1000)), 8)

    speed = getattr(endpoint, "reported_speed_bps", None) or 0
    if speed > 0:
        score += min(int(speed / 2_000_000), 10)

    sessions = getattr(endpoint, "reported_sessions", None) or 0
    score -= min(int(sessions), 10)

    failures = getattr(endpoint, "consecutive_failures", None) or 0
    score -= failures * 12

    last_failure = getattr(endpoint, "last_failure_at", None)
    reason = (getattr(endpoint, "failure_reason", None) or "").lower()
    if last_failure and (not last_success or last_failure > last_success):
        if when - last_failure <= timedelta(hours=3):
            if "timeout" in reason:
                score -= 20
            elif "verif" in reason or "country" in reason:
                score -= 18
            else:
                score -= 12

    return score
