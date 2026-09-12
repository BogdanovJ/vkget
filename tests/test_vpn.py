from __future__ import annotations

import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import VpnEndpoint
from app.vpn.discovery import merge_discovered, persist_discovered
from app.vpn.fallback import vpn_eligible_failure, vpn_mode
from app.vpn.gate import DiscoveredEndpoint, parse_vpngate_csv
from app.vpn.geo import GeoResult, reset_host_egress_cache, verify_russia_exit
from app.vpn.manager import get_best_endpoint, mark_failure, pick_protocol
from app.vpn.obratno import parse_obratno_html
from app.vpn.ovpn import OvpnError, decode_vpngate_config, sanitize_ovpn
from app.vpn.runtime import download_with_vpn_fallback, try_vpn_download
from app.vpn.scoring import compute_score
from app.vpn.util import cooldown_for_failures as util_cooldown
from app.vpn.util import normalize_public_ipv4


VALID_OVPN = """client
dev tun
proto udp
remote 5.143.1.10 1194
nobind
persist-key
<ca>
-----BEGIN CERTIFICATE-----
MIIB
-----END CERTIFICATE-----
</ca>
"""

VALID_TCP_OVPN = """client
dev tun
proto tcp
remote 5.143.1.10 443
<ca>
-----BEGIN CERTIFICATE-----
MIIB
-----END CERTIFICATE-----
</ca>
"""

DANGEROUS_OVPN = """client
dev tun
proto udp
remote 5.143.1.10 1194
script-security 2
up /bin/true
down /bin/true
plugin evil.so
config /tmp/other.ovpn
<ca>
-----BEGIN CERTIFICATE-----
MIIB
-----END CERTIFICATE-----
</ca>
"""

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "vpn")


def encoded_ovpn(text: str = VALID_OVPN) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def sample_csv() -> str:
    ru = encoded_ovpn()
    jp = encoded_ovpn(VALID_OVPN.replace("5.143.1.10", "1.2.3.4"))
    return "\n".join(
        [
            "*vpn_servers",
            "#HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,NumVpnSessions,Uptime,TotalUsers,TotalTraffic,LogType,Operator,Message,OpenVPN_ConfigData_Base64",
            f"ru1,5.143.1.10,1000,40,162000000,Russian Federation,RU,12,86400000,1,1,2hour,op,,{ru}",
            f"jp1,1.2.3.4,900,10,1000000,Japan,JP,1,1,1,1,2hour,op,,{jp}",
            "ru_empty,8.8.4.4,1,1,1,Russian Federation,RU,1,1,1,1,2hour,op,,",
            "ru_private,10.0.0.9,1,1,1,Russian Federation,RU,1,1,1,1,2hour,op,," + ru,
            "*",
        ]
    )


class VpnCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    def add_endpoint(self, **kwargs) -> VpnEndpoint:
        values = {
            "ip_address": "5.143.1.10",
            "hostname": "vpn123.opengw.net",
            "source": "vpngate",
            "sources": "vpngate",
            "openvpn_udp_config": VALID_OVPN,
            "openvpn_udp_port": 1194,
            "udp_config_is_ip": True,
            "is_active": True,
            "is_available": True,
            "is_stale": False,
            "score": 50,
        }
        values.update(kwargs)
        with self.Session() as db:
            row = VpnEndpoint(**values)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row


class GateParseTests(unittest.TestCase):
    def test_parses_ru_rows_and_skips_others(self):
        rows = parse_vpngate_csv(sample_csv())
        self.assertEqual([row.ip_address for row in rows], ["5.143.1.10"])
        self.assertTrue(rows[0].openvpn_udp_config)
        self.assertEqual(rows[0].reported_speed_bps, 162000000)
        self.assertEqual(rows[0].reported_ping_ms, 40)
        self.assertEqual(rows[0].reported_sessions, 12)
        self.assertTrue(rows[0].udp_config_is_ip)

    def test_decodes_base64_config(self):
        text = decode_vpngate_config(encoded_ovpn())
        self.assertIn("remote 5.143.1.10 1194", text)
        self.assertIn("<ca>", text)


class ObratnoParseTests(unittest.TestCase):
    def test_parses_ovpn_links_and_variants(self):
        with open(os.path.join(FIXTURE_DIR, "obratno.html"), encoding="utf-8") as fh:
            html = fh.read()
        rows = parse_obratno_html(html, "https://vpnobratno.info/en/")
        by_ip = {row.ip_address: row for row in rows}
        self.assertIn("5.143.1.10", by_ip)
        self.assertIn("8.8.8.8", by_ip)
        self.assertNotIn("10.1.2.3", by_ip)
        self.assertTrue(by_ip["5.143.1.10"].ovpn_url_is_ip)
        self.assertIn("5.143.1.10", by_ip["5.143.1.10"].ovpn_url)
        self.assertEqual(by_ip["5.143.1.10"].hostname, "vpn123456.opengw.net")


