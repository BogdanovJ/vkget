from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import AppState, VpnEndpoint
from .gate import DiscoveredEndpoint, apply_sanitized_config, fetch_vpngate_endpoints
from .obratno import fetch_obratno_endpoints, fetch_ovpn_profile
from .ovpn import OvpnError, sanitize_ovpn
from .scoring import compute_score, is_manual
from .util import merge_sources, now, preferred_source


DISCOVERY_STATE_KEY = "vpn_last_discovery_at"


@dataclass
class DiscoveryStats:
    found: int = 0
    added: int = 0
    updated: int = 0
    stale: int = 0
    inactive: int = 0
    gate_ok: bool = False
    obratno_ok: bool = False
    gate_current: int = 0
    obratno_current: int = 0
    unique_current: int = 0
    historical_eligible: int = 0
    candidate_pool: int = 0
    known_good: int = 0
    cooldown: int = 0


def _vpn_discovery_enabled() -> bool:
    value = getattr(settings, "vpn_discovery_enabled", True)
    return value is True


def _interval_minutes() -> int:
    value = getattr(settings, "vpn_discovery_interval_minutes", 30)
    if not isinstance(value, int):
        return 30
    return max(value, 1)


def discovery_is_due(db, current: datetime | None = None) -> bool:
    if not _vpn_discovery_enabled():
        return False
    row = db.get(AppState, DISCOVERY_STATE_KEY)
    if not row or not row.value:
        return True
    try:
        last = datetime.fromisoformat(row.value)
    except ValueError:
        return True
    return (current or now()) >= last + timedelta(minutes=_interval_minutes())


def mark_discovery_ran(db, current: datetime | None = None) -> None:
    stamp = (current or now()).isoformat()
    row = db.get(AppState, DISCOVERY_STATE_KEY)
    if row:
        row.value = stamp
    else:
        db.add(AppState(key=DISCOVERY_STATE_KEY, value=stamp))


def merge_discovered(
    base: DiscoveredEndpoint,
    extra: DiscoveredEndpoint,
) -> DiscoveredEndpoint:
    base.sources = merge_sources(base.source, base.sources, extra.source, extra.sources)
    base.source = preferred_source(base.sources)
    if extra.hostname and (not base.hostname or extra.udp_config_is_ip or extra.tcp_config_is_ip):
        if extra.hostname and not base.hostname:
            base.hostname = extra.hostname
    if extra.hostname and not base.hostname:
        base.hostname = extra.hostname
    if extra.reported_speed_bps and not base.reported_speed_bps:
        base.reported_speed_bps = extra.reported_speed_bps
    if extra.reported_ping_ms and not base.reported_ping_ms:
        base.reported_ping_ms = extra.reported_ping_ms
    if extra.reported_sessions is not None and base.reported_sessions is None:
        base.reported_sessions = extra.reported_sessions
    if extra.reported_uptime_ms and not base.reported_uptime_ms:
        base.reported_uptime_ms = extra.reported_uptime_ms

    if extra.openvpn_udp_config:
        if extra.udp_config_is_ip:
            if base.openvpn_udp_config and not base.udp_config_is_ip:
                base.openvpn_udp_ddns_config = base.openvpn_udp_ddns_config or base.openvpn_udp_config
            base.openvpn_udp_config = extra.openvpn_udp_config
            base.openvpn_udp_port = extra.openvpn_udp_port or base.openvpn_udp_port
            base.udp_config_is_ip = True
        elif not base.openvpn_udp_config:
            base.openvpn_udp_config = extra.openvpn_udp_config
            base.openvpn_udp_port = extra.openvpn_udp_port
            base.udp_config_is_ip = False
    if extra.openvpn_udp_ddns_config:
        base.openvpn_udp_ddns_config = extra.openvpn_udp_ddns_config
    if extra.openvpn_tcp_config:
        if extra.tcp_config_is_ip:
            if base.openvpn_tcp_config and not base.tcp_config_is_ip:
                base.openvpn_tcp_ddns_config = base.openvpn_tcp_ddns_config or base.openvpn_tcp_config
            base.openvpn_tcp_config = extra.openvpn_tcp_config
            base.openvpn_tcp_port = extra.openvpn_tcp_port or base.openvpn_tcp_port
            base.tcp_config_is_ip = True
        elif not base.openvpn_tcp_config:
            base.openvpn_tcp_config = extra.openvpn_tcp_config
            base.openvpn_tcp_port = extra.openvpn_tcp_port
            base.tcp_config_is_ip = False
    if extra.openvpn_tcp_ddns_config:
        base.openvpn_tcp_ddns_config = extra.openvpn_tcp_ddns_config
    if extra.ovpn_urls:
        for variant in extra.ovpn_urls:
            if variant not in base.ovpn_urls:
                base.ovpn_urls.append(variant)

    if extra.ovpn_url and (
        extra.ovpn_url_is_ip
        or not base.ovpn_url
        or (extra.ovpn_url_proto == "udp" and not base.openvpn_udp_config)
    ):
        if extra.ovpn_url_is_ip or not base.ovpn_url:
            base.ovpn_url = extra.ovpn_url
            base.ovpn_url_is_ip = extra.ovpn_url_is_ip
            base.ovpn_url_proto = extra.ovpn_url_proto
    return base


