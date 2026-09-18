from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from app.scheduler import (
    SHARE_SENTINEL,
    is_mount_point,
    prepare_download_share,
    storage_ok,
)
from app.selfcheck import _storage_check


class StorageShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "downloads"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _settings(self):
        return patch("app.scheduler.settings", download_root=str(self.root))

    def test_missing_sentinel_on_unmounted_dir_is_not_ok(self):
        with self._settings(), patch("app.scheduler.is_mount_point", return_value=False):
            self.assertFalse(storage_ok())
        self.assertFalse((self.root / SHARE_SENTINEL).exists())

    def test_existing_sentinel_and_writable_dir_is_ok(self):
        (self.root / SHARE_SENTINEL).write_text("ok\n")
        with self._settings(), patch("app.scheduler.is_mount_point", return_value=False):
            self.assertTrue(storage_ok())

    def test_mount_creates_sentinel(self):
        with self._settings(), patch("app.scheduler.is_mount_point", return_value=True):
            self.assertTrue(prepare_download_share(str(self.root)))
            self.assertTrue(storage_ok())
        self.assertEqual((self.root / SHARE_SENTINEL).read_text(), "ok\n")

    def test_missing_root_is_not_ok(self):
        missing = self.root / "gone"
        with patch("app.scheduler.settings", download_root=str(missing)):
            self.assertFalse(storage_ok())

    def test_selfcheck_explains_missing_sentinel(self):
        with patch("app.scheduler.settings", download_root=str(self.root)), patch(
            "app.selfcheck.settings", download_root=str(self.root)
        ), patch("app.scheduler.is_mount_point", return_value=False):
            check = _storage_check()
        self.assertFalse(check.ok)
        self.assertEqual(check.status, "ERROR")
        self.assertIn("missing .vkget-share sentinel", check.detail)
        self.assertIn(str(self.root), check.current)

    def test_selfcheck_ok_after_mount_creates_sentinel(self):
        with patch("app.scheduler.settings", download_root=str(self.root)), patch(
            "app.selfcheck.settings", download_root=str(self.root)
        ), patch("app.scheduler.is_mount_point", return_value=True):
            check = _storage_check()
        self.assertTrue(check.ok)
        self.assertEqual(check.detail, "writable and sentinel present")
        self.assertTrue((self.root / SHARE_SENTINEL).is_file())

    def test_bind_mount_in_mountinfo_counts_as_share(self):
        mountinfo = "99 1 8:1 /mnt/downloads /downloads rw,relatime - ext4 /dev/sda1 rw\n"
        with patch("os.path.realpath", side_effect=lambda path: path), patch(
            "os.path.ismount", return_value=False
        ), patch("builtins.open", mock_open(read_data=mountinfo)):
            self.assertTrue(is_mount_point("/downloads"))
            self.assertFalse(is_mount_point("/var"))
