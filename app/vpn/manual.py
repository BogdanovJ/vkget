from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ..config import settings
from ..models import VpnEndpoint
from .gate import DiscoveredEndpoint, apply_sanitized_config
from .ovpn import OvpnError, sanitize_ovpn
from .scoring import compute_score
from .util import first_public_ipv4, merge_sources, normalize_public_ipv4, now


class ManualEndpointError(ValueError):
    pass


def upsert_manual_endpoint(
    db,
    *,
    config_text: str,
    ip_address: str | None = None,
    hostname: str = "",
    priority: int | None = None,
    current: datetime | None = None,
) -> VpnEndpoint:
    try:
        sanitized = sanitize_ovpn(config_text)
    except OvpnError as exc:
        raise ManualEndpointError(str(exc)) from exc

    ip = (
        normalize_public_ipv4(ip_address)
        or normalize_public_ipv4(sanitized.remote_host)
        or first_public_ipv4(config_text)
    )
    if not ip:
        raise ManualEndpointError("manual endpoint needs a public IPv4")

    stamp = current or now()
    row = db.scalar(select(VpnEndpoint).where(VpnEndpoint.ip_address == ip))
    if row is None:
        row = VpnEndpoint(
            ip_address=ip,
            hostname=(hostname or sanitized.remote_host or "")[:255],
            country="RU",
            provider="manual",
            source="manual",
            sources="manual",
            first_seen_at=stamp,
            created_at=stamp,
        )
        db.add(row)
    else:
        row.sources = merge_sources(row.sources, row.source, "manual")
        if hostname:
            row.hostname = hostname[:255]
        elif sanitized.remote_host and not row.hostname:
            row.hostname = sanitized.remote_host[:255]

    item = DiscoveredEndpoint(
        ip_address=ip,
        hostname=row.hostname or "",
        source="manual",
        sources=row.sources,
    )
    apply_sanitized_config(item, sanitized)
    if item.openvpn_udp_config:
        if item.udp_config_is_ip:
            if row.openvpn_udp_config and not row.udp_config_is_ip:
                row.openvpn_udp_ddns_config = row.openvpn_udp_ddns_config or row.openvpn_udp_config
            row.openvpn_udp_config = item.openvpn_udp_config
            row.openvpn_udp_port = item.openvpn_udp_port or row.openvpn_udp_port
            row.udp_config_is_ip = True
        else:
            row.openvpn_udp_ddns_config = item.openvpn_udp_ddns_config or item.openvpn_udp_config
            if not row.openvpn_udp_config:
                row.openvpn_udp_config = item.openvpn_udp_config
                row.udp_config_is_ip = False
            row.openvpn_udp_port = item.openvpn_udp_port or row.openvpn_udp_port
    if item.openvpn_tcp_config:
        if item.tcp_config_is_ip:
            if row.openvpn_tcp_config and not row.tcp_config_is_ip:
                row.openvpn_tcp_ddns_config = row.openvpn_tcp_ddns_config or row.openvpn_tcp_config
            row.openvpn_tcp_config = item.openvpn_tcp_config
            row.openvpn_tcp_port = item.openvpn_tcp_port or row.openvpn_tcp_port
            row.tcp_config_is_ip = True
        else:
            row.openvpn_tcp_ddns_config = item.openvpn_tcp_ddns_config or item.openvpn_tcp_config
            if not row.openvpn_tcp_config:
                row.openvpn_tcp_config = item.openvpn_tcp_config
                row.tcp_config_is_ip = False
            row.openvpn_tcp_port = item.openvpn_tcp_port or row.openvpn_tcp_port

    row.source = "manual"
    row.provider = row.provider or "manual"
    row.last_seen_at = stamp
    row.is_stale = False
    row.is_active = True
    row.is_available = row.has_usable_config()
    if priority is not None and str(priority).strip() != "":
        try:
            row.priority = int(priority)
        except (TypeError, ValueError) as exc:
            raise ManualEndpointError("priority must be an integer") from exc
    elif not row.priority:
        row.priority = int(getattr(settings, "vpn_manual_priority", 50) or 50)
    row.updated_at = stamp
    row.score = compute_score(row, stamp)
    db.commit()
    db.refresh(row)
    return row
