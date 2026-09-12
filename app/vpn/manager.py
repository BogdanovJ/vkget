from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import VpnEndpoint
from .geo import GeoResult, verify_russia_exit
from .ovpn import OvpnError, sanitize_ovpn
from .scoring import compute_score
from .util import cooldown_for_failures, now


@dataclass
class ProtocolChoice:
    name: str
    config: str
    port: int | None
    uses_ip: bool


@dataclass
class GatewayStatus:
    connected: bool = False
    available: bool = False
    endpoint_ip: str | None = None
    protocol: str | None = None
    detail: str = ""


def _int_setting(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def gateway_base_url() -> str:
    return str(getattr(settings, "vpn_gateway_url", "") or "").rstrip("/")


def proxy_url() -> str:
    return str(getattr(settings, "vpn_proxy_url", "") or "").strip()


def pick_protocol(endpoint: VpnEndpoint) -> ProtocolChoice | None:
    udp = endpoint.openvpn_udp_config
    tcp = endpoint.openvpn_tcp_config
    if udp and endpoint.udp_config_is_ip:
        return ProtocolChoice("udp", udp, endpoint.openvpn_udp_port, True)
    if udp:
        return ProtocolChoice("udp", udp, endpoint.openvpn_udp_port, False)
    if tcp and endpoint.tcp_config_is_ip:
        return ProtocolChoice("tcp", tcp, endpoint.openvpn_tcp_port, True)
    if tcp:
        return ProtocolChoice("tcp", tcp, endpoint.openvpn_tcp_port, False)
    return None


def endpoint_is_candidate(endpoint: VpnEndpoint, current: datetime | None = None) -> bool:
    when = current or now()
    if not endpoint.is_active or endpoint.is_stale:
        return False
    if not endpoint.has_usable_config():
        return False
    if endpoint.cooldown_until and endpoint.cooldown_until > when:
        return False
    return True


def list_candidate_endpoints(
    db,
    *,
    exclude_ids: set[int] | None = None,
    current: datetime | None = None,
) -> list[VpnEndpoint]:
    when = current or now()
    skip = exclude_ids or set()
    rows = list(db.scalars(select(VpnEndpoint)).all())
    usable = [
        row
        for row in rows
        if row.id not in skip and endpoint_is_candidate(row, when)
    ]
    usable.sort(key=lambda row: row.score, reverse=True)
    return usable


def get_best_endpoint(
    db=None,
    *,
    exclude_ids: set[int] | None = None,
    current: datetime | None = None,
) -> VpnEndpoint | None:
    if db is not None:
        rows = list_candidate_endpoints(db, exclude_ids=exclude_ids, current=current)
        return rows[0] if rows else None
    with SessionLocal() as owned:
        rows = list_candidate_endpoints(owned, exclude_ids=exclude_ids, current=current)
        return rows[0] if rows else None


def mark_success(endpoint_id: int, geo: GeoResult | None = None) -> None:
    with SessionLocal() as db:
        row = db.get(VpnEndpoint, endpoint_id)
        if not row:
            return
        stamp = now()
        row.last_success_at = stamp
        row.last_checked_at = stamp
        row.consecutive_failures = 0
        row.successful_connections = (row.successful_connections or 0) + 1
        row.is_available = True
        row.failure_reason = None
        row.cooldown_until = None
        if geo:
            row.verified_country = geo.country
            row.last_verified_at = stamp
        row.score = compute_score(row, stamp)
        row.updated_at = stamp
        db.commit()


def mark_failure(endpoint_id: int, reason: str) -> None:
    with SessionLocal() as db:
        row = db.get(VpnEndpoint, endpoint_id)
        if not row:
            return
        stamp = now()
        row.last_failure_at = stamp
        row.last_checked_at = stamp
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.failed_connections = (row.failed_connections or 0) + 1
        row.is_available = False
        row.failure_reason = (reason or "failed")[:1000]
        row.cooldown_until = stamp + cooldown_for_failures(row.consecutive_failures)
        row.score = compute_score(row, stamp)
        row.updated_at = stamp
        db.commit()
        print(
            f"vkget: VPN endpoint failed: {row.ip_address} {row.failure_reason}",
            flush=True,
        )


def verification_is_fresh(endpoint: VpnEndpoint, current: datetime | None = None) -> bool:
    if endpoint.verified_country != "RU" or not endpoint.last_verified_at:
        return False
    minutes = _int_setting("vpn_verify_cache_minutes", 30)
    return (current or now()) - endpoint.last_verified_at <= timedelta(minutes=max(minutes, 1))


class VPNManager:
    async def check_connection(self) -> GatewayStatus:
        base = gateway_base_url()
        if not base:
            return GatewayStatus(detail="gateway URL is not configured")
        try:
            timeout = httpx.Timeout(2.0, connect=1.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{base}/status")
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            return GatewayStatus(detail=str(exc))
        return GatewayStatus(
            connected=bool(data.get("connected")),
            available=True,
            endpoint_ip=data.get("endpoint_ip"),
            protocol=data.get("protocol"),
            detail=str(data.get("detail") or ""),
        )

    async def disconnect(self) -> None:
        base = gateway_base_url()
        if not base:
            return
        try:
            timeout = httpx.Timeout(5.0, connect=2.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                await client.post(f"{base}/disconnect")
        except Exception as exc:
            print(f"vkget: VPN disconnect failed: {exc}", flush=True)

    async def connect(self, endpoint: VpnEndpoint) -> ProtocolChoice | None:
        choice = pick_protocol(endpoint)
        if not choice:
            mark_failure(endpoint.id, "no usable OpenVPN config")
            return None
        try:
            sanitized = sanitize_ovpn(choice.config)
        except OvpnError as exc:
            mark_failure(endpoint.id, f"invalid OpenVPN config: {exc}")
            return None

        base = gateway_base_url()
        if not base:
            print("vkget: VPN gateway URL is empty", flush=True)
            return None

        print(
            f"vkget: VPN connecting: {endpoint.ip_address} protocol={choice.name}",
            flush=True,
        )
        try:
            timeout = httpx.Timeout(
                _int_setting("vpn_connect_timeout", 20) + 5,
                connect=5.0,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{base}/connect",
                    json={
                        "config": sanitized.text,
                        "protocol": choice.name,
                        "endpoint_ip": endpoint.ip_address,
                        "username": getattr(settings, "vpn_username", "vpn"),
                        "password": getattr(settings, "vpn_password", "vpn"),
                    },
                )
        except Exception as exc:
            mark_failure(endpoint.id, f"gateway error: {exc}")
            return None
        if response.status_code >= 400:
            detail = response.text[:300]
            mark_failure(endpoint.id, f"gateway rejected connect: {detail}")
            return None
        print("vkget: VPN connected", flush=True)
        return choice

    async def connect_and_verify(self, endpoint: VpnEndpoint) -> GeoResult | None:
        choice = await self.connect(endpoint)
        if not choice:
            return None
        if verification_is_fresh(endpoint):
            print(
                f"vkget: VPN external IP verified: country=RU (cached) {endpoint.ip_address}",
                flush=True,
            )
            return GeoResult(
                ip=endpoint.ip_address,
                country="RU",
                provider="cache",
            )
        proxy = proxy_url()
        try:
            geo = await verify_russia_exit(proxy)
        except Exception as exc:
            await self.disconnect()
            mark_failure(endpoint.id, f"Russia verification failed: {exc}")
            return None
        print(
            f"vkget: VPN external IP verified: country={geo.country} ip={geo.ip}",
            flush=True,
        )
        return geo


manager = VPNManager()