def _apply_item_to_row(row: VpnEndpoint, item: DiscoveredEndpoint, seen_at: datetime) -> None:
    sources = merge_sources(row.sources, row.source, item.source, item.sources)
    row.sources = sources
    if is_manual(row):
        row.source = "manual"
    elif item.source == "vpnobratno" and (item.udp_config_is_ip or item.tcp_config_is_ip):
        row.source = "vpnobratno"
    else:
        row.source = preferred_source(sources)
    if item.hostname:
        row.hostname = item.hostname[:255]
    row.country = item.country or row.country or "RU"
    row.provider = item.provider or row.provider or "vpngate"
    if item.reported_speed_bps is not None:
        row.reported_speed_bps = item.reported_speed_bps
    if item.reported_ping_ms is not None:
        row.reported_ping_ms = item.reported_ping_ms
    if item.reported_sessions is not None:
        row.reported_sessions = item.reported_sessions
    if item.reported_uptime_ms is not None:
        row.reported_uptime_ms = item.reported_uptime_ms
    if item.openvpn_udp_config:
        if item.udp_config_is_ip:
            if row.openvpn_udp_config and not row.udp_config_is_ip:
                row.openvpn_udp_ddns_config = row.openvpn_udp_ddns_config or row.openvpn_udp_config
            row.openvpn_udp_config = item.openvpn_udp_config
            row.openvpn_udp_port = item.openvpn_udp_port or row.openvpn_udp_port
            row.udp_config_is_ip = True
        elif not row.openvpn_udp_config:
            row.openvpn_udp_config = item.openvpn_udp_config
            row.openvpn_udp_port = item.openvpn_udp_port
            row.udp_config_is_ip = False
    if item.openvpn_udp_ddns_config:
        row.openvpn_udp_ddns_config = item.openvpn_udp_ddns_config
    elif item.openvpn_udp_port and not row.openvpn_udp_port:
        row.openvpn_udp_port = item.openvpn_udp_port
    if item.openvpn_tcp_config:
        if item.tcp_config_is_ip:
            if row.openvpn_tcp_config and not row.tcp_config_is_ip:
                row.openvpn_tcp_ddns_config = row.openvpn_tcp_ddns_config or row.openvpn_tcp_config
            row.openvpn_tcp_config = item.openvpn_tcp_config
            row.openvpn_tcp_port = item.openvpn_tcp_port or row.openvpn_tcp_port
            row.tcp_config_is_ip = True
        elif not row.openvpn_tcp_config:
            row.openvpn_tcp_config = item.openvpn_tcp_config
            row.openvpn_tcp_port = item.openvpn_tcp_port
            row.tcp_config_is_ip = False
    if item.openvpn_tcp_ddns_config:
        row.openvpn_tcp_ddns_config = item.openvpn_tcp_ddns_config
    elif item.openvpn_tcp_port and not row.openvpn_tcp_port:
        row.openvpn_tcp_port = item.openvpn_tcp_port
    row.last_seen_at = seen_at
    row.is_stale = False
    row.is_active = True
    if row.has_usable_config():
        row.is_available = True
    row.updated_at = seen_at
    row.score = compute_score(row, seen_at)


