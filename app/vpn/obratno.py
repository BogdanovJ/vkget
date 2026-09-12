from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from ..config import settings
from .gate import DiscoveredEndpoint, apply_sanitized_config
from .ovpn import OvpnError, sanitize_ovpn
from .util import first_public_ipv4, normalize_public_ipv4


HREF_RE = re.compile(r"""href\s*=\s*['"]([^'"]+\.ovpn(?:\?[^'"]*)?)['"]""", re.I)
HOSTNAME_RE = re.compile(r"\b(?:vpn[a-z0-9-]*\.opengw\.net|[a-z0-9.-]+\.opengw\.net)\b", re.I)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        href = attrs_dict.get("href", "")
        if href.lower().endswith(".ovpn") or ".ovpn?" in href.lower():
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text)))
            self._href = None
            self._text = []


def _better_ovpn_url(item: DiscoveredEndpoint, proto: str, url_is_ip: bool) -> bool:
    if not item.ovpn_url:
        return True
    if url_is_ip and not item.ovpn_url_is_ip:
        return True
    if url_is_ip == item.ovpn_url_is_ip and proto == "udp" and item.ovpn_url_proto != "udp":
        return True
    return False


def _classify_variant(url: str, text: str, nearby: str = "") -> tuple[str | None, bool]:
    blob = f"{url} {text} {nearby}".lower()
    is_ip = bool(
        re.search(r"\bip[\s_-]*udp\b", blob)
        or re.search(r"\bip[\s_-]*tcp\b", blob)
        or re.search(r"[\W_]ip[\W_].*(udp|tcp)", blob)
        or "/ip-" in blob
        or "-ip-" in blob
        or "_ip_" in blob
        or bool(first_public_ipv4(url))
    )
    if "udp" in blob and "tcp" not in blob:
        return "udp", is_ip
    if "tcp" in blob and "udp" not in blob:
        return "tcp", is_ip
    if "udp" in blob:
        return "udp", is_ip
    if "tcp" in blob:
        return "tcp", is_ip
    return None, is_ip


def parse_obratno_html(html: str, base_url: str | None = None) -> list[DiscoveredEndpoint]:
    base = base_url or settings.vpn_obratno_url
    parser = _LinkParser()
    try:
        parser.feed(html or "")
    except Exception:
        parser.links = []

    hrefs = list(parser.links)
    for match in HREF_RE.finditer(html or ""):
        href = match.group(1)
        if not any(existing == href for existing, _text in hrefs):
            hrefs.append((href, ""))

    found: dict[str, DiscoveredEndpoint] = {}
    for href, text in hrefs:
        absolute = urljoin(base, href)
        start = max((html or "").lower().find(href.lower()) - 180, 0)
        nearby = (html or "")[start:start + 360]
        proto, is_ip = _classify_variant(absolute, text, nearby)
        ip = (
            first_public_ipv4(absolute)
            or first_public_ipv4(text)
            or first_public_ipv4(nearby)
        )
        if not ip:
            continue
        hostname = ""
        host_match = HOSTNAME_RE.search(f"{absolute} {text} {nearby}")
        if host_match:
            hostname = host_match.group(0)
        item = found.get(ip)
        if item is None:
            item = DiscoveredEndpoint(
                ip_address=ip,
                hostname=hostname,
                country="RU",
                provider="vpngate",
                source="vpnobratno",
            )
            found[ip] = item
        elif hostname and not item.hostname:
            item.hostname = hostname
        url_is_ip = is_ip or bool(first_public_ipv4(absolute))
        if proto == "tcp":
            if item.openvpn_tcp_port is None:
                item.openvpn_tcp_port = 443
            item.tcp_config_is_ip = item.tcp_config_is_ip or url_is_ip
        else:
            if item.openvpn_udp_port is None:
                item.openvpn_udp_port = 1194
            item.udp_config_is_ip = item.udp_config_is_ip or url_is_ip
        variant = (absolute, proto or "udp", url_is_ip)
        if variant not in item.ovpn_urls:
            item.ovpn_urls.append(variant)
        if _better_ovpn_url(item, proto or "udp", url_is_ip):
            item.ovpn_url = absolute
            item.ovpn_url_proto = proto or "udp"
            item.ovpn_url_is_ip = url_is_ip
    return list(found.values())


async def fetch_obratno_html(url: str | None = None) -> str:
    target = url or settings.vpn_obratno_url
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            target,
            headers={"User-Agent": "vkget/1.0"},
        )
        response.raise_for_status()
        return response.text


async def fetch_ovpn_profile(url: str) -> str:
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={"User-Agent": "vkget/1.0"},
        )
        response.raise_for_status()
        return response.text


def endpoint_from_ovpn(
    config_text: str,
    *,
    source: str = "vpnobratno",
    fallback_ip: str | None = None,
) -> DiscoveredEndpoint:
    sanitized = sanitize_ovpn(config_text)
    ip = (
        normalize_public_ipv4(sanitized.remote_host)
        or fallback_ip
        or first_public_ipv4(config_text)
    )
    if not ip:
        raise OvpnError("OpenVPN configuration has no public IPv4")
    item = DiscoveredEndpoint(
        ip_address=ip,
        hostname="" if normalize_public_ipv4(sanitized.remote_host) else (sanitized.remote_host or ""),
        country="RU",
        provider="vpngate",
        source=source,
    )
    apply_sanitized_config(item, sanitized)
    return item


async def fetch_obratno_endpoints(url: str | None = None) -> list[DiscoveredEndpoint]:
    html = await fetch_obratno_html(url)
    return parse_obratno_html(html, url or settings.vpn_obratno_url)
