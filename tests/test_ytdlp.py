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
    download_folder_name,
    download_format,
    download_stem,
    download_url_candidates,
    ffmpeg_tv_args,
    format_upload_date,
    is_library_media_path,
    is_tv_ready,
    iter_library_media,
    label_from_url,
    looks_like_bot_protection,
    looks_like_no_data_blocks,
    mirror_url,
    normalize_vk_url,
    remove_stale_download_parts,
    reset_library_tv_failures,
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
            "2024-01-15 - Lecture 4 — Linear maps [-214484275_456239461].%(ext)s",
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
        self.assertEqual(path.name, "Untitled [-214484275_456239461].%(ext)s")
        self.assertNotIn("NA -", path.name)

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
        self.assertLessEqual(len(stem) + len(".mp4"), 255)
        self.assertIn("[-1_2]", stem)
        self.assertTrue(stem.startswith("2024-01-15 - "))


class DownloadOutputWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_uses_subscription_folder_not_uploader(self):
        outputs: list[str] = []

        async def fake_once(url, output, fmt, extra_cookies=None, user_agent=None):
            outputs.append(output)
            return 0, output.replace("%(ext)s", "mp4"), "ok"

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
                "VK Uploader",
                title="Lecture 4",
                video_id="-1_2",
                upload_date="20240115",
                folder="Algebra course",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(len(outputs), 1)
        self.assertIn("/Algebra course/", outputs[0])
        self.assertIn("2024-01-15 - Lecture 4 [-1_2].%(ext)s", outputs[0])
        self.assertNotIn("/VK Uploader/", outputs[0])
        self.assertTrue(path.endswith("2024-01-15 - Lecture 4 [-1_2].mp4"))


class StalePartialDownloadTests(unittest.TestCase):
    def test_removes_tiny_part_but_keeps_large_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "Talk [-1_2].%(ext)s")
            tiny = Path(tmp) / "Talk [-1_2].mp4.part"
            large = Path(tmp) / "Talk [-1_2].webm.part"
            empty = Path(tmp) / "Talk [-1_2].mp4"
            keep = Path(tmp) / "Talk [-1_2].mp4.ok"
            tiny.write_bytes(b"x" * 100)
            large.write_bytes(b"x" * (200 * 1024))
            empty.write_bytes(b"")
            keep.write_bytes(b"done")

            removed = remove_stale_download_parts(output)
            self.assertIn(str(tiny), removed)
            self.assertIn(str(empty), removed)
            self.assertFalse(tiny.exists())
            self.assertFalse(empty.exists())
            self.assertTrue(large.exists())
            self.assertTrue(keep.exists())

            forced = remove_stale_download_parts(output, force=True)
            self.assertIn(str(large), forced)
            self.assertFalse(large.exists())
            self.assertTrue(keep.exists())

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
            return 0, output.replace("%(ext)s", "mp4"), "ok"

        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.ytdlp.settings"
        ) as fake_settings, patch(
            "app.ytdlp._download_once", side_effect=fake_once
        ), patch(
            "app.ytdlp.ensure_tv_compatible",
            new=AsyncMock(side_effect=lambda path: path),
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
        self.assertTrue(path.endswith("Talk [-1_2].mp4"))

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

    def test_library_paths_skip_temps_and_hidden(self):
        from pathlib import Path

        self.assertTrue(is_library_media_path(Path("/downloads/show/talk.mp4")))
        self.assertTrue(is_library_media_path(Path("/downloads/show/talk.webm")))
        self.assertFalse(is_library_media_path(Path("/downloads/show/.vkget-share")))
        self.assertFalse(is_library_media_path(Path("/downloads/show/talk.mp4.part")))
        self.assertFalse(is_library_media_path(Path("/downloads/show/talk.compat.mp4")))
        self.assertFalse(is_library_media_path(Path("/downloads/show/.hidden.mp4")))


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
            bad = os.path.join(tmp, "bad.mp4")
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
            self.assertEqual(
                await asyncio_run_ffmpeg(
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
                        bad,
                    ]
                ),
                0,
            )
            from app.ytdlp import (
                ensure_tv_compatible,
                next_library_tv_rewrite,
                probe_media,
            )

            reset_library_tv_failures()
            probe = await probe_media(good)
            self.assertTrue(is_tv_ready(probe, good))
            before = os.stat(good).st_mtime_ns
            same = await ensure_tv_compatible(good, force_remux=False)
            self.assertEqual(same, good)
            self.assertEqual(os.stat(good).st_mtime_ns, before)
            found = [str(path) for path in iter_library_media(tmp)]
            self.assertEqual(found, [bad, good])
            picked = await next_library_tv_rewrite(tmp)
            self.assertEqual(picked, bad)


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
