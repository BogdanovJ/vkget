from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.selfcheck import (
    ComponentCheck,
    _write_cache,
    collect_checks,
    format_version,
    load_selfcheck,
    parse_version,
    run_selfcheck,
    selfcheck_summary,
)


class VersionParseTests(unittest.TestCase):
    def test_parses_ytdlp_version(self):
        self.assertEqual(parse_version("2026.08.19"), (2026, 8, 19))
        self.assertEqual(format_version((2026, 8, 19)), "2026.08.19")
        self.assertIsNone(parse_version("not a version"))


class SelfCheckTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.cookies = Path(self.tmp.name) / "cookies.txt"
        self.cookies.write_text("# Netscape HTTP Cookie File\n.vk.com\tTRUE\t/\tTRUE\t0\tn\tv\n")
        self.downloads = Path(self.tmp.name) / "downloads"
        self.downloads.mkdir()
        (self.downloads / ".vkget-share").write_text("ok\n")

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)
        self.tmp.cleanup()

    def _settings(self):
        return patch(
            "app.selfcheck.settings",
            cookie_file=str(self.cookies),
            download_root=str(self.downloads),
        )

    def _ok_db(self):
        return patch(
            "app.selfcheck._database_check",
            return_value=ComponentCheck("database", "DATABASE", True, "OK", "connected", "ok"),
        )

    def test_missing_ytdlp_is_reported(self):
        def fake_run(args, timeout=4):
            if "yt-dlp" in args[0]:
                return 127, ""
            return 0, f"{args[0]} version 7.1.1"

        with self._settings(), self._ok_db(), patch(
            "app.selfcheck.YTDLP_BIN", "/tmp/vkget-missing-yt-dlp"
        ), patch("app.selfcheck.shutil.which", return_value=None), patch(
            "app.selfcheck._run", side_effect=fake_run
        ), patch("app.selfcheck.storage_ok", return_value=True):
            items = {item.id: item for item in collect_checks(None)}
        self.assertFalse(items["ytdlp"].ok)
        self.assertEqual(items["ytdlp"].status, "MISSING")

    def test_outdated_ytdlp_flags_update(self):
        def fake_run(args, timeout=4):
            if "yt-dlp" in args[0]:
                return 0, "2026.08.19"
            return 0, f"{args[0]} version 7.1.1"

        with self._settings(), self._ok_db(), patch(
            "app.selfcheck._run", side_effect=fake_run
        ), patch("app.selfcheck.shutil.which", return_value="/bin/true"), patch(
            "app.selfcheck.storage_ok", return_value=True
        ):
            items = {item.id: item for item in collect_checks("2026.09.01")}
        self.assertEqual(items["ytdlp"].status, "UPDATE")
        self.assertEqual(items["ytdlp"].latest, "2026.09.01")
        self.assertFalse(items["ytdlp"].ok)
        self.assertTrue(items["ffmpeg"].ok)

    def test_current_ytdlp_is_ok(self):
        with self._settings(), self._ok_db(), patch(
            "app.selfcheck._run", return_value=(0, "2026.09.01")
        ), patch("app.selfcheck.shutil.which", return_value="/bin/true"), patch(
            "app.selfcheck.storage_ok", return_value=True
        ):
            items = {item.id: item for item in collect_checks("2026.09.01")}
        self.assertEqual(items["ytdlp"].status, "OK")
        self.assertTrue(items["ytdlp"].ok)

    def test_ytdlp_timeout_uses_image_version(self):
        def fake_run(args, timeout=8):
            if "yt-dlp" in args[0]:
                return 124, "timed out"
            return 0, f"{args[0]} version 7.1.1"

        with self._settings(), self._ok_db(), patch(
            "app.selfcheck._run", side_effect=fake_run
        ), patch("app.selfcheck.shutil.which", return_value="/bin/true"), patch(
            "app.selfcheck.storage_ok", return_value=True
        ), patch("app.selfcheck.IMAGE_YTDLP_VERSION", "2026.08.19"):
            items = {item.id: item for item in collect_checks("2026.08.19")}
        ytdlp = items["ytdlp"]
        self.assertTrue(ytdlp.ok)
        self.assertEqual(ytdlp.status, "OK")
        self.assertEqual(ytdlp.current, "2026.08.19")
        self.assertNotEqual(ytdlp.current, ytdlp.detail)
        self.assertIn("image", ytdlp.detail.lower())

    def test_ytdlp_timeout_without_image_is_timeout(self):
        def fake_run(args, timeout=8):
            if "yt-dlp" in args[0]:
                return 124, "timed out"
            return 0, f"{args[0]} version 7.1.1"

        with self._settings(), self._ok_db(), patch(
            "app.selfcheck._run", side_effect=fake_run
        ), patch("app.selfcheck.shutil.which", return_value="/bin/true"), patch(
            "app.selfcheck.storage_ok", return_value=True
        ), patch("app.selfcheck.IMAGE_YTDLP_VERSION", ""):
            items = {item.id: item for item in collect_checks("2026.08.19")}
        ytdlp = items["ytdlp"]
        self.assertFalse(ytdlp.ok)
        self.assertEqual(ytdlp.status, "TIMEOUT")
        self.assertEqual(ytdlp.current, "—")
        self.assertIn("did not finish in time", ytdlp.detail)
        self.assertNotEqual(ytdlp.current, ytdlp.detail)

    def test_ytdlp_timeout_still_flags_update_from_image(self):
        def fake_run(args, timeout=8):
            if "yt-dlp" in args[0]:
                return 124, "timed out"
            return 0, f"{args[0]} version 7.1.1"

        with self._settings(), self._ok_db(), patch(
            "app.selfcheck._run", side_effect=fake_run
        ), patch("app.selfcheck.shutil.which", return_value="/bin/true"), patch(
            "app.selfcheck.storage_ok", return_value=True
        ), patch("app.selfcheck.IMAGE_YTDLP_VERSION", "2026.08.19"):
            items = {item.id: item for item in collect_checks("2026.09.01")}
        ytdlp = items["ytdlp"]
        self.assertEqual(ytdlp.status, "UPDATE")
        self.assertEqual(ytdlp.current, "2026.08.19")
        self.assertEqual(ytdlp.latest, "2026.09.01")
        self.assertFalse(ytdlp.ok)

    def test_empty_cookies_are_flagged(self):
        self.cookies.write_text("\n")
        with self._settings(), self._ok_db(), patch(
            "app.selfcheck._run", return_value=(0, "2026.09.01")
        ), patch("app.selfcheck.shutil.which", return_value="/bin/true"), patch(
            "app.selfcheck.storage_ok", return_value=True
        ):
            items = {item.id: item for item in collect_checks("2026.09.01")}
        self.assertEqual(items["cookies"].status, "EMPTY")
        self.assertFalse(items["cookies"].ok)

    def test_home_load_does_not_call_github(self):
        with self.Session() as db, patch(
            "app.selfcheck.fetch_latest_ytdlp", side_effect=AssertionError("github")
        ), patch("app.selfcheck.collect_checks", return_value=[]):
            report = load_selfcheck(db)
        self.assertIn("components", report)
        self.assertFalse(report["attention"])

    def test_home_reuses_cached_components(self):
        cached = {
            "checked_at": "2026-09-18T10:00:00",
            "latest_ytdlp": "2026.08.19",
            "latest_checked_at": "2026-09-18T10:00:00",
            "latest_error": "",
            "ok": True,
            "attention": False,
            "ytdlp_update": False,
            "components": [
                {
                    "id": "ytdlp",
                    "name": "YT-DLP",
                    "ok": True,
                    "status": "OK",
                    "current": "2026.08.19",
                    "detail": "Installed 2026.08.19; latest 2026.08.19",
                    "latest": "2026.08.19",
                }
            ],
        }
        with self.Session() as db:
            _write_cache(db, cached)
            with patch(
                "app.selfcheck.collect_checks", side_effect=AssertionError("live")
            ), patch(
                "app.selfcheck.fetch_latest_ytdlp", side_effect=AssertionError("github")
            ):
                report = load_selfcheck(db, live=False)
        self.assertTrue(report["ok"])
        self.assertEqual(report["components"][0]["current"], "2026.08.19")
        self.assertFalse(report["ytdlp_update"])

    def test_check_now_fetches_latest(self):
        with self.Session() as db, patch(
            "app.selfcheck.fetch_latest_ytdlp", return_value="2026.09.01"
        ), patch("app.selfcheck.collect_checks") as collect:
            collect.return_value = [
                ComponentCheck("ytdlp", "YT-DLP", True, "OK", "2026.09.01", "ok", "2026.09.01")
            ]
            report = run_selfcheck(db, refresh_latest=True)
        self.assertEqual(report["latest_ytdlp"], "2026.09.01")
        self.assertTrue(report["ok"])
        summary = selfcheck_summary(report)
        self.assertEqual(summary["status"], "OK")
        self.assertEqual(summary["components"][0]["id"], "ytdlp")


