from __future__ import annotations

from datetime import datetime, timedelta


# Keep this formula short and readable:
# score = udp_bonus + ip_config_bonus
#       + speed_points + latency_points + uptime_points + success_points
#       - session_penalty - failure_penalty - recent_fail_penalty
def compute_score(endpoint, current: datetime | None = None) -> int:
    when = current or datetime.now()
    score = 0

    if getattr(endpoint, "openvpn_udp_config", None):
        score += 25
        if getattr(endpoint, "udp_config_is_ip", False):
            score += 15
        elif getattr(endpoint, "tcp_config_is_ip", False):
            score += 5
    elif getattr(endpoint, "openvpn_tcp_config", None):
        score += 8
        if getattr(endpoint, "tcp_config_is_ip", False):
            score += 8

    speed = getattr(endpoint, "reported_speed_bps", None) or 0
    if speed > 0:
        score += min(int(speed / 1_000_000), 30)
        if speed < 1_000_000:
            score -= 8

    ping = getattr(endpoint, "measured_latency_ms", None)
    if ping is None:
        ping = getattr(endpoint, "reported_ping_ms", None)
    if ping is not None:
        score += max(0, 20 - int(ping) // 25)

    uptime_ms = getattr(endpoint, "reported_uptime_ms", None) or 0
    if uptime_ms > 0:
        score += min(int(uptime_ms / (24 * 60 * 60 * 1000)), 10)

    successes = getattr(endpoint, "successful_connections", None) or 0
    score += min(successes, 15)

    last_success = getattr(endpoint, "last_success_at", None)
    if last_success and when - last_success <= timedelta(hours=6):
        score += 10

    sessions = getattr(endpoint, "reported_sessions", None) or 0
    score -= min(int(sessions), 20)

    failures = getattr(endpoint, "consecutive_failures", None) or 0
    score -= failures * 8

    last_failure = getattr(endpoint, "last_failure_at", None)
    if last_failure and (not last_success or last_failure > last_success):
        if when - last_failure <= timedelta(hours=3):
            score -= 15

    return score
