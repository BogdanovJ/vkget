from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

import httpx

from ..config import settings
from .ovpn import OvpnError, decode_and_sanitize
from .util import normalize_public_ipv4, parse_int


@dataclass
class DiscoveredEndpoint:
    ip_address: str
    hostname: str = ""
    country: str = "RU"
    provider: str = "vpngate"
    source: str = "vpngate"
    openvpn_udp_port: int | None = None
    openvpn_tcp_port: int | None = None
    openvpn_udp_config: str | None = None
    openvpn_tcp_config: str | None = None
    openvpn_udp_ddns_config: str | None = None
    openvpn_tcp_ddns_config: str | None = None
    udp_config_is_ip: bool = False
    tcp_config_is_ip: bool = False
    reported_speed_bps: int | None = None
    reported_ping_ms: int | None = None
    reported_sessions: int | None = None
    reported_uptime_ms: int | None = None
    ovpn_url: str | None = None
    ovpn_url_is_ip: bool = False
    ovpn_url_proto: str | None = None
    ovpn_urls: list = field(default_factory=list)
    sources: str = ""

    def __post_init__(self):
        if not self.sources:
            self.sources = self.source


def _row_get(row: dict[str, str], *names: str) -> str:
    lower = {key.lstrip("#").strip().lower(): value for key, value in row.items()}
    for name in names:
        value = lower.get(name.lower())
        if value not in (None, ""):
            return str(value)
    return ""


def parse_vpngate_csv(text: str) -> list[DiscoveredEndpoint]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line == "*vpn_servers" or line == "*":
            continue
        lines.append(line)
    if not lines:
        return []

    header = lines[0].lstrip("#")
    reader = csv.DictReader(io.StringIO("\n".join([header, *lines[1:]])))
    found: list[DiscoveredEndpoint] = []
    for row in reader:
        country = _row_get(row, "CountryShort").upper()
        if country != "RU":
            continue
        ip = normalize_public_ipv4(_row_get(row, "IP"))
        if not ip:
            continue
        encoded = _row_get(row, "OpenVPN_ConfigData_Base64")
        if not encoded:
            continue
        try:
            sanitized = decode_and_sanitize(encoded)
        except OvpnError:
            continue

        item = DiscoveredEndpoint(
            ip_address=ip,
            hostname=_row_get(row, "HostName", "hostname"),
            country="RU",
            provider="vpngate",
            source="vpngate",
            reported_speed_bps=parse_int(_row_get(row, "Speed")),
            reported_ping_ms=parse_int(_row_get(row, "Ping")),
            reported_sessions=parse_int(_row_get(row, "NumVpnSessions")),
            reported_uptime_ms=parse_int(_row_get(row, "Uptime")),
        )
        apply_sanitized_config(item, sanitized)
        if item.openvpn_udp_config or item.openvpn_tcp_config:
            found.append(item)
    return found


def apply_sanitized_config(item: DiscoveredEndpoint, sanitized) -> None:
    proto = sanitized.proto or "udp"
    uses_ip = sanitized.uses_ip_remote
    if proto == "tcp":
        if uses_ip:
            if item.openvpn_tcp_config and not item.tcp_config_is_ip:
                item.openvpn_tcp_ddns_config = item.openvpn_tcp_config
            item.openvpn_tcp_config = sanitized.text
            item.tcp_config_is_ip = True
            item.openvpn_tcp_port = sanitized.remote_port or item.openvpn_tcp_port
        else:
            item.openvpn_tcp_ddns_config = sanitized.text
            if not item.openvpn_tcp_config:
                item.openvpn_tcp_config = sanitized.text
                item.tcp_config_is_ip = False
            if not item.openvpn_tcp_port:
                item.openvpn_tcp_port = sanitized.remote_port
        return
    if uses_ip:
        if item.openvpn_udp_config and not item.udp_config_is_ip:
            item.openvpn_udp_ddns_config = item.openvpn_udp_config
        item.openvpn_udp_config = sanitized.text
        item.udp_config_is_ip = True
        item.openvpn_udp_port = sanitized.remote_port or item.openvpn_udp_port
        return
    item.openvpn_udp_ddns_config = sanitized.text
    if not item.openvpn_udp_config:
        item.openvpn_udp_config = sanitized.text
        item.udp_config_is_ip = False
    if not item.openvpn_udp_port:
        item.openvpn_udp_port = sanitized.remote_port


async def fetch_vpngate_csv(url: str | None = None) -> str:
    target = url or settings.vpn_gate_csv_url
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            target,
            headers={"User-Agent": "vkget/1.0"},
        )
        response.raise_for_status()
        return response.text


async def fetch_vpngate_endpoints(url: str | None = None) -> list[DiscoveredEndpoint]:
    return parse_vpngate_csv(await fetch_vpngate_csv(url))
