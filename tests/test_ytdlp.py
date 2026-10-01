from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pathlib import Path

from app.ytdlp import (
    _netscape_cookie_line,
    build_download_output,
    dated_filename,
    download_folder_name,
    download_format,
    download_stem,
    safe_component,
    download_url_candidates,
    ffmpeg_tv_args,
    format_upload_date,
    is_tv_ready,
    label_from_url,
    looks_like_bot_protection,
    looks_like_no_data_blocks,
    mirror_url,
    normalize_vk_url,
    adopt_existing_download,
    is_partial_download,
    is_usable_channel,
    parse_download_marks,
    place_downloaded_file,
    resolve_on_disk_download,
    remove_orphan_partials,
    remove_stale_download_parts,
    scan_url_candidates,
    to_vk_com,
    to_vkvideo,
    tv_codec_plan,
)


def tv_probe(*, video=None, audio=None, cover=False, no_audio=False, fmt="mov,mp4,m4a,3gp,3g2,mj2"):
    video_stream = {
        "codec_type": "video",
        "codec_name": "h264",
        "pix_fmt": "yuv420p",
        "profile": "High",
        "level": 40,
        "width": 1280,
        "height": 720,
        "codec_tag_string": "avc1",
        "field_order": "progressive",
    }
    audio_stream = {
        "codec_type": "audio",
        "codec_name": "aac",
        "profile": "LC",
        "channels": 2,
        "sample_rate": "48000",
    }
    if video:
        video_stream.update(video)
    if audio:
        audio_stream.update(audio)
    streams = []
    if cover:
        streams.append(
            {
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 1},
            }
        )
    streams.append(video_stream)
    if not no_audio:
        streams.append(audio_stream)
    return {"format": {"format_name": fmt}, "streams": streams}


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

    def test_subscription_key_collapses_host_and_channel_aliases(self):
        from app.ytdlp import subscription_source_key

        channel = "vkvideo.ru/@shows"
        self.assertEqual(
            subscription_source_key("https://vk.com/@Shows/"),
            channel,
        )
        self.assertEqual(
            subscription_source_key("https://vkvideo.ru/video/@shows?z=video-1_2"),
            channel,
        )
        self.assertEqual(
            subscription_source_key("http://m.vk.com/video/@shows?ref=feed"),
            channel,
        )
        self.assertEqual(
            subscription_source_key("https://new.vk.com/@shows"),
            channel,
        )
        self.assertEqual(
            subscription_source_key("https://www.vk.ru/videos/@shows/"),
            channel,
        )

    def test_subscription_key_keeps_distinct_playlists(self):
        from app.ytdlp import subscription_source_key

        one = subscription_source_key("https://vk.com/playlist/-1_2")
        two = subscription_source_key("https://vkvideo.ru/video/playlist/-1_2?ref=1")
        other = subscription_source_key("https://vk.com/playlist/-1_3")
        album = subscription_source_key(
            "https://vk.com/videos-1?section=album_7&ref=feed"
        )
        videos = subscription_source_key("https://vkvideo.ru/videos-1")
        self.assertEqual(one, "vkvideo.ru/playlist/-1_2")
        self.assertEqual(two, one)
        self.assertNotEqual(other, one)
        self.assertEqual(videos, "vkvideo.ru/videos-1")
        self.assertEqual(album, "vkvideo.ru/videos-1?section=album_7")
        self.assertEqual(subscription_source_key("   "), "")
        self.assertEqual(subscription_source_key("https://example.com/A/B/"), "example.com/a/b")

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
            final = Path(output.replace("%(ext)s", "mp4"))
            final.write_bytes(b"video")
            return 0, str(final), "ok"

        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.ytdlp.settings"
        ) as fake_settings, patch(
            "app.ytdlp._download_once", side_effect=fake_once
        ), patch(
            "app.ytdlp.ensure_tv_compatible",
            new=AsyncMock(side_effect=lambda path, force_remux=False: path),
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
        self.assertTrue(path.endswith("Hello [1_2]-2024-01-02.mp4"))
        self.assertEqual(
            calls,
            ["https://vk.com/video-1_2", "https://vkvideo.ru/video-1_2"],
        )


class DownloadPathTests(unittest.TestCase):
    def test_subscription_folder_and_dated_filename(self):
        self.assertEqual(download_folder_name(subscription_title="Algebra", channel="Uploader"), "Algebra")
        path = build_download_output(
            folder="Algebra",
            title="Lecture 4 — Linear maps",
            video_id="-214484275_456239461",
            upload_date="20240115",
        )
        self.assertEqual(path.parent.name, "Algebra")
        self.assertEqual(
            path.name,
            "Lecture 4 — Linear maps [-214484275_456239461]-2024-01-15.%(ext)s",
        )

    def test_placeholders_never_become_folders_or_na_dates(self):
        self.assertEqual(
            download_folder_name(subscription_title="Subscription", channel="Unknown"),
            "_single",
        )
        self.assertEqual(format_upload_date("NA"), "")
        self.assertEqual(format_upload_date("2024-01-15"), "2024-01-15")
        path = build_download_output(
            folder="Subscription",
            title="Video -214484275_456239461",
            video_id="-214484275_456239461",
            upload_date="NA",
        )
        self.assertEqual(path.parent.name, "_single")
        self.assertEqual(
            path.name,
            "Video -214484275_456239461 [-214484275_456239461].%(ext)s",
        )
        self.assertNotIn("NA -", path.name)
        self.assertNotIn("Untitled", path.name)

        dated = build_download_output(
            folder="Subscription",
            title="NA",
            video_id="-1_2",
            upload_date="20240115",
            channel="Algebra",
        )
        self.assertEqual(
            dated.name,
            "Video -1_2 [-1_2]-2024-01-15.%(ext)s",
        )
        self.assertNotIn("Algebra", dated.name)

    def test_playlist_id_subscription_name_is_a_valid_folder(self):
        self.assertEqual(
            download_folder_name(subscription_title="-214484275_7", channel="Subscription"),
            "-214484275_7",
        )
        path = build_download_output(
            folder="-214484275_7",
            title="Talk",
            video_id="-1_2",
            upload_date="20240115",
        )
        self.assertEqual(path.parent.name, "-214484275_7")

    def test_one_off_uses_uploader_or_single(self):
        self.assertEqual(download_folder_name(channel="Some Channel"), "Some Channel")
        self.assertEqual(download_folder_name(channel="_single"), "_single")

    def test_long_filename_stays_under_limit(self):
        stem = download_stem("A" * 400, "-1_2", "20240115")
        self.assertLessEqual(len((stem + ".%(ext)s").encode()), 255)
        self.assertLessEqual(len(stem) + len(".mp4"), 255)
        self.assertTrue(stem.endswith("[-1_2]-2024-01-15"))
        cyrillic = "Лекция " + "я" * 180
        cyr_stem = download_stem(cyrillic, "-214484275_456239461", "20240115")
        self.assertLessEqual(len((cyr_stem + ".%(ext)s").encode()), 255)
        self.assertLessEqual(len((cyr_stem + ".mp4").encode()), 255)
        folder = safe_component(cyrillic)
        self.assertLessEqual(len(folder.encode()), 255)

    def test_dated_filename_requires_trailing_date(self):
        self.assertTrue(
            dated_filename(
                "/downloads/Algebra/Lecture 4 — Linear maps [-1_2]-2024-01-15.mp4",
                "-1_2",
            )
        )
        self.assertFalse(dated_filename("/downloads/Algebra/Algebra.mp4", "-1_2"))
        self.assertFalse(
            dated_filename(
                "/downloads/Algebra/2024-01-15 - Algebra [-1_2].mp4",
                "-1_2",
            )
        )

    def test_marks_keep_title_between_tabs(self):
        path, meta = parse_download_marks(
            "progress\n"
            "vkget_path:/downloads/Algebra/[-1_2].mp4\n"
            "vkget_meta:20240115\tLecture 4 — Linear maps\t-1_2\n"
        )
        self.assertEqual(path, "/downloads/Algebra/[-1_2].mp4")
        self.assertEqual(meta["upload_date"], "20240115")
        self.assertEqual(meta["title"], "Lecture 4 — Linear maps")
        self.assertEqual(meta["id"], "-1_2")


class DownloadOutputWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_uses_subscription_folder_not_uploader(self):
        outputs: list[str] = []

        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None):
            outputs.append(output)
            final = Path(output.replace("%(ext)s", "mp4"))
            final.write_bytes(b"video")
            return 0, str(final), "ok"

        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.ytdlp.settings"
        ) as fake_settings, patch(
            "app.ytdlp._download_once", side_effect=fake_once
        ), patch(
            "app.ytdlp.ensure_tv_compatible",
            new=AsyncMock(side_effect=lambda path, force_remux=False: path),
        ):
            fake_settings.flaresolverr_url = ""
            fake_settings.download_root = tmp
            fake_settings.max_height = 720
            fake_settings.download_rate = "500K"
            fake_settings.cookie_file = "/missing"
            from app.ytdlp import download_video

            rc, path, log = await download_video(
                "https://vk.com/video-1_2",
                "VK Uploader",
                title="Lecture 4",
                video_id="-1_2",
                upload_date="20240115",
                folder="Algebra course",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(len(outputs), 1)
        self.assertIn("/Algebra course/", outputs[0])
        self.assertIn("[-1_2].%(ext)s", outputs[0])
        self.assertNotIn("Lecture 4", outputs[0])
        self.assertNotIn("/VK Uploader/", outputs[0])
        self.assertTrue(path.endswith("Lecture 4 [-1_2]-2024-01-15.mp4"))

    async def test_already_on_disk_is_renamed_instead_of_failing(self):
        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None):
            final = Path(output.replace("%(ext)s", "mp4"))
            final.write_bytes(b"video")
            return 1, None, f"ERROR: [Errno 17] File exists: {final}"

        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.ytdlp.settings"
        ) as fake_settings, patch(
            "app.ytdlp._download_once", side_effect=fake_once
        ), patch(
            "app.ytdlp.ensure_tv_compatible",
            new=AsyncMock(side_effect=lambda path, force_remux=False: path),
        ):
            fake_settings.flaresolverr_url = ""
            fake_settings.download_root = tmp
            fake_settings.max_height = 720
            fake_settings.download_rate = "500K"
            fake_settings.cookie_file = "/missing"
            from app.ytdlp import download_video

            rc, path, log = await download_video(
                "https://vk.com/video-1_2",
                "Algebra",
                title="Lecture 4",
                video_id="-1_2",
                upload_date="20240115",
                folder="Algebra",
            )
            self.assertEqual(rc, 0)
            self.assertTrue(path.endswith("Lecture 4 [-1_2]-2024-01-15.mp4"))
            self.assertTrue(os.path.isfile(path))
            self.assertFalse(path.endswith("[-1_2].mp4"))

    async def test_success_without_a_file_is_not_success(self):
        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None):
            return 0, "not-a-real-file", "ok"

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
                "https://vk.com/video-1_2",
                "Algebra",
                title="Lecture 4",
                video_id="-1_2",
                upload_date="20240115",
                folder="Algebra",
            )

        self.assertEqual(rc, 1)
        self.assertFalse(path and os.path.isfile(path))

    async def test_ytdlp_marks_name_the_file_not_the_channel(self):
        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None, proxy=None):
            partial = output.replace("%(ext)s", "mp4")
            Path(partial).write_bytes(b"video")
            log = (
                f"vkget_path:{partial}\n"
                "vkget_meta:20240115\tLecture 4 — Linear maps\t-1_2\n"
            )
            return 0, partial, log

        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.ytdlp.settings"
        ) as fake_settings, patch(
            "app.ytdlp._download_once", side_effect=fake_once
        ), patch(
            "app.ytdlp.ensure_tv_compatible",
            new=AsyncMock(side_effect=lambda path, force_remux=False: path),
        ):
            fake_settings.flaresolverr_url = ""
            fake_settings.download_root = tmp
            fake_settings.max_height = 720
            fake_settings.download_rate = "500K"
            fake_settings.cookie_file = "/missing"
            from app.ytdlp import download_video

            rc, path, log = await download_video(
                "https://vk.com/video-1_2",
                "Algebra",
                title="NA",
                video_id="-1_2",
                upload_date="NA",
                folder="Algebra",
            )
            partial = Path(tmp) / "Algebra" / "[-1_2].mp4"

            self.assertEqual(rc, 0)
            self.assertTrue(path.endswith("Lecture 4 — Linear maps [-1_2]-2024-01-15.mp4"))
            self.assertTrue(os.path.isfile(path))
            self.assertFalse(partial.exists())
            self.assertNotIn("Algebra [-1_2]", path)


