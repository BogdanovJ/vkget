from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import VpnProfile
from app.vpn.ovpn import OvpnError, sanitize_ovpn
from app.vpn.profiles import create_profile, delete_profile, update_profile
from app.vpn.runtime import download_with_vpn
from app.vpn.settings import is_vpn_enabled, selected_profile, set_vpn_enabled
from app.vpn.wireguard import WireGuardError, sanitize_wireguard


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

WG_PRIV = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
WG_PUB = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
VALID_WG = f"""[Interface]
PrivateKey = {WG_PRIV}
Address = 10.0.0.2/32
[Peer]
PublicKey = {WG_PUB}
Endpoint = 185.10.20.30:51820
AllowedIPs = 0.0.0.0/0
"""


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

    def add_profile(self, **kwargs) -> VpnProfile:
        values = {
            "name": "Russian VPS",
            "vpn_type": "openvpn",
            "config_text": VALID_OVPN,
            "enabled": True,
            "is_default": False,
            "fallback_to_direct": True,
        }
        values.update(kwargs)
        with self.Session() as db:
            if values.get("is_default"):
                return create_profile(
                    db,
                    name=values["name"],
                    vpn_type=values["vpn_type"],
                    config_text=values["config_text"],
                    enabled=values["enabled"],
                    is_default=True,
                    fallback_to_direct=values["fallback_to_direct"],
                )
            row = VpnProfile(**values)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row


class SettingsTests(VpnCase):
    def test_env_default_is_off(self):
        with self.Session() as db:
            self.assertFalse(is_vpn_enabled(db))

    def test_runtime_switch_overrides_env(self):
        with self.Session() as db:
            set_vpn_enabled(db, True)
            self.assertTrue(is_vpn_enabled(db))
            set_vpn_enabled(db, False)
            self.assertFalse(is_vpn_enabled(db))


class ProfileTests(VpnCase):
    def test_create_openvpn_and_wireguard(self):
        with self.Session() as db:
            ovpn = create_profile(db, name="OVPN", vpn_type="openvpn", config_text=VALID_OVPN)
            wg = create_profile(db, name="WG", vpn_type="wireguard", config_text=VALID_WG)
            self.assertEqual(ovpn.vpn_type, "openvpn")
            self.assertEqual(wg.vpn_type, "wireguard")
            self.assertTrue(ovpn.is_default)
            self.assertFalse(wg.is_default)

    def test_edit_and_delete(self):
        with self.Session() as db:
            row = create_profile(db, name="One", vpn_type="openvpn", config_text=VALID_OVPN)
            update_profile(db, row, name="Two", fallback_to_direct=False)
            db.refresh(row)
            self.assertEqual(row.name, "Two")
            self.assertFalse(row.fallback_to_direct)
            delete_profile(db, row)
            self.assertIsNone(db.get(VpnProfile, row.id))

    def test_only_one_selected_profile(self):
        with self.Session() as db:
            first = create_profile(db, name="A", vpn_type="openvpn", config_text=VALID_OVPN)
            second = create_profile(
                db,
                name="B",
                vpn_type="wireguard",
                config_text=VALID_WG,
                is_default=True,
            )
            self.assertTrue(db.get(VpnProfile, second.id).is_default)
            self.assertFalse(db.get(VpnProfile, first.id).is_default)
            self.assertEqual(selected_profile(db).id, second.id)

    def test_disabled_profile_is_not_selected(self):
        with self.Session() as db:
            row = create_profile(db, name="Off", vpn_type="openvpn", config_text=VALID_OVPN)
            update_profile(db, row, enabled=False)
            self.assertIsNone(selected_profile(db))


class ValidationTests(unittest.TestCase):
    def test_valid_openvpn_accepted(self):
        cleaned = sanitize_ovpn(VALID_OVPN)
        self.assertIn("remote 5.143.1.10 1194", cleaned.text)

    def test_dangerous_openvpn_rejected(self):
        with self.assertRaises(OvpnError) as ctx:
            sanitize_ovpn(DANGEROUS_OVPN)
        message = str(ctx.exception)
        self.assertIn("script-security", message)
        self.assertIn("plugin", message)

    def test_malformed_openvpn_rejected(self):
        with self.assertRaises(OvpnError):
            sanitize_ovpn("this is not a vpn config")

    def test_valid_wireguard_accepted(self):
        cleaned = sanitize_wireguard(VALID_WG)
        self.assertIn("[Peer]", cleaned.text)
        self.assertIn("185.10.20.30:51820", cleaned.endpoint)

    def test_malformed_wireguard_rejected(self):
        with self.assertRaises(WireGuardError) as ctx:
            sanitize_wireguard("[Interface]\nPrivateKey = nope\n")
        self.assertIn("[Peer]", str(ctx.exception))
        self.assertNotIn("ValueError", str(ctx.exception))


class DownloadFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_never_contacts_gateway_or_proxy(self):
        calls: list[str] = []

        async def download(url, channel, proxy=None):
            calls.append(proxy or "direct")
            return 0, "/tmp/ok.mp4", "ok"

        with patch("app.vpn.runtime.is_vpn_enabled", return_value=False), patch(
            "app.vpn.runtime.manager.connect", new=AsyncMock()
        ) as connect:
            rc, path, log = await download_with_vpn(download, "https://vk.com/video-1_2", "ch")
        self.assertEqual(rc, 0)
        self.assertEqual(path, "/tmp/ok.mp4")
        self.assertEqual(calls, ["direct"])
        connect.assert_not_called()

    async def test_enabled_uses_proxy_when_connect_works(self):
        calls: list[str] = []
        profile = SimpleNamespace(
            id=1,
            name="Russian VPS",
            vpn_type="wireguard",
            enabled=True,
            fallback_to_direct=True,
        )

        async def download(url, channel, proxy=None):
            calls.append(proxy or "direct")
            return 0, "/tmp/vpn.mp4", "vpn ok"

        with patch("app.vpn.runtime.is_vpn_enabled", return_value=True), patch(
            "app.vpn.runtime.selected_profile", return_value=profile
        ), patch("app.vpn.runtime.fallback_to_direct", return_value=True), patch(
            "app.vpn.runtime.SessionLocal", _session_with(profile)
        ), patch(
            "app.vpn.runtime.manager.connect",
            new=AsyncMock(return_value=SimpleNamespace(ok=True, detail="connected")),
        ), patch(
            "app.vpn.runtime.manager.disconnect", new=AsyncMock()
        ), patch(
            "app.vpn.runtime.proxy_url", return_value="socks5://vpn-gateway:1080"
        ), patch("app.vpn.runtime.mark_profile_success"):
            rc, path, log = await download_with_vpn(download, "https://vk.com/video-1_2", "ch")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["socks5://vpn-gateway:1080"])

    async def test_connect_failure_falls_back(self):
        calls: list[str] = []
        profile = SimpleNamespace(
            id=1,
            name="Russian VPS",
            vpn_type="openvpn",
            enabled=True,
            fallback_to_direct=True,
        )

        async def download(url, channel, proxy=None):
            calls.append(proxy or "direct")
            return 0, "/tmp/direct.mp4", "direct"

        with patch("app.vpn.runtime.is_vpn_enabled", return_value=True), patch(
            "app.vpn.runtime.selected_profile", return_value=profile
        ), patch("app.vpn.runtime.fallback_to_direct", return_value=True), patch(
            "app.vpn.runtime.SessionLocal", _session_with(profile)
        ), patch(
            "app.vpn.runtime.manager.connect",
            new=AsyncMock(return_value=SimpleNamespace(ok=False, detail="handshake timeout")),
        ), patch("app.vpn.runtime.mark_profile_failure"):
            rc, path, log = await download_with_vpn(download, "https://vk.com/video-1_2", "ch")
        self.assertEqual(path, "/tmp/direct.mp4")
        self.assertEqual(calls, ["direct"])

    async def test_vpn_download_failure_disconnects_then_direct(self):
        calls: list[str] = []
        profile = SimpleNamespace(
            id=1,
            name="Russian VPS",
            vpn_type="openvpn",
            enabled=True,
            fallback_to_direct=True,
        )

        async def download(url, channel, proxy=None):
            calls.append(proxy or "direct")
            if proxy:
                return 1, None, "proxy connection lost"
            return 0, "/tmp/direct.mp4", "direct"

        disconnect = AsyncMock()
        with patch("app.vpn.runtime.is_vpn_enabled", return_value=True), patch(
            "app.vpn.runtime.selected_profile", return_value=profile
        ), patch("app.vpn.runtime.fallback_to_direct", return_value=True), patch(
            "app.vpn.runtime.SessionLocal", _session_with(profile)
        ), patch(
            "app.vpn.runtime.manager.connect",
            new=AsyncMock(return_value=SimpleNamespace(ok=True, detail="connected")),
        ), patch("app.vpn.runtime.manager.disconnect", new=disconnect), patch(
            "app.vpn.runtime.proxy_url", return_value="socks5://vpn-gateway:1080"
        ), patch("app.vpn.runtime.mark_profile_failure"):
            rc, path, log = await download_with_vpn(download, "https://vk.com/video-1_2", "ch")
        self.assertEqual(path, "/tmp/direct.mp4")
        self.assertEqual(calls, ["socks5://vpn-gateway:1080", "direct"])
        disconnect.assert_awaited()

    async def test_failure_without_fallback_stays_failed(self):
        profile = SimpleNamespace(
            id=1,
            name="Russian VPS",
            vpn_type="openvpn",
            enabled=True,
            fallback_to_direct=False,
        )

        async def download(url, channel, proxy=None):
            raise AssertionError("direct path must not run")

        with patch("app.vpn.runtime.is_vpn_enabled", return_value=True), patch(
            "app.vpn.runtime.selected_profile", return_value=profile
        ), patch("app.vpn.runtime.fallback_to_direct", return_value=False), patch(
            "app.vpn.runtime.SessionLocal", _session_with(profile)
        ), patch(
            "app.vpn.runtime.manager.connect",
            new=AsyncMock(return_value=SimpleNamespace(ok=False, detail="down")),
        ), patch("app.vpn.runtime.mark_profile_failure"):
            rc, path, log = await download_with_vpn(download, "https://vk.com/video-1_2", "ch")
        self.assertEqual(rc, 1)
        self.assertIsNone(path)
        self.assertIn("down", log)

    async def test_no_profile_falls_back(self):
        calls: list[str] = []

        async def download(url, channel, proxy=None):
            calls.append(proxy or "direct")
            return 0, "/tmp/direct.mp4", "ok"

        with patch("app.vpn.runtime.is_vpn_enabled", return_value=True), patch(
            "app.vpn.runtime.selected_profile", return_value=None
        ), patch("app.vpn.runtime.fallback_to_direct", return_value=True), patch(
            "app.vpn.runtime.SessionLocal", _session_with(None)
        ):
            rc, path, log = await download_with_vpn(download, "https://vk.com/video-1_2", "ch")
        self.assertEqual(calls, ["direct"])
        self.assertEqual(path, "/tmp/direct.mp4")


