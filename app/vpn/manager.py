from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import VpnEndpoint
from .geo import GeoResult, verify_russia_exit
from .ovpn import OvpnError, parse_remote, sanitize_ovpn
from .scoring import compute_score
from .util import cooldown_for_failures, now


VARIANT_ORDER = ("ip_udp", "udp", "ip_tcp", "tcp")


@dataclass
class ProtocolChoice:
    name: str
    config: str
    port: int | None
    uses_ip: bool
    variant: str = ""
    remote_host: str | None = None


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


def _variant_slots(endpoint: VpnEndpoint) -> dict[str, tuple[str | None, int | None, bool]]:
    udp_ip = endpoint.openvpn_udp_config if endpoint.udp_config_is_ip else None
    udp_ddns = endpoint.openvpn_udp_ddns_config
    if not udp_ddns and endpoint.openvpn_udp_config and not endpoint.udp_config_is_ip:
        udp_ddns = endpoint.openvpn_udp_config
    tcp_ip = endpoint.openvpn_tcp_config if endpoint.tcp_config_is_ip else None
    tcp_ddns = endpoint.openvpn_tcp_ddns_config
    if not tcp_ddns and endpoint.openvpn_tcp_config and not endpoint.tcp_config_is_ip:
        tcp_ddns = endpoint.openvpn_tcp_config
    return {
        "ip_udp": (udp_ip, endpoint.openvpn_udp_port, True),
        "udp": (udp_ddns, endpoint.openvpn_udp_port, False),
        "ip_tcp": (tcp_ip, endpoint.openvpn_tcp_port, True),
        "tcp": (tcp_ddns, endpoint.openvpn_tcp_port, False),
    }


def list_variants(endpoint: VpnEndpoint) -> list[ProtocolChoice]:
    choices: list[ProtocolChoice] = []
    seen: set[str] = set()
    for variant in VARIANT_ORDER:
        config, port, uses_ip = _variant_slots(endpoint)[variant]
        if not config or config in seen:
            continue
        seen.add(config)
        host, parsed_port, proto = parse_remote(config)
        name = "tcp" if variant.endswith("tcp") else "udp"
        if proto in {"udp", "tcp"}:
            name = proto
        choices.append(
            ProtocolChoice(
                name=name,
                variant=variant,
                config=config,
                port=parsed_port or port,
                uses_ip=uses_ip,
                remote_host=host,
            )
        )
    last_failed = getattr(endpoint, "last_failed_variant", None)
    if last_failed and len(choices) > 1:
        choices = [item for item in choices if item.variant != last_failed] + [
            item for item in choices if item.variant == last_failed
        ]
    return choices


def pick_protocol(endpoint: VpnEndpoint) -> ProtocolChoice | None:
    choices = list_variants(endpoint)
    return choices[0] if choices else None


def endpoint_is_candidate(endpoint: VpnEndpoint, current: datetime | None = None) -> bool:
    when = current or now()
    if not endpoint.is_active:
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


def classify_connect_reason(detail: str | None) -> str:
    text = (detail or "").lower()
    if "timed out" in text or "timeout" in text:
        return "connect_timeout"
    if "verif" in text or "country" in text:
        return "verification_failure"
    if "exited" in text or "openvpn" in text:
        return "openvpn_error"
    if "tcp" in text and ("unreach" in text or "refused" in text or "probe" in text):
        return "tcp_unreachable"
    if "gateway" in text:
        return "gateway_error"
    return "connect_error"


def _log_fields(title: str, **fields) -> None:
    print(f"vkget: {title}", flush=True)
    for key, value in fields.items():
        if value is None or value == "":
            continue
        print(f"  {key}={value}", flush=True)


def _choice_remote(choice: ProtocolChoice) -> str | None:
    if choice.remote_host and choice.port:
        return f"{choice.remote_host}:{choice.port}"
    if choice.remote_host:
        return choice.remote_host
    return None


def mark_success(
    endpoint_id: int,
    geo: GeoResult | None = None,
    variant: str | None = None,
) -> None:
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
        if variant:
            row.last_good_variant = variant
        if geo:
            row.verified_country = geo.country
            row.last_verified_at = stamp
        row.score = compute_score(row, stamp)
        row.updated_at = stamp
        db.commit()


def mark_verified(
    endpoint_id: int,
    geo: GeoResult | None = None,
    variant: str | None = None,
) -> None:
    with SessionLocal() as db:
        row = db.get(VpnEndpoint, endpoint_id)
        if not row:
            return
        stamp = now()
        row.last_success_at = stamp
        row.last_checked_at = stamp
        row.consecutive_failures = 0
        row.is_available = True
        row.failure_reason = None
        row.cooldown_until = None
        if variant:
            row.last_good_variant = variant
        if geo:
            row.verified_country = geo.country
            row.last_verified_at = stamp
        row.score = compute_score(row, stamp)
        row.updated_at = stamp
        db.commit()


def mark_failure(endpoint_id: int, reason: str, variant: str | None = None) -> None:
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
        if variant:
            row.last_failed_variant = variant
        row.score = compute_score(row, stamp)
        row.updated_at = stamp
        db.commit()
        print(
            f"vkget: VPN endpoint failed: {row.ip_address} {row.failure_reason}",
            flush=True,
        )


