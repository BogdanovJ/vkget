from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..config import settings


@dataclass
class GeoResult:
    ip: str
    country: str
    provider: str


_HOST_CACHE: tuple[float, GeoResult] | None = None
HOST_CACHE_TTL = 600


async def _fetch_json(url: str, proxy: str | None, timeout: float) -> dict:
    kwargs: dict = {
        "timeout": httpx.Timeout(timeout, connect=min(timeout, 5.0)),
        "follow_redirects": True,
        "headers": {"User-Agent": "vkget/1.0"},
    }
    if proxy:
        kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**kwargs) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _ip_api(proxy: str | None, timeout: float) -> GeoResult:
    data = await _fetch_json(
        "http://ip-api.com/json/?fields=status,countryCode,query",
        proxy,
        timeout,
    )
    if str(data.get("status") or "").lower() == "fail":
        raise RuntimeError("ip-api lookup failed")
    ip = str(data.get("query") or "").strip()
    country = str(data.get("countryCode") or "").strip().upper()
    if not ip or not country:
        raise RuntimeError("ip-api returned incomplete geo data")
    return GeoResult(ip=ip, country=country, provider="ip-api")


async def _ifconfig_co(proxy: str | None, timeout: float) -> GeoResult:
    data = await _fetch_json("https://ifconfig.co/json", proxy, timeout)
    ip = str(data.get("ip") or "").strip()
    country = str(data.get("country_iso") or data.get("country") or "").strip().upper()
    if len(country) > 2:
        country = country[:2]
    if not ip or not country:
        raise RuntimeError("ifconfig.co returned incomplete geo data")
    return GeoResult(ip=ip, country=country, provider="ifconfig.co")


GEO_PROVIDERS = (_ip_api, _ifconfig_co)


async def lookup_egress(
    proxy: str | None = None,
    timeout: float | None = None,
    providers=GEO_PROVIDERS,
) -> GeoResult:
    limit = timeout
    if limit is None:
        value = getattr(settings, "vpn_verify_timeout", 10)
        limit = float(value) if isinstance(value, (int, float)) else 10.0
    errors: list[str] = []
    for provider in providers:
        try:
            return await provider(proxy, limit)
        except Exception as exc:
            errors.append(f"{getattr(provider, '__name__', provider)}: {exc}")
    raise RuntimeError("geolocation lookup failed: " + "; ".join(errors))


async def lookup_host_egress(timeout: float | None = None) -> GeoResult:
    global _HOST_CACHE
    cached = _HOST_CACHE
    if cached and time.time() - cached[0] < HOST_CACHE_TTL:
        return cached[1]
    result = await lookup_egress(proxy=None, timeout=timeout)
    _HOST_CACHE = (time.time(), result)
    return result


def reset_host_egress_cache() -> None:
    global _HOST_CACHE
    _HOST_CACHE = None


async def verify_russia_exit(
    proxy: str,
    timeout: float | None = None,
    providers=GEO_PROVIDERS,
) -> GeoResult:
    vpn = await lookup_egress(proxy=proxy, timeout=timeout, providers=providers)
    if vpn.country != "RU":
        raise RuntimeError(f"VPN exit country is {vpn.country}, expected RU")
    try:
        host = await lookup_host_egress(timeout=timeout)
    except Exception:
        host = None
    if host and host.ip == vpn.ip:
        raise RuntimeError("VPN exit IP matches the normal host IP")
    return vpn