class StalePartialDownloadTests(unittest.TestCase):
    def test_keeps_large_part_and_its_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "Talk [-1_2].%(ext)s")
            tiny = Path(tmp) / "Talk [-1_2].mp4.part"
            sidecar = Path(tmp) / "Talk [-1_2].mp4.ytdl"
            large = Path(tmp) / "Talk [-1_2].webm.part"
            empty = Path(tmp) / "Talk [-1_2].mp4"
            keep = Path(tmp) / "Talk [-1_2].mp4.ok"
            tiny.write_bytes(b"x" * 100)
            sidecar.write_bytes(b"{}")
            large.write_bytes(b"x" * (200 * 1024))
            empty.write_bytes(b"")
            keep.write_bytes(b"done")

            removed = remove_stale_download_parts(output)
            self.assertIn(str(empty), removed)
            self.assertFalse(empty.exists())
            self.assertTrue(tiny.exists())
            self.assertTrue(sidecar.exists())
            self.assertTrue(large.exists())
            self.assertTrue(keep.exists())

            forced = remove_stale_download_parts(output, force=True)
            self.assertIn(str(large), forced)
            self.assertIn(str(sidecar), forced)
            self.assertFalse(large.exists())
            self.assertFalse(sidecar.exists())
            self.assertFalse(tiny.exists())
            self.assertTrue(keep.exists())

    def test_removes_tiny_partial_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "[-1_2].%(ext)s")
            tiny = Path(tmp) / "[-1_2].mp4.part"
            sidecar = Path(tmp) / "[-1_2].mp4.ytdl"
            tiny.write_bytes(b"x" * 100)
            sidecar.write_bytes(b"{}")
            removed = remove_stale_download_parts(output)
            self.assertIn(str(tiny), removed)
            self.assertIn(str(sidecar), removed)
            self.assertFalse(tiny.exists())
            self.assertFalse(sidecar.exists())

    def test_dash_audio_stream_is_a_partial_kept_for_resume(self):
        audio = Path("[-211437014_456248648].fdash_sep-11.m4a")
        video = Path("[-211437014_456248648].fdash_sep-4.mp4")
        numeric = Path("[-1_2].f137.mp4")
        finished = Path("Talk [-211437014_456248648]-2026-09-11.mp4")
        bare = Path("[-211437014_456248648].mp4")
        self.assertTrue(is_partial_download(audio))
        self.assertTrue(is_partial_download(video))
        self.assertTrue(is_partial_download(numeric))
        self.assertTrue(is_partial_download(Path("[-1_2].m4a")))
        self.assertFalse(is_partial_download(finished))
        self.assertFalse(is_partial_download(bare))

        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "[-211437014_456248648].%(ext)s")
            audio_file = Path(tmp) / audio.name
            video_part = Path(tmp) / "[-211437014_456248648].fdash_sep-4.mp4.part"
            audio_file.write_bytes(b"a" * (400 * 1024))
            video_part.write_bytes(b"v" * (900 * 1024))

            removed = remove_stale_download_parts(output)
            self.assertEqual(removed, [])
            self.assertTrue(audio_file.exists())
            self.assertTrue(video_part.exists())
            self.assertEqual(resolve_on_disk_download(output, str(audio_file)), "")

    def test_single_is_not_a_channel_or_preferred_folder(self):
        self.assertFalse(is_usable_channel("_single"))
        self.assertEqual(download_folder_name(channel="_single"), "_single")
        self.assertEqual(
            download_folder_name(subscription_title="Algebra", channel="_single"),
            "Algebra",
        )