def record_variant_failure(endpoint_id: int, variant: str, reason: str) -> None:
    with SessionLocal() as db:
        row = db.get(VpnEndpoint, endpoint_id)
        if not row:
            return
        row.last_failed_variant = variant
        row.last_checked_at = now()
        row.failure_reason = (reason or "variant failed")[:1000]
        row.updated_at = row.last_checked_at
        db.commit()


def verification_is_fresh(endpoint: VpnEndpoint, current: datetime | None = None) -> bool:
    if endpoint.verified_country != "RU" or not endpoint.last_verified_at:
        return False
    minutes = _int_setting("vpn_verify_cache_minutes", 30)
    return (current or now()) - endpoint.last_verified_at <= timedelta(minutes=max(minutes, 1))


async def probe_tcp(host: str, port: int, timeout: float) -> bool:
    if not host or not port or timeout <= 0:
        return True
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)),
            timeout=timeout,
        )
    except Exception:
        return False
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
    return True


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

    async def _open_tunnel(
        self,
        endpoint: VpnEndpoint,
        choice: ProtocolChoice,
    ) -> tuple[bool, str]:
        try:
            sanitized = sanitize_ovpn(choice.config)
        except OvpnError as exc:
            return False, f"invalid OpenVPN config: {exc}"

        base = gateway_base_url()
        if not base:
            return False, "gateway URL is empty"

        try:
            timeout = httpx.Timeout(
                _int_setting("vpn_connect_timeout", 8) + 5,
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
            return False, f"gateway error: {exc}"
        if response.status_code >= 400:
            detail = response.text[:300]
            try:
                detail = str(response.json().get("detail") or detail)
            except Exception:
                pass
            return False, detail
        return True, "connected"

    async def connect(self, endpoint: VpnEndpoint) -> ProtocolChoice | None:
        choice, _reason = await self._connect_variants(endpoint, verify=False)
        return choice

    async def connect_and_verify(self, endpoint: VpnEndpoint) -> GeoResult | None:
        _choice, geo = await self._connect_variants(endpoint, verify=True)
        return geo

    async def _connect_variants(
        self,
        endpoint: VpnEndpoint,
        *,
        verify: bool,
    ) -> tuple[ProtocolChoice | None, GeoResult | None]:
        choices = list_variants(endpoint)
        if not choices:
            mark_failure(endpoint.id, "no usable OpenVPN config")
            return None, None

        print(
            f"vkget: VPN candidate selected: {endpoint.ip_address} score={endpoint.score}",
            flush=True,
        )
        last_reason = "all variants failed"
        last_variant = None
        probe_timeout = max(_int_setting("vpn_tcp_probe_timeout", 2), 0)

        for choice in choices:
            last_variant = choice.variant
            _log_fields(
                "VPN trying:",
                endpoint=endpoint.ip_address,
                variant=choice.variant,
                remote=_choice_remote(choice),
            )
            started = time.monotonic()
            if choice.name == "tcp" and choice.remote_host and choice.port and probe_timeout:
                reachable = await probe_tcp(
                    choice.remote_host,
                    choice.port,
                    probe_timeout,
                )
                if not reachable:
                    duration = time.monotonic() - started
                    last_reason = "tcp_unreachable"
                    _log_fields(
                        "VPN variant failed:",
                        endpoint=endpoint.ip_address,
                        variant=choice.variant,
                        reason=last_reason,
                        duration=f"{duration:.1f}s",
                    )
                    record_variant_failure(endpoint.id, choice.variant, last_reason)
                    continue

            ok, detail = await self._open_tunnel(endpoint, choice)
            duration = time.monotonic() - started
            if not ok:
                last_reason = classify_connect_reason(detail)
                _log_fields(
                    "VPN variant failed:",
                    endpoint=endpoint.ip_address,
                    variant=choice.variant,
                    reason=last_reason,
                    duration=f"{duration:.1f}s",
                )
                record_variant_failure(endpoint.id, choice.variant, last_reason)
                await self.disconnect()
                continue

            _log_fields(
                "VPN connected:",
                endpoint=endpoint.ip_address,
                variant=choice.variant,
            )
            if not verify:
                return choice, None
            if verification_is_fresh(endpoint):
                geo = GeoResult(
                    ip=endpoint.ip_address,
                    country="RU",
                    provider="cache",
                )
                _log_fields(
                    "VPN external IP verified:",
                    ip=geo.ip,
                    country=geo.country,
                )
                mark_verified(endpoint.id, geo, choice.variant)
                return choice, geo
            try:
                geo = await verify_russia_exit(proxy_url())
            except Exception as exc:
                await self.disconnect()
                mark_failure(
                    endpoint.id,
                    f"Russia verification failed: {exc}",
                    choice.variant,
                )
                return None, None
            _log_fields(
                "VPN external IP verified:",
                ip=geo.ip,
                country=geo.country,
            )
            mark_verified(endpoint.id, geo, choice.variant)
            return choice, geo

        mark_failure(endpoint.id, last_reason, last_variant)
        return None, None


manager = VPNManager()
