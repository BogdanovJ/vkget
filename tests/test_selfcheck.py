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