class ExistingFileTests(unittest.TestCase):
    def test_keeps_proper_file_and_drops_bare_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proper = root / "Manual" / "Lecture [-220754053_456246764]-2024-01-15.mp4"
            bare = root / "_single" / "[-220754053_456246764].mp4"
            proper.parent.mkdir()
            bare.parent.mkdir()
            proper.write_bytes(b"kept")
            bare.write_bytes(b"duplicate")

            adopted = adopt_existing_download(
                tmp,
                video_id="-220754053_456246764",
                title="Lecture",
                upload_date="20240115",
                folder="Algebra",
            )

            self.assertEqual(adopted, str(proper))
            self.assertEqual(proper.read_bytes(), b"kept")
            self.assertFalse(bare.exists())

    def test_renames_bare_id_file_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = root / "_single" / "[-1_2].mp4"
            bare.parent.mkdir()
            bare.write_bytes(b"video")
            existing = root / "Algebra" / "Lecture [-1_2]-2024-01-15.mp4"
            existing.parent.mkdir()
            existing.write_bytes(b"kept")

            placed = place_downloaded_file(
                str(bare),
                title="Lecture",
                video_id="-1_2",
                upload_date="20240115",
                directory=existing.parent,
            )

            self.assertEqual(placed, str(existing))
            self.assertEqual(existing.read_bytes(), b"kept")
            self.assertFalse(bare.exists())

    def test_renames_only_bare_copy_into_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = root / "_single" / "[-1_2].mp4"
            bare.parent.mkdir()
            bare.write_bytes(b"video")

            adopted = adopt_existing_download(
                tmp,
                video_id="-1_2",
                title="Lecture",
                upload_date="20240115",
                folder="Algebra",
            )

            expected = root / "Algebra" / "Lecture [-1_2]-2024-01-15.mp4"
            self.assertEqual(adopted, str(expected))
            self.assertTrue(expected.is_file())
            self.assertEqual(expected.read_bytes(), b"video")
            self.assertFalse(bare.exists())

    def test_unmerged_dash_audio_is_not_a_finished_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "Shows" / "[-211437014_456248648].fdash_sep-11.m4a"
            audio.parent.mkdir()
            audio.write_bytes(b"a" * (500 * 1024))

            adopted = adopt_existing_download(
                tmp,
                video_id="-211437014_456248648",
                title="№ 761",
                upload_date="20260911",
                folder="Shows",
            )

            self.assertIsNone(adopted)
            self.assertTrue(audio.is_file())
            self.assertEqual(audio.stat().st_size, 500 * 1024)

    def test_place_leaves_unmerged_dash_audio_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "[-211437014_456248648].fdash_sep-11.m4a"
            audio.write_bytes(b"a" * 1000)
            placed = place_downloaded_file(
                str(audio),
                title="№ 761",
                video_id="-211437014_456248648",
                upload_date="20260911",
            )
            self.assertEqual(placed, str(audio))
            self.assertEqual(list(Path(tmp).iterdir()), [audio])