class IpValidationTests(unittest.TestCase):
    def test_rejects_invalid_and_private_ips(self):
        self.assertIsNone(normalize_public_ipv4("not-an-ip"))
        self.assertIsNone(normalize_public_ipv4("10.0.0.1"))
        self.assertIsNone(normalize_public_ipv4("127.0.0.1"))
        self.assertIsNone(normalize_public_ipv4("192.168.1.5"))
        self.assertIsNone(normalize_public_ipv4("::1"))
        self.assertEqual(normalize_public_ipv4(" 8.8.8.8 "), "8.8.8.8")


class MergeTests(VpnCase):
    def test_duplicate_ip_merges_sources_and_keeps_stats(self):
        created = datetime(2026, 1, 1, 12, 0, 0)
        self.add_endpoint(
            successful_connections=4,
            failed_connections=1,
            first_seen_at=created,
            last_success_at=created,
        )
        gate = DiscoveredEndpoint(
            ip_address="5.143.1.10",
            hostname="vpn123.opengw.net",
            source="vpngate",
            openvpn_udp_config=VALID_OVPN,
            udp_config_is_ip=False,
            reported_speed_bps=1000,
        )
        extra = DiscoveredEndpoint(
            ip_address="5.143.1.10",
            source="vpnobratno",
            openvpn_udp_config=VALID_OVPN.replace("vpn123.opengw.net", "5.143.1.10"),
            udp_config_is_ip=True,
        )
        merged = merge_discovered(gate, extra)
        self.assertTrue(merged.udp_config_is_ip)
        with patch("app.vpn.discovery.SessionLocal", self.Session), patch(
            "app.vpn.discovery.settings"
        ) as fake_settings:
            fake_settings.vpn_stale_after_hours = 24
            fake_settings.vpn_disable_after_days = 7
            persist_discovered({"5.143.1.10": merged}, SimpleNamespace(
                found=1, added=0, updated=0, stale=0, inactive=0
            ))
        with self.Session() as db:
            row = db.scalar(select(VpnEndpoint))
            self.assertEqual(row.successful_connections, 4)
            self.assertIn("vpngate", row.sources)
            self.assertIn("vpnobratno", row.sources)
            self.assertTrue(row.udp_config_is_ip)


class OvpnSanitizeTests(unittest.TestCase):
    def test_rejects_dangerous_directives(self):
        with self.assertRaises(OvpnError) as ctx:
            sanitize_ovpn(DANGEROUS_OVPN)
        message = str(ctx.exception)
        self.assertIn("script-security", message)
        self.assertIn("plugin", message)
        self.assertIn("up", message)

    def test_keeps_safe_client_profile(self):
        cleaned = sanitize_ovpn(VALID_OVPN)
        self.assertIn("remote 5.143.1.10 1194", cleaned.text)
        self.assertIn("<ca>", cleaned.text)
        self.assertNotIn("script-security 2", cleaned.text)
        self.assertTrue(cleaned.uses_ip_remote)
        self.assertEqual(cleaned.proto, "udp")


class ScoringTests(unittest.TestCase):
    def test_prefers_healthy_udp_ip_endpoint(self):
        good = SimpleNamespace(
            openvpn_udp_config=VALID_OVPN,
            openvpn_tcp_config=None,
            udp_config_is_ip=True,
            tcp_config_is_ip=False,
            reported_speed_bps=162_000_000,
            reported_ping_ms=40,
            measured_latency_ms=None,
            reported_uptime_ms=7 * 24 * 60 * 60 * 1000,
            successful_connections=6,
            last_success_at=datetime.now(),
            last_failure_at=None,
            reported_sessions=3,
            consecutive_failures=0,
        )
        bad = SimpleNamespace(
            openvpn_udp_config=None,
            openvpn_tcp_config=VALID_TCP_OVPN,
            udp_config_is_ip=False,
            tcp_config_is_ip=False,
            reported_speed_bps=100_000,
            reported_ping_ms=400,
            measured_latency_ms=None,
            reported_uptime_ms=0,
            successful_connections=0,
            last_success_at=None,
            last_failure_at=datetime.now(),
            reported_sessions=40,
            consecutive_failures=5,
        )
        self.assertGreater(compute_score(good), compute_score(bad))


class CooldownTests(unittest.TestCase):
    def test_cooldown_steps(self):
        self.assertEqual(util_cooldown(1), timedelta(minutes=10))
        self.assertEqual(util_cooldown(2), timedelta(minutes=30))
        self.assertEqual(util_cooldown(3), timedelta(hours=2))
        self.assertEqual(util_cooldown(5), timedelta(hours=12))


