from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ytdlp import (
    _netscape_cookie_line,
    download_url_candidates,
    label_from_url,
    looks_like_bot_protection,
    mirror_url,
    normalize_vk_url,
    scan_url_candidates,
    to_vk_com,
    to_vkvideo,
)


class UrlConversionTests(unittest.TestCase):
    def test_normalize_keeps_vkvideo_host(self):
        self.assertEqual(
            normalize_vk_url("  https://vkvideo.ru/playlist/-123_456  "),
            "https://vkvideo.ru/playlist/-123_456",
        )

    def test_normalize_keeps_vk_com_host(self):
        self.assertEqual(
            normalize_vk_url("https://vk.com/playlist/-123_456"),
            "https://vk.com/playlist/-123_456",
        )

    def test_normalize_upgrades_http_and_strips_slash(self):
        self.assertEqual(
            normalize_vk_url("http://vkvideo.ru/@channel/"),
            "https://vkvideo.ru/@channel",
        )

    def test_normalize_adds_scheme(self):
        self.assertEqual(
            normalize_vk_url("vkvideo.ru/playlist/1"),
            "https://vkvideo.ru/playlist/1",
        )

    def test_to_vk_com_from_vkvideo(self):
        self.assertEqual(
            to_vk_com("https://vkvideo.ru/video-123_456"),
            "https://vk.com/video-123_456",
        )

    def test_to_vkvideo_from_vk_com(self):
        self.assertEqual(
            to_vkvideo("https://vk.com/playlist/-123_456"),
            "https://vkvideo.ru/playlist/-123_456",
        )

    def test_www_and_m_hosts(self):
        self.assertEqual(
            to_vkvideo("https://www.vk.com/@foo"),
            "https://vkvideo.ru/@foo",
        )
        self.assertEqual(
            to_vk_com("https://m.vk.com/video1"),
            "https://vk.com/video1",
        )
        self.assertEqual(
            to_vk_com("https://www.vkvideo.ru/playlist/9"),
            "https://vk.com/playlist/9",
        )

    def test_mirror_url_swaps_hosts(self):
        self.assertEqual(
            mirror_url("https://vk.com/playlist/1"),
            "https://vkvideo.ru/playlist/1",
        )
        self.assertEqual(
            mirror_url("https://vkvideo.ru/video-1_2"),
            "https://vk.com/video-1_2",
        )

    def test_scan_prefers_vkvideo_including_stored_vk_com(self):
        self.assertEqual(
            scan_url_candidates("https://vk.com/playlist/-1_2"),
            [
                "https://vkvideo.ru/playlist/-1_2",
                "https://vk.com/playlist/-1_2",
            ],
        )
        self.assertEqual(
            scan_url_candidates("https://vkvideo.ru/@channel"),
            [
                "https://vkvideo.ru/@channel",
                "https://vk.com/@channel",
            ],
        )

    def test_download_prefers_vk_com(self):
        self.assertEqual(
            download_url_candidates("https://vkvideo.ru/video-1_2"),
            [
                "https://vk.com/video-1_2",
                "https://vkvideo.ru/video-1_2",
            ],
        )
        self.assertEqual(
            download_url_candidates("https://vk.com/video-1_2"),
            [
                "https://vk.com/video-1_2",
                "https://vkvideo.ru/video-1_2",
            ],
        )

    def test_label_from_url_works_for_either_host(self):
        self.assertEqual(label_from_url("https://vkvideo.ru/@shows"), "@shows")
        self.assertEqual(label_from_url("https://vk.com/@shows"), "@shows")
        self.assertEqual(
            label_from_url("https://vk.com/playlist/-123_456"),
            "-123_456",
        )
        self.assertEqual(
            label_from_url("https://vkvideo.ru/playlist/-123_456"),
            "-123_456",
        )

    def test_non_vk_urls_are_left_alone(self):
        url = "https://example.com/watch?v=1"
        self.assertEqual(to_vk_com(url), url)
        self.assertEqual(to_vkvideo(url), url)
        self.assertEqual(scan_url_candidates(url), [url])
        self.assertEqual(download_url_candidates(url), [url])


class BotProtectionAndCookieTests(unittest.TestCase):
    def test_challenge_errors_are_detected(self):
        self.assertTrue(looks_like_bot_protection("HTTP Error 403: Forbidden"))
        self.assertTrue(looks_like_bot_protection("Just a moment... Cloudflare"))
        self.assertTrue(looks_like_bot_protection("cf-ray: abc checking your browser"))
        self.assertFalse(looks_like_bot_protection("Video unavailable"))
        self.assertFalse(looks_like_bot_protection(""))

    def test_netscape_cookie_line(self):
        line = _netscape_cookie_line(
            {
                "name": "cf_clearance",
                "value": "abc",
                "domain": ".vk.com",
                "path": "/",
                "secure": True,
                "expiry": 1700000000,
            }
        )
        self.assertEqual(
            line,
            ".vk.com\tTRUE\t/\tTRUE\t1700000000\tcf_clearance\tabc",
        )

    def test_session_cookie_expiry(self):
        line = _netscape_cookie_line(
            {
                "name": "sid",
                "value": "1",
                "domain": "vkvideo.ru",
                "expires": -1,
            }
        )
        self.assertEqual(line, "vkvideo.ru\tFALSE\t/\tFALSE\t0\tsid\t1")

    def test_cookie_merge_keeps_httponly_lines(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.vk.com\tTRUE\t/\tTRUE\t1\tremixsid\tabc\n"
                ".vk.com\tTRUE\t/\tFALSE\t1\tremixlang\t0\n"
            )
            path = handle.name
        cookie_path = None
        try:
            with patch("app.ytdlp.settings") as fake_settings:
                fake_settings.cookie_file = path
                from app.ytdlp import _write_cookie_file

                cookie_path, cleanup = _write_cookie_file(
                    [{"name": "cf_clearance", "value": "tok", "domain": ".vk.com"}]
                )
            self.assertTrue(cleanup)
            with open(cookie_path, encoding="utf-8") as merged:
                text = merged.read()
            self.assertIn("#HttpOnly_.vk.com\tTRUE\t/\tTRUE\t1\tremixsid\tabc", text)
            self.assertIn(".vk.com\tTRUE\t/\tFALSE\t1\tremixlang\t0", text)
            self.assertIn(".vk.com\tTRUE\t/\tFALSE\t0\tcf_clearance\ttok", text)
        finally:
            os.unlink(path)
            if cookie_path:
                os.unlink(cookie_path)


class FlareSolverrTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_cookies_from_flaresolverr(self):
        payload = {
            "status": "ok",
            "solution": {
                "cookies": [
                    {
                        "name": "cf_clearance",
                        "value": "tok",
                        "domain": ".vk.com",
                        "path": "/",
                        "secure": True,
                        "expires": 1,
                    }
                ],
                "userAgent": "Mozilla/5.0 test",
            },
        }
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value=payload)

        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("app.ytdlp.settings") as fake_settings, patch(
            "app.ytdlp.httpx.AsyncClient", return_value=client
        ):
            fake_settings.flaresolverr_url = "http://flaresolverr:8191"
            fake_settings.cookie_file = "/missing"
            from app.ytdlp import fetch_flaresolverr_cookies

            cookies, ua = await fetch_flaresolverr_cookies("https://vk.com/video1")

        self.assertEqual(cookies[0]["name"], "cf_clearance")
        self.assertEqual(ua, "Mozilla/5.0 test")
        client.post.assert_awaited_once()
        called_url = client.post.await_args.args[0]
        self.assertEqual(called_url, "http://flaresolverr:8191/v1")

    async def test_disabled_when_env_empty(self):
        with patch("app.ytdlp.settings") as fake_settings:
            fake_settings.flaresolverr_url = ""
            from app.ytdlp import fetch_flaresolverr_cookies, flaresolverr_enabled

            self.assertFalse(flaresolverr_enabled())
            with self.assertRaises(RuntimeError):
                await fetch_flaresolverr_cookies("https://vk.com/video1")

    async def test_inspect_playlist_tries_vkvideo_then_vk_com(self):
        calls: list[str] = []

        async def fake_json(url, extra, timeout, extra_cookies=None, user_agent=None):
            calls.append(url)
            if "vkvideo.ru" in url:
                raise RuntimeError("HTTP Error 403: Forbidden")
            return {"title": "ok", "entries": []}

        with patch("app.ytdlp.settings") as fake_settings, patch(
            "app.ytdlp._ytdlp_json", side_effect=fake_json
        ):
            fake_settings.flaresolverr_url = ""
            from app.ytdlp import inspect_playlist_flat

            data = await inspect_playlist_flat("https://vk.com/playlist/-1_2")

        self.assertEqual(data["title"], "ok")
        self.assertEqual(
            calls,
            [
                "https://vkvideo.ru/playlist/-1_2",
                "https://vk.com/playlist/-1_2",
            ],
        )

    async def test_inspect_playlist_uses_flaresolverr_on_403(self):
        json_calls: list[tuple[str, bool]] = []

        async def fake_json(url, extra, timeout, extra_cookies=None, user_agent=None):
            json_calls.append((url, bool(extra_cookies)))
            if extra_cookies:
                return {"title": "cleared", "entries": []}
            raise RuntimeError("HTTP Error 403: Forbidden")

        async def fake_flare(url):
            return ([{"name": "cf_clearance", "value": "x", "domain": ".vkvideo.ru"}], "UA")

        with patch("app.ytdlp.settings") as fake_settings, patch(
            "app.ytdlp._ytdlp_json", side_effect=fake_json
        ), patch("app.ytdlp.fetch_flaresolverr_cookies", side_effect=fake_flare):
            fake_settings.flaresolverr_url = "http://flaresolverr:8191"
            from app.ytdlp import inspect_playlist_flat

            data = await inspect_playlist_flat("https://vk.com/playlist/-1_2")

        self.assertEqual(data["title"], "cleared")
        self.assertEqual(
            json_calls,
            [
                ("https://vkvideo.ru/playlist/-1_2", False),
                ("https://vkvideo.ru/playlist/-1_2", True),
            ],
        )

    async def test_download_tries_vk_com_then_vkvideo(self):
        calls: list[str] = []

        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None):
            calls.append(url)
            if "vk.com" in url:
                return 1, None, "HTTP Error 403: Forbidden"
            return 0, "/downloads/x.mp4", "ok"

        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.ytdlp.settings"
        ) as fake_settings, patch(
            "app.ytdlp._download_once", side_effect=fake_once
        ):
            fake_settings.flaresolverr_url = ""
            fake_settings.download_root = tmp
            fake_settings.max_height = 720
            fake_settings.download_rate = "500K"
            fake_settings.cookie_file = "/missing"
            from app.ytdlp import download_video

            rc, path, log = await download_video(
                "https://vkvideo.ru/video-1_2",
                "channel",
                title="Hello",
                video_id="1_2",
                upload_date="20240102",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(path, "/downloads/x.mp4")
        self.assertEqual(
            calls,
            ["https://vk.com/video-1_2", "https://vkvideo.ru/video-1_2"],
        )


if __name__ == "__main__":
    unittest.main()