class OrphanPartialTests(unittest.TestCase):
    def test_sweep_keeps_active_and_manual_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            finished = root / "Show" / "Talk [-9_9]-2024-01-15.mp4"
            done_part = root / "Show" / "Talk [-9_9]-2024-01-15.mp4.part"
            queued_part = root / "Show" / "[-1_2].mp4.part"
            failed_part = root / "Show" / "[-3_4].webm.part"
            failed_meta = root / "Show" / "[-3_4].webm.ytdl"
            manual = root / "Elsewhere" / "manual.mp4.part"
            manual_done = root / "Elsewhere" / "clip.mp4"
            manual_part = root / "Elsewhere" / "clip.mp4.part"
            media = root / "Show" / "Keep me.mp4"
            queued_audio = root / "Show" / "[-1_2].fdash_sep-11.m4a"
            done_audio = root / "Show" / "[-9_9].fdash_sep-11.m4a"
            for path, payload in (
                (finished, b"show"),
                (done_part, b"x"),
                (queued_part, b"x" * 1000),
                (failed_part, b"x" * (80 * 1024)),
                (failed_meta, b"{}"),
                (manual, b"x"),
                (manual_done, b"clip"),
                (manual_part, b"x"),
                (media, b"keep"),
                (queued_audio, b"a" * 1000),
                (done_audio, b"a" * 1000),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

            removed = remove_orphan_partials(tmp, {"-1_2", "-3_4"})

            self.assertIn(str(done_part), removed)
            self.assertIn(str(manual_part), removed)
            self.assertFalse(done_part.exists())
            self.assertFalse(manual_part.exists())
            self.assertTrue(queued_part.exists())
            self.assertTrue(failed_part.exists())
            self.assertTrue(failed_meta.exists())
            self.assertTrue(manual.exists())
            self.assertTrue(finished.exists())
            self.assertTrue(media.exists())
            self.assertTrue(manual_done.exists())
            self.assertTrue(queued_audio.exists())
            self.assertFalse(done_audio.exists())
            self.assertIn(str(done_audio), removed)

    def test_detects_data_blocks_error(self):
        self.assertTrue(
            looks_like_no_data_blocks(
                "ERROR: Did not get any data blocks ERROR: Did not get any data blocks"
            )
        )
        self.assertFalse(looks_like_no_data_blocks("HTTP Error 403: Forbidden"))


class NoDataBlocksRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_once_after_discarding_partial(self):
        calls: list[str] = []

        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None, proxy=None):
            calls.append(url)
            if len(calls) == 1:
                return 1, None, "ERROR: Did not get any data blocks"
            final = Path(output.replace("%(ext)s", "mp4"))
            final.write_bytes(b"video")
            return 0, str(final), "ok"

        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.ytdlp.settings"
        ) as fake_settings, patch(
            "app.ytdlp._download_once", side_effect=fake_once
        ), patch(
            "app.ytdlp.ensure_tv_compatible",
            new=AsyncMock(side_effect=lambda path, **kwargs: path),
        ):
            fake_settings.flaresolverr_url = ""
            fake_settings.download_root = tmp
            fake_settings.max_height = 720
            fake_settings.download_rate = "500K"
            fake_settings.cookie_file = "/missing"
            from app.ytdlp import download_video

            rc, path, log = await download_video(
                "https://vk.com/video-1_2",
                "channel",
                title="Talk",
                video_id="-1_2",
                upload_date="20240115",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["https://vk.com/video-1_2", "https://vk.com/video-1_2"])
        self.assertTrue(path.endswith("Talk [-1_2]-2024-01-15.mp4"))

    async def test_does_not_retry_unrelated_errors(self):
        calls: list[str] = []

        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None, proxy=None):
            calls.append(url)
            return 1, None, "HTTP Error 403: Forbidden"

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
                "https://vk.com/video-1_2",
                "channel",
                title="Talk",
                video_id="-1_2",
            )

        self.assertEqual(rc, 1)
        self.assertEqual(
            calls,
            ["https://vk.com/video-1_2", "https://vkvideo.ru/video-1_2"],
        )