class SystemTemplateTests(unittest.TestCase):
    def setUp(self):
        from fastapi.templating import Jinja2Templates

        from app.vpn.util import format_ago

        self.templates = Jinja2Templates(directory="app/templates")
        self.templates.env.filters["ago"] = format_ago

    def _render(self, **system):
        payload = {
            "ok": False,
            "ytdlp_update": False,
            "checked_at": "2026-09-18T10:00:00",
            "latest_ytdlp": "2026.08.19",
            "latest_error": "",
            "components": [],
        }
        payload.update(system)
        return self.templates.env.get_template("system.html").render(system=payload)

    def test_timeout_is_not_duplicated(self):
        html = self._render(
            components=[
                {
                    "id": "ytdlp",
                    "name": "YT-DLP",
                    "ok": False,
                    "status": "TIMEOUT",
                    "current": "—",
                    "latest": "2026.08.19",
                    "detail": "yt-dlp did not finish in time",
                }
            ]
        )
        self.assertNotIn("timed out\n", html)
        self.assertEqual(html.count("timed out"), 0)
        self.assertIn("TIMEOUT", html)
        self.assertIn("yt-dlp did not finish in time", html)
        self.assertNotIn("yt-dlp updates require a new image", html.lower())

    def test_update_explains_how_to_rebuild(self):
        html = self._render(
            ytdlp_update=True,
            components=[
                {
                    "id": "ytdlp",
                    "name": "YT-DLP",
                    "ok": False,
                    "status": "UPDATE",
                    "current": "2026.08.19",
                    "latest": "2026.09.01",
                    "detail": "Newer yt-dlp is available.",
                }
            ]
        )
        self.assertIn("YTDLP_VERSION", html)
        self.assertIn("UPDATE", html)
        self.assertIn("2026.08.19", html)
        self.assertIn("2026.09.01", html)