def _row_from_item(item: DiscoveredEndpoint, seen_at: datetime) -> VpnEndpoint:
    row = VpnEndpoint(
        ip_address=item.ip_address,
        hostname=(item.hostname or "")[:255],
        country=item.country or "RU",
        provider=item.provider or "vpngate",
        source=item.source,
        sources=item.sources or item.source,
        openvpn_udp_port=item.openvpn_udp_port,
        openvpn_tcp_port=item.openvpn_tcp_port,
        openvpn_udp_config=item.openvpn_udp_config,
        openvpn_tcp_config=item.openvpn_tcp_config,
        openvpn_udp_ddns_config=item.openvpn_udp_ddns_config,
        openvpn_tcp_ddns_config=item.openvpn_tcp_ddns_config,
        udp_config_is_ip=item.udp_config_is_ip,
        tcp_config_is_ip=item.tcp_config_is_ip,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        is_active=True,
        is_available=bool(item.openvpn_udp_config or item.openvpn_tcp_config),
        is_stale=False,
        reported_speed_bps=item.reported_speed_bps,
        reported_ping_ms=item.reported_ping_ms,
        reported_sessions=item.reported_sessions,
        reported_uptime_ms=item.reported_uptime_ms,
        created_at=seen_at,
        updated_at=seen_at,
    )
    row.score = compute_score(row, seen_at)
    return row


def _slot_filled(
    item: DiscoveredEndpoint,
    existing: VpnEndpoint | None,
    proto: str,
    is_ip: bool,
) -> bool:
    if proto == "tcp":
        if is_ip:
            return bool(item.tcp_config_is_ip or (existing and existing.tcp_config_is_ip))
        return bool(
            item.openvpn_tcp_ddns_config
            or (item.openvpn_tcp_config and not item.tcp_config_is_ip)
            or (existing and (existing.openvpn_tcp_ddns_config or (
                existing.openvpn_tcp_config and not existing.tcp_config_is_ip
            )))
        )
    if is_ip:
        return bool(item.udp_config_is_ip or (existing and existing.udp_config_is_ip))
    return bool(
        item.openvpn_udp_ddns_config
        or (item.openvpn_udp_config and not item.udp_config_is_ip)
        or (existing and (existing.openvpn_udp_ddns_config or (
            existing.openvpn_udp_config and not existing.udp_config_is_ip
        )))
    )


def _obratno_urls(item: DiscoveredEndpoint) -> list[tuple[str, str, bool]]:
    urls = list(item.ovpn_urls or [])
    if item.ovpn_url:
        fallback = (item.ovpn_url, item.ovpn_url_proto or "udp", bool(item.ovpn_url_is_ip))
        if fallback not in urls:
            urls.insert(0, fallback)
    return urls


async def _fill_obratno_configs(
    items: list[DiscoveredEndpoint],
    existing_by_ip: dict[str, VpnEndpoint],
) -> None:
    limit = getattr(settings, "vpn_obratno_fetch_limit", 20)
    if not isinstance(limit, int):
        limit = 20
    fetched = 0
    for item in items:
        existing = existing_by_ip.get(item.ip_address)
        for url, proto, is_ip in _obratno_urls(item):
            if fetched >= max(limit, 0):
                return
            if not url or _slot_filled(item, existing, proto, is_ip):
                continue
            try:
                raw = await fetch_ovpn_profile(url)
                sanitized = sanitize_ovpn(raw)
            except (OvpnError, httpx.HTTPError) as exc:
                print(
                    f"vkget: VPN discovery: Obratno ovpn skipped for {item.ip_address}: {exc}",
                    flush=True,
                )
                continue
            apply_sanitized_config(item, sanitized)
            fetched += 1