class TvCompatibleTests(unittest.TestCase):
    def test_format_prefers_h264_aac(self):
        fmt = download_format(720)
        self.assertTrue(fmt.startswith("best[ext=mp4][vcodec^=avc][height<=720]"))
        self.assertIn("bestvideo[vcodec^=avc1][height<=720]+bestaudio[acodec^=mp4a]", fmt)
        self.assertIn("bestvideo[height<=720]+bestaudio", fmt)
        avc_at = fmt.find("vcodec^=avc")
        any_at = fmt.find("bestvideo[height<=720]+bestaudio")
        self.assertLess(avc_at, any_at)
        self.assertTrue(fmt.endswith("best[height<=720]"))
        self.assertFalse(fmt.endswith("/best"))

    def test_plan_copies_tv_safe_h264_aac_lc(self):
        probe = tv_probe()
        plan = tv_codec_plan(probe)
        self.assertTrue(plan["copy_video"])
        self.assertTrue(plan["copy_audio"])
        args = ffmpeg_tv_args("in.mp4", "out.mp4", probe)
        self.assertEqual(args[args.index("-map") + 1], "0:V:0")
        self.assertEqual(args[args.index("-c:v") + 1], "copy")
        self.assertEqual(args[args.index("-c:a") + 1], "copy")
        self.assertEqual(args[args.index("-tag:v") + 1], "avc1")
        self.assertIn("+faststart", args)

    def test_plan_skips_cover_art_and_maps_real_video(self):
        probe = tv_probe(cover=True)
        plan = tv_codec_plan(probe)
        self.assertTrue(plan["has_video"])
        self.assertTrue(plan["copy_video"])
        args = ffmpeg_tv_args("in.mp4", "out.mp4", probe)
        self.assertEqual(args[args.index("-map") + 1], "0:V:0")

    def test_plan_rejects_he_aac_and_surround(self):
        he = tv_codec_plan(tv_probe(audio={"profile": "HE-AAC"}))
        self.assertTrue(he["copy_video"])
        self.assertFalse(he["copy_audio"])
        surround = tv_codec_plan(tv_probe(audio={"channels": 6}))
        self.assertFalse(surround["copy_audio"])
        args = ffmpeg_tv_args("in.mp4", "out.mp4", tv_probe(audio={"profile": "HE-AAC"}))
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-profile:a") + 1], "aac_low")
        self.assertEqual(args[args.index("-ac") + 1], "2")
        self.assertEqual(args[args.index("-ar") + 1], "48000")

    def test_plan_rejects_avc3_high_level_1080p_and_odd_size(self):
        self.assertFalse(tv_codec_plan(tv_probe(video={"codec_tag_string": "avc3"}))["copy_video"])
        self.assertFalse(tv_codec_plan(tv_probe(video={"level": 51}))["copy_video"])
        self.assertFalse(tv_codec_plan(tv_probe(video={"width": 1920, "height": 1080}))["copy_video"])
        self.assertFalse(tv_codec_plan(tv_probe(video={"width": 1279, "height": 720}))["copy_video"])
        args = ffmpeg_tv_args("in.mp4", "out.mp4", tv_probe(video={"height": 1080, "width": 1920}))
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-tag:v") + 1], "avc1")
        self.assertIn("scale=", args[args.index("-vf") + 1])
        self.assertIn("min(720,ih)", args[args.index("-vf") + 1])

    def test_plan_transcodes_vp9_opus(self):
        probe = tv_probe(video={"codec_name": "vp9"}, audio={"codec_name": "opus", "profile": ""})
        plan = tv_codec_plan(probe)
        self.assertFalse(plan["copy_video"])
        self.assertFalse(plan["copy_audio"])
        args = ffmpeg_tv_args("in.webm", "out.mp4", probe)
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-profile:a") + 1], "aac_low")
        self.assertIn("+faststart", args)

    def test_plan_transcodes_10bit_h264(self):
        plan = tv_codec_plan(tv_probe(video={"pix_fmt": "yuv420p10le"}))
        self.assertFalse(plan["copy_video"])
        self.assertTrue(plan["copy_audio"])

class TvCompatibleEncodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcodes_mpeg4_mp2_to_h264_aac(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "bad.mp4")
            make = await asyncio_run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=160x120:rate=5:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:duration=1",
                    "-c:v",
                    "mpeg4",
                    "-c:a",
                    "mp2",
                    src,
                ]
            )
            self.assertEqual(make, 0)
            from app.ytdlp import ensure_tv_compatible, probe_media

            out = await ensure_tv_compatible(src)
            self.assertTrue(os.path.isfile(out))
            probe = await probe_media(out)
            streams = {s["codec_type"]: s["codec_name"] for s in probe["streams"]}
            self.assertEqual(streams["video"], "h264")
            self.assertEqual(streams["audio"], "aac")
            video = next(s for s in probe["streams"] if s["codec_type"] == "video")
            audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
            self.assertEqual(video["pix_fmt"], "yuv420p")
            self.assertEqual((video.get("codec_tag_string") or "").lower(), "avc1")
            self.assertEqual(_norm_test_profile(audio.get("profile")), "lc")
            self.assertTrue(is_tv_ready(probe, out))

    async def test_skips_already_tv_ready_library_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.mp4")
            self.assertEqual(
                await asyncio_run_ffmpeg(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc=size=1280x720:rate=25:duration=1",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=1000:duration=1",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-profile:v",
                        "high",
                        "-level",
                        "4.0",
                        "-c:a",
                        "aac",
                        "-profile:a",
                        "aac_low",
                        "-ac",
                        "2",
                        "-ar",
                        "48000",
                        "-movflags",
                        "+faststart",
                        good,
                    ]
                ),
                0,
            )
            from app.ytdlp import ensure_tv_compatible, probe_media

            probe = await probe_media(good)
            self.assertTrue(is_tv_ready(probe, good))
            before = os.stat(good).st_mtime_ns
            same = await ensure_tv_compatible(good, force_remux=False)
            self.assertEqual(same, good)
            self.assertEqual(os.stat(good).st_mtime_ns, before)


class IdleRenameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db import Base
        from app.scheduler import _rename_settled

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.root = Path(tempfile.mkdtemp())
        (self.root / ".vkget-share").write_text("ok\n")
        self.outside = Path(tempfile.mkdtemp())
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        _rename_settled.clear()

    def tearDown(self):
        from app.scheduler import _rename_settled

        _rename_settled.clear()
        self.engine.dispose()
        os.unlink(self.db_path)
        for folder in (self.root, self.outside):
            for path in sorted(folder.rglob("*"), reverse=True):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            folder.rmdir()

    def _video(self, db, *, external_id, name, folder, title="Algebra", upload_date=None):
        from app.models import Video

        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_text("video")
        video = Video(
            source="vk",
            external_id=external_id,
            webpage_url=f"https://vk.com/video{external_id}",
            title=title,
            channel="Algebra",
            upload_date=upload_date,
            status="COMPLETED",
            local_path=str(target),
        )
        db.add(video)
        db.commit()
        return video

    async def test_renames_one_channel_only_file_per_idle_pass(self):
        from app.scheduler import rename_one_completed_file

        with self.Session() as db:
            dated = self._video(
                db,
                external_id="-1_9",
                name="Lecture 4 — Linear maps [-1_9]-2024-01-15.mp4",
                folder=self.root / "Algebra",
                title="Lecture 4 — Linear maps",
                upload_date="20240115",
            )
            outside = self._video(
                db,
                external_id="-1_8",
                name="Algebra.mp4",
                folder=self.outside,
            )
            first = self._video(
                db,
                external_id="-1_2",
                name="Algebra.mp4",
                folder=self.root / "Algebra",
            )
            second = self._video(
                db,
                external_id="-1_3",
                name="Channel only.mp4",
                folder=self.root / "Algebra",
            )
            dated_path = dated.local_path
            outside_path = outside.local_path
            first_id = first.id
            second_id = second.id

        calls: list[str] = []

        async def fake_resolve(url, *, title="", channel="", upload_date=None, external_id=""):
            calls.append(url)
            self.assertEqual(channel, "")
            return {
                "title": "Lecture 4 — Linear maps",
                "channel": "Algebra",
                "upload_date": "20240115",
            }

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.settings"
        ) as fake_settings, patch(
            "app.scheduler.resolve_video_metadata",
            side_effect=fake_resolve,
        ):
            fake_settings.download_root = str(self.root)
            renamed = await rename_one_completed_file()
            with self.Session() as db:
                from app.models import Video

                mid = db.get(Video, second_id)
                self.assertTrue(mid.local_path.endswith("Channel only.mp4"))
                self.assertTrue(os.path.isfile(mid.local_path))
            again = await rename_one_completed_file()

        expected = "Lecture 4 — Linear maps [-1_2]-2024-01-15.mp4"
        self.assertTrue(renamed)
        self.assertTrue(again)
        self.assertEqual(
            calls,
            [
                "https://vk.com/video-1_2",
                "https://vk.com/video-1_3",
            ],
        )
        self.assertTrue(os.path.isfile(dated_path))
        self.assertTrue(os.path.isfile(outside_path))
        self.assertFalse((self.root / "Algebra" / "Algebra.mp4").exists())
        with self.Session() as db:
            from app.models import Video

            first_row = db.get(Video, first_id)
            second_row = db.get(Video, second_id)
            self.assertTrue(first_row.local_path.endswith(expected))
            self.assertTrue(os.path.isfile(first_row.local_path))
            self.assertEqual(first_row.title, "Algebra")
            self.assertTrue(
                second_row.local_path.endswith(
                    "Lecture 4 — Linear maps [-1_3]-2024-01-15.mp4"
                )
            )
            self.assertTrue(os.path.isfile(second_row.local_path))
            self.assertEqual(second_row.title, "Algebra")

    def test_completed_dash_audio_returns_to_the_queue(self):
        from app.models import Video
        from app.scheduler import release_fragment_completions

        with self.Session() as db:
            broken = self._video(
                db,
                external_id="-211437014_456248648",
                name="[-211437014_456248648].fdash_sep-11.m4a",
                folder=self.root / "Shows",
                title="№ 761",
            )
            kept = self._video(
                db,
                external_id="-1_9",
                name="Lecture [-1_9]-2024-01-15.mp4",
                folder=self.root / "Shows",
                title="Lecture",
                upload_date="20240115",
            )
            broken_id = broken.id
            kept_id = kept.id
            audio = broken.local_path

        with patch("app.scheduler.SessionLocal", self.Session):
            released = release_fragment_completions()

        self.assertEqual(released, 1)
        self.assertTrue(os.path.isfile(audio))
        with self.Session() as db:
            broken = db.get(Video, broken_id)
            kept = db.get(Video, kept_id)
            self.assertEqual(broken.status, "QUEUED")
            self.assertIsNone(broken.local_path)
            self.assertIsNone(broken.completed_at)
            self.assertIsNotNone(broken.next_attempt_at)
            self.assertEqual(kept.status, "COMPLETED")
            self.assertTrue(kept.local_path.endswith("Lecture [-1_9]-2024-01-15.mp4"))


def _norm_test_profile(value) -> str:
    return str(value or "").strip().casefold()


async def asyncio_run_ffmpeg(args: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


if __name__ == "__main__":
    unittest.main()
