from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..config import settings
from ..db import SessionLocal
from ..models import VpnProfile
from .geo import GeoResult, lookup_egress
from .ovpn import OvpnError, sanitize_ovpn
from .profiles import mark_profile_failure, mark_profile_success
from .settings import gateway_base_url, proxy_url
from .status import explain_gateway_failure
from .wireguard import WireGuardError, sanitize_wireguard


@dataclass
class GatewayStatus:
    connected: bool = False
    available: bool = False
    vpn_type: str | None = None
    endpoint_ip: str | None = None
    detail: str = ""


@dataclass
class ConnectResult:
    ok: bool
    detail: str = ""
    geo: GeoResult | None = None
    latency_ms: int | None = None


def _int_setting(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sanitize_profile(profile: VpnProfile) -> str:
    if profile.vpn_type == "wireguard":
        return sanitize_wireguard(profile.config_text).text
    return sanitize_ovpn(profile.config_text).text


class VPNManager:
    async def check_connection(self) -> GatewayStatus:
        base = gateway_base_url()
        if not base:
            _label, detail = explain_gateway_failure("unconfigured")
            return GatewayStatus(detail=detail)
        try:
            timeout = httpx.Timeout(2.0, connect=1.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{base}/status")
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            _label, detail = explain_gateway_failure(exc)
            return GatewayStatus(detail=detail)
        return GatewayStatus(
            connected=bool(data.get("connected")),
            available=True,
            vpn_type=data.get("type") or data.get("protocol"),
            endpoint_ip=data.get("endpoint_ip"),
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

    async def connect(self, profile: VpnProfile) -> ConnectResult:
        try:
            cleaned = _sanitize_profile(profile)
        except (OvpnError, WireGuardError) as exc:
            return ConnectResult(False, str(exc))
        base = gateway_base_url()
        if not base:
            return ConnectResult(False, "gateway URL is empty")
        print(
            f"vkget: VPN profile selected: {profile.name} ({profile.vpn_type})",
            flush=True,
        )
        try:
            timeout = httpx.Timeout(
                _int_setting("vpn_connect_timeout", 15) + 5,
                connect=5.0,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{base}/connect",
                    json={
                        "type": profile.vpn_type,
                        "config": cleaned,
                    },
                )
        except Exception as exc:
            return ConnectResult(False, f"gateway error: {exc}")
        if response.status_code >= 400:
            detail = response.text[:300]
            try:
                detail = str(response.json().get("detail") or detail)
            except Exception:
                pass
            return ConnectResult(False, detail)
        print("vkget: VPN connected", flush=True)
        return ConnectResult(True, "connected")

    async def connect_and_probe(self, profile: VpnProfile) -> ConnectResult:
        started = time.monotonic()
        result = await self.connect(profile)
        if not result.ok:
            with SessionLocal() as db:
                mark_profile_failure(db, profile.id, result.detail)
            return result
        try:
            geo = await lookup_egress(proxy_url())
        except Exception as exc:
            await self.disconnect()
            detail = f"exit check failed: {exc}"
            with SessionLocal() as db:
                mark_profile_failure(db, profile.id, detail)
            return ConnectResult(False, detail)
        latency_ms = int((time.monotonic() - started) * 1000)
        with SessionLocal() as db:
            mark_profile_success(
                db,
                profile.id,
                exit_ip=geo.ip,
                country=geo.country,
                latency_ms=latency_ms,
            )
        return ConnectResult(True, "connected", geo=geo, latency_ms=latency_ms)


manager = VPNManager()