class StaleTests(VpnCase):
    def test_missing_servers_age_then_reactivate(self):
        old = datetime.now() - timedelta(hours=30)
        ancient = datetime.now() - timedelta(days=8)
        self.add_endpoint(ip_address="5.143.1.10", last_seen_at=old)
        self.add_endpoint(
            ip_address="5.143.1.11",
            last_seen_at=ancient,
            openvpn_udp_config=VALID_OVPN,
        )
        with patch("app.vpn.discovery.SessionLocal", self.Session), patch(
            "app.vpn.discovery.settings"
        ) as fake_settings:
            fake_settings.vpn_stale_after_hours = 24
            fake_settings.vpn_disable_after_days = 7
            persist_discovered({}, SimpleNamespace(
                found=0, added=0, updated=0, stale=0, inactive=0
            ))
        with self.Session() as db:
            stale = db.scalar(select(VpnEndpoint).where(VpnEndpoint.ip_address == "5.143.1.10"))
            dead = db.scalar(select(VpnEndpoint).where(VpnEndpoint.ip_address == "5.143.1.11"))
            self.assertTrue(stale.is_stale)
            self.assertTrue(stale.is_active)
            self.assertTrue(dead.is_stale)
            self.assertFalse(dead.is_active)

        returning = DiscoveredEndpoint(
            ip_address="5.143.1.11",
            source="vpngate",
            openvpn_udp_config=VALID_OVPN,
            udp_config_is_ip=True,
        )
        with patch("app.vpn.discovery.SessionLocal", self.Session), patch(
            "app.vpn.discovery.settings"
        ) as fake_settings:
            fake_settings.vpn_stale_after_hours = 24
            fake_settings.vpn_disable_after_days = 7
            persist_discovered({"5.143.1.11": returning}, SimpleNamespace(
                found=1, added=0, updated=0, stale=0, inactive=0
            ))
        with self.Session() as db:
            row = db.scalar(select(VpnEndpoint).where(VpnEndpoint.ip_address == "5.143.1.11"))
            self.assertTrue(row.is_active)
            self.assertFalse(row.is_stale)


class RotationTests(VpnCase):
    def test_get_best_skips_cooldown_and_inactive(self):
        ready = self.add_endpoint(ip_address="5.143.1.10", score=20)
        self.add_endpoint(
            ip_address="5.143.2.10",
            score=90,
            cooldown_until=datetime.now() + timedelta(hours=1),
        )
        self.add_endpoint(ip_address="5.143.3.10", score=80, is_active=False)
        with self.Session() as db:
            best = get_best_endpoint(db)
            self.assertEqual(best.id, ready.id)

class IsolatedVpnTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotation_stops_after_max_attempts(self):
        endpoints = [
            SimpleNamespace(id=1, ip_address="5.143.1.10", score=30),
            SimpleNamespace(id=2, ip_address="5.143.1.11", score=20),
            SimpleNamespace(id=3, ip_address="5.143.1.12", score=10),
            SimpleNamespace(id=4, ip_address="5.143.1.13", score=1),
        ]

        async def fail_connect(endpoint):
            return None

        with patch("app.vpn.runtime.get_best_endpoint", side_effect=endpoints), patch(
            "app.vpn.runtime.manager.connect_and_verify", side_effect=fail_connect
        ) as connect, patch(
            "app.vpn.runtime.manager.disconnect", new=AsyncMock()
        ), patch("app.vpn.runtime.settings") as fake_settings:
            fake_settings.vpn_max_endpoint_attempts = 3
            result = await try_vpn_download(AsyncMock(), "https://vk.com/video-1_2", "ch")
        self.assertTrue(result.attempted)
        self.assertFalse(result.succeeded)
        self.assertEqual(connect.await_count, 3)
        self.assertEqual(result.endpoint_ids, [1, 2, 3])

    async def test_verify_russia_pass_and_fail(self):
        reset_host_egress_cache()

        async def ru(_proxy, _timeout):
            return GeoResult(ip="5.143.1.10", country="RU", provider="mock")

        async def us(_proxy, _timeout):
            return GeoResult(ip="1.1.1.1", country="US", provider="mock")

        async def same(_proxy, _timeout):
            return GeoResult(ip="9.9.9.9", country="RU", provider="mock")

        host = GeoResult(ip="9.9.9.9", country="DE", provider="mock")
        other_host = GeoResult(ip="4.4.4.4", country="DE", provider="mock")

        with patch("app.vpn.geo.lookup_host_egress", AsyncMock(return_value=other_host)):
            ok = await verify_russia_exit("socks5://vpn:1080", providers=(ru,))
            self.assertEqual(ok.country, "RU")
        reset_host_egress_cache()
        with patch("app.vpn.geo.lookup_host_egress", AsyncMock(return_value=other_host)):
            with self.assertRaises(RuntimeError):
                await verify_russia_exit("socks5://vpn:1080", providers=(us,))
        reset_host_egress_cache()
        with patch("app.vpn.geo.lookup_host_egress", AsyncMock(return_value=host)):
            with self.assertRaises(RuntimeError):
                await verify_russia_exit("socks5://vpn:1080", providers=(same,))

    async def test_fallback_eligibility_and_modes(self):
        self.assertTrue(vpn_eligible_failure("HTTP Error 403: Forbidden"))
        self.assertTrue(vpn_eligible_failure("HTTP Error 451"))
        self.assertTrue(vpn_eligible_failure("okcdn fragment timed out"))
        self.assertTrue(vpn_eligible_failure("VPN_SLOW_RATE: too slow"))
        self.assertFalse(vpn_eligible_failure("Unsupported URL"))
        self.assertFalse(vpn_eligible_failure("this video has been deleted"))
        self.assertFalse(vpn_eligible_failure("No space left on device"))
        self.assertFalse(vpn_eligible_failure("malformed url"))

        calls: list[str] = []

        async def download(url, channel, proxy=None):
            calls.append(proxy or "direct")
            if proxy:
                return 0, "/tmp/ok.mp4", "vpn ok"
            return 1, None, "HTTP Error 403: Forbidden"

        with patch("app.vpn.runtime.try_vpn_download", new=AsyncMock(return_value=SimpleNamespace(
            attempted=True, succeeded=True, rc=0, path="/tmp/ok.mp4", log="vpn ok"
        ))) as vpn_try, patch("app.vpn.fallback.settings") as fake_settings:
            fake_settings.vk_vpn_mode = "off"
            self.assertEqual(vpn_mode(), "off")
            rc, path, log = await download_with_vpn_fallback(download, "https://vk.com/video-1_2", "ch")
            self.assertEqual(rc, 1)
            self.assertEqual(calls, ["direct"])
            vpn_try.assert_not_called()

            fake_settings.vk_vpn_mode = "auto"
            calls.clear()
            rc, path, log = await download_with_vpn_fallback(download, "https://vk.com/video-1_2", "ch")
            self.assertEqual(rc, 0)
            self.assertEqual(path, "/tmp/ok.mp4")
            vpn_try.assert_called()

            fake_settings.vk_vpn_mode = "always"
            calls.clear()
            vpn_try.reset_mock()
            rc, path, log = await download_with_vpn_fallback(download, "https://vk.com/video-1_2", "ch")
            self.assertEqual(rc, 0)
            vpn_try.assert_called()

            fake_settings.vk_vpn_mode = "always"
            vpn_try.return_value = SimpleNamespace(
                attempted=False, succeeded=False, rc=1, path=None, log=""
            )
            async def direct_ok(url, channel, proxy=None):
                return 0, "/tmp/direct.mp4", "ok"
            rc, path, log = await download_with_vpn_fallback(direct_ok, "https://vk.com/video-1_2", "ch")
            self.assertEqual(path, "/tmp/direct.mp4")


class ProtocolPickTests(unittest.TestCase):
    def test_prefers_ip_udp_then_udp_then_ip_tcp_then_tcp(self):
        row = VpnEndpoint(
            ip_address="5.143.1.10",
            openvpn_udp_config=VALID_OVPN,
            openvpn_tcp_config=VALID_TCP_OVPN,
            udp_config_is_ip=True,
            tcp_config_is_ip=True,
        )
        self.assertEqual(pick_protocol(row).name, "udp")
        row.udp_config_is_ip = False
        self.assertEqual(pick_protocol(row).name, "udp")
        row.openvpn_udp_config = None
        self.assertEqual(pick_protocol(row).name, "tcp")


class MarkFailureTests(VpnCase):
    def test_mark_failure_sets_cooldown(self):
        row = self.add_endpoint()
        with patch("app.vpn.manager.SessionLocal", self.Session):
            mark_failure(row.id, "timeout")
        with self.Session() as db:
            updated = db.get(VpnEndpoint, row.id)
            self.assertEqual(updated.consecutive_failures, 1)
            self.assertFalse(updated.is_available)
            self.assertIsNotNone(updated.cooldown_until)


class YtdlpProxyTests(unittest.TestCase):
    def test_common_args_adds_proxy(self):
        from app.ytdlp import common_args, parse_ytdlp_speed_bps

        args, cleanup = common_args(proxy="socks5://vpn-gateway:1080")
        self.assertIn("--proxy", args)
        self.assertIn("socks5://vpn-gateway:1080", args)
        if cleanup:
            os.unlink(cleanup)
        self.assertEqual(
            parse_ytdlp_speed_bps("[download]  12.3% of 1.23GiB at 120.00KiB/s ETA 02:15"),
            120 * 1024,
        )


if __name__ == "__main__":
    unittest.main()