async def collect_discovered() -> tuple[dict[str, DiscoveredEndpoint], DiscoveryStats]:
    stats = DiscoveryStats()
    discovered: dict[str, DiscoveredEndpoint] = {}

    try:
        gate_items = await fetch_vpngate_endpoints()
        stats.gate_current = len(gate_items)
        for item in gate_items:
            current = discovered.get(item.ip_address)
            discovered[item.ip_address] = merge_discovered(current, item) if current else item
        stats.gate_ok = True
    except Exception as exc:
        print(f"vkget: VPN discovery: VPN Gate failed: {exc}", flush=True)

    try:
        obratno_items = await fetch_obratno_endpoints()
        stats.obratno_current = len(obratno_items)
        stats.obratno_ok = True
        with SessionLocal() as db:
            existing = {
                row.ip_address: row
                for row in db.scalars(select(VpnEndpoint)).all()
            }
        await _fill_obratno_configs(obratno_items, existing)
        for item in obratno_items:
            current = discovered.get(item.ip_address)
            if current:
                merge_discovered(current, item)
            elif item.openvpn_udp_config or item.openvpn_tcp_config:
                discovered[item.ip_address] = item
            else:
                # Keep the IP in the catalogue only if a later merge added a config.
                discovered.setdefault(item.ip_address, item)
    except Exception as exc:
        print(f"vkget: VPN discovery: VPN Obratno failed: {exc}", flush=True)

    usable = {
        ip: item
        for ip, item in discovered.items()
        if item.openvpn_udp_config or item.openvpn_tcp_config
    }
    stats.found = len(usable)
    stats.unique_current = len(usable)
    return usable, stats


def persist_discovered(
    discovered: dict[str, DiscoveredEndpoint],
    stats: DiscoveryStats,
    current: datetime | None = None,
) -> DiscoveryStats:
    seen_at = current or now()
    try:
        hours = int(getattr(settings, "vpn_stale_after_hours", 24))
        stale_after = timedelta(hours=max(hours, 1))
    except (TypeError, ValueError):
        stale_after = timedelta(hours=24)
    try:
        days = int(getattr(settings, "vpn_disable_after_days", 7))
        disable_after = timedelta(days=max(days, 1))
    except (TypeError, ValueError):
        disable_after = timedelta(days=7)

    with SessionLocal() as db:
        existing_rows = list(db.scalars(select(VpnEndpoint)).all())
        existing = {row.ip_address: row for row in existing_rows}
        seen_ips = set(discovered)

        for ip, item in discovered.items():
            row = existing.get(ip)
            if row is None:
                db.add(_row_from_item(item, seen_at))
                stats.added += 1
            else:
                _apply_item_to_row(row, item, seen_at)
                stats.updated += 1

        for row in existing_rows:
            if row.ip_address in seen_ips:
                continue
            if is_manual(row):
                row.score = compute_score(row, seen_at)
                row.updated_at = seen_at
                continue
            last_seen = row.last_seen_at or row.created_at or seen_at
            missing = seen_at - last_seen
            if missing > disable_after:
                row.is_active = False
                row.is_stale = True
                stats.inactive += 1
            elif missing > stale_after:
                row.is_stale = True
                stats.stale += 1
            row.score = compute_score(row, seen_at)
            row.updated_at = seen_at

        mark_discovery_ran(db, seen_at)
        db.commit()
        _attach_pool_stats(stats, db, seen_at)
    return stats


def _attach_pool_stats(stats: DiscoveryStats, db, current: datetime) -> None:
    from .status import compute_pool_stats

    pool = compute_pool_stats(db, current)
    stats.historical_eligible = pool["historical_eligible"]
    stats.candidate_pool = pool["candidate_pool"]
    stats.known_good = pool["known_good"]
    stats.cooldown = pool["cooldown"]
    stats.inactive = pool["inactive"]


async def refresh_vpn_catalogue() -> DiscoveryStats:
    discovered, stats = await collect_discovered()
    persist_discovered(discovered, stats)
    print("vkget: VPN discovery:", flush=True)
    print(f"  VPN Gate current RU: {stats.gate_current}", flush=True)
    print(f"  VPN Obratno current RU: {stats.obratno_current}", flush=True)
    print(f"  unique current: {stats.unique_current}", flush=True)
    print(f"  historical eligible: {stats.historical_eligible}", flush=True)
    print(f"  candidate pool: {stats.candidate_pool}", flush=True)
    print(f"  inactive: {stats.inactive}", flush=True)
    if stats.added:
        print(f"vkget: VPN discovery: {stats.added} new endpoints added", flush=True)
    return stats


async def maybe_refresh_vpn_catalogue(force: bool = False) -> DiscoveryStats | None:
    if not _vpn_discovery_enabled() and not force:
        return None
    try:
        with SessionLocal() as db:
            due = force or discovery_is_due(db)
        if not due:
            return None
        return await refresh_vpn_catalogue()
    except Exception as exc:
        print(f"vkget: VPN discovery failed: {exc}", flush=True)
        return None