def _session_with(profile):
    class _CM:
        def __enter__(self):
            return SimpleNamespace(get=lambda _cls, _id: profile)

        def __exit__(self, *args):
            return False

    return lambda: _CM()


class YtdlpProxyTests(unittest.TestCase):
    def test_common_args_adds_proxy_only_when_asked(self):
        from app.ytdlp import common_args

        args, cleanup = common_args()
        self.assertNotIn("--proxy", args)
        if cleanup:
            os.unlink(cleanup)
        args, cleanup = common_args(proxy="socks5://vpn-gateway:1080")
        self.assertIn("--proxy", args)
        self.assertIn("socks5://vpn-gateway:1080", args)
        if cleanup:
            os.unlink(cleanup)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "vpn-gateway")
        )
        if gateway_dir not in sys.path:
            sys.path.insert(0, gateway_dir)

    def test_broken_pipe_is_swallowed(self):
        from server import write_http_body

        def boom(_body):
            raise BrokenPipeError(32, "Broken pipe")

        self.assertFalse(write_http_body(boom, b"{}"))

        def reset(_body):
            raise ConnectionResetError(104, "Connection reset")

        self.assertFalse(write_http_body(reset, b"{}"))
        written = []
        self.assertTrue(write_http_body(written.append, b"ok"))

    def test_connect_rejects_unknown_type(self):
        from server import connect

        code, body = connect({"type": "l2tp", "config": "x"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_openvpn_and_wireguard_cleanup_on_failure(self):
        from server import _connect_openvpn, _connect_wireguard

        code, body = _connect_openvpn({"config": "not-a-config"})
        self.assertEqual(code, 400)

        with patch("server.subprocess.run") as run:
            run.side_effect = OSError("no wg-quick")
            code, body = _connect_wireguard({"config": VALID_WG})
        self.assertEqual(code, 502)
        self.assertIn("WireGuard failed", body["detail"])


if __name__ == "__main__":
    unittest.main()
