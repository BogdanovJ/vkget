from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Subscription, Video
from app.retention import (
    apply_retention,
    describe_retention,
    effective_retention_days,
    get_common_retention_days,
    parse_retention_form,
    release_recorded_file,
    set_common_retention_days,
)


class PolicyTests(unittest.TestCase):
    def test_blank_inherits_and_zero_keeps(self):
        self.assertIsNone(parse_retention_form(""))
        self.assertIsNone(parse_retention_form("  "))
        self.assertEqual(parse_retention_form("0"), 0)
        self.assertEqual(parse_retention_form("14"), 14)
        self.assertEqual(parse_retention_form("-3"), 0)

    def test_effective_days(self):
        self.assertEqual(effective_retention_days(14, 30), 14)
        self.assertEqual(effective_retention_days(None, 30), 30)
        self.assertIsNone(effective_retention_days(0, 30))
        self.assertIsNone(effective_retention_days(None, None))
        self.assertIsNone(effective_retention_days(None, 0))

    def test_labels(self):
        self.assertEqual(describe_retention(14, 30), "14 DAYS")
        self.assertEqual(describe_retention(None, 30), "COMMON · 30 DAYS")
        self.assertEqual(describe_retention(0, 30), "KEEP")
        self.assertEqual(describe_retention(None, None), "KEEP")


class RetentionDatabaseTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.root = Path(tempfile.mkdtemp())
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.moment = datetime(2026, 9, 23, 12, 0, 0)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.root.rmdir()

    def _video(self, db, *, sub=None, name="clip.mp4", status="COMPLETED", age_days=10, path=None):
        folder = self.root / "shows"
        folder.mkdir(exist_ok=True)
        target = folder / name
        if path is None:
            target.write_text("video")
            stored = str(target)
        else:
            stored = path
        video = Video(
            subscription_id=sub.id if sub else None,
            source="vk",
            external_id=name,
            webpage_url=f"https://vk.com/video{name}",
            title=name,
            status=status,
            local_path=stored,
            completed_at=self.moment - timedelta(days=age_days),
        )
        db.add(video)
        db.commit()
        return video, target

    def test_common_policy_round_trip(self):
        with self.Session() as db:
            self.assertIsNone(get_common_retention_days(db))
            set_common_retention_days(db, 30)
            self.assertEqual(get_common_retention_days(db), 30)
            set_common_retention_days(db, 0)
            self.assertIsNone(get_common_retention_days(db))
            set_common_retention_days(db, None)
            self.assertIsNone(get_common_retention_days(db))

    def test_override_expires_recorded_file_only(self):
        with self.Session() as db:
            sub = Subscription(source_url="https://vk.com/playlist/-1_2", title="Shows", retention_days=7)
            db.add(sub)
            db.commit()
            set_common_retention_days(db, 1)
            video, target = self._video(db, sub=sub, name="mine.mp4")
            neighbor = target.with_name("other.mp4")
            neighbor.write_text("leave me")

            expired = apply_retention(db, root=str(self.root), moment=self.moment)

            self.assertEqual(expired, 1)
            self.assertFalse(target.exists())
            self.assertTrue(neighbor.exists())
            db.refresh(video)
            self.assertEqual(video.status, "EXPIRED")
            self.assertIsNone(video.local_path)

    def test_inherit_uses_common_and_zero_keeps(self):
        with self.Session() as db:
            inherit = Subscription(source_url="https://vk.com/playlist/-1_2", title="Inherit")
            keep = Subscription(source_url="https://vk.com/playlist/-1_3", title="Keep", retention_days=0)
            db.add_all([inherit, keep])
            db.commit()
            set_common_retention_days(db, 7)
            inherited, inherited_path = self._video(db, sub=inherit, name="inherited.mp4")
            kept, kept_path = self._video(db, sub=keep, name="kept.mp4")

            apply_retention(db, root=str(self.root), moment=self.moment)

            db.refresh(inherited)
            db.refresh(kept)
            self.assertEqual(inherited.status, "EXPIRED")
            self.assertFalse(inherited_path.exists())
            self.assertEqual(kept.status, "COMPLETED")
            self.assertTrue(kept_path.exists())

    def test_unset_policy_deletes_nothing(self):
        with self.Session() as db:
            sub = Subscription(source_url="https://vk.com/playlist/-1_2", title="Shows")
            db.add(sub)
            db.commit()
            video, target = self._video(db, sub=sub)

            expired = apply_retention(db, root=str(self.root), moment=self.moment)

            self.assertEqual(expired, 0)
            self.assertTrue(target.exists())
            db.refresh(video)
            self.assertEqual(video.status, "COMPLETED")

    def test_young_file_stays(self):
        with self.Session() as db:
            sub = Subscription(source_url="https://vk.com/playlist/-1_2", retention_days=14)
            db.add(sub)
            db.commit()
            video, target = self._video(db, sub=sub, age_days=13)

            apply_retention(db, root=str(self.root), moment=self.moment)

            db.refresh(video)
            self.assertEqual(video.status, "COMPLETED")
            self.assertTrue(target.exists())

    def test_one_off_follows_common_policy(self):
        with self.Session() as db:
            set_common_retention_days(db, 7)
            video, target = self._video(db, name="once.mp4")

            apply_retention(db, root=str(self.root), moment=self.moment)

            db.refresh(video)
            self.assertEqual(video.status, "EXPIRED")
            self.assertFalse(target.exists())

    def test_missing_file_still_expires(self):
        with self.Session() as db:
            sub = Subscription(source_url="https://vk.com/playlist/-1_2", retention_days=7)
            db.add(sub)
            db.commit()
            missing = str(self.root / "shows" / "gone.mp4")
            video, _target = self._video(db, sub=sub, name="gone.mp4", path=missing)

            apply_retention(db, root=str(self.root), moment=self.moment)

            db.refresh(video)
            self.assertEqual(video.status, "EXPIRED")
            self.assertIsNone(video.local_path)

    def test_path_outside_root_is_left_alone(self):
        with self.Session() as db:
            sub = Subscription(source_url="https://vk.com/playlist/-1_2", retention_days=7)
            db.add(sub)
            db.commit()
            outside = self.root.parent / f"outside-{self.root.name}.mp4"
            outside.write_text("secret")
            self.addCleanup(outside.unlink)
            video, _target = self._video(db, sub=sub, path=str(outside))

            expired = apply_retention(db, root=str(self.root), moment=self.moment)

            self.assertEqual(expired, 0)
            self.assertTrue(outside.exists())
            db.refresh(video)
            self.assertEqual(video.status, "COMPLETED")
            self.assertEqual(video.local_path, str(outside))

    def test_relative_and_symlink_paths_are_refused(self):
        with self.Session() as db:
            sub = Subscription(source_url="https://vk.com/playlist/-1_2", retention_days=1)
            db.add(sub)
            db.commit()
            outside = self.root.parent / f"linked-{self.root.name}.mp4"
            outside.write_text("secret")
            self.addCleanup(outside.unlink)
            folder = self.root / "shows"
            folder.mkdir(exist_ok=True)
            link = folder / "link.mp4"
            link.symlink_to(outside)
            linked, _target = self._video(db, sub=sub, name="link.mp4", path=str(link))
            relative, _rel = self._video(
                db,
                sub=sub,
                name="rel.mp4",
                path="shows/rel.mp4",
            )

            self.assertFalse(release_recorded_file(str(link), str(self.root)))
            self.assertFalse(release_recorded_file("shows/rel.mp4", str(self.root)))
            apply_retention(db, root=str(self.root), moment=self.moment)

            self.assertTrue(link.is_symlink())
            self.assertTrue(outside.exists())
            db.refresh(linked)
            db.refresh(relative)
            self.assertEqual(linked.status, "COMPLETED")
            self.assertEqual(relative.status, "COMPLETED")

    def test_non_completed_rows_are_skipped(self):
        with self.Session() as db:
            set_common_retention_days(db, 1)
            video, target = self._video(db, name="queued.mp4", status="QUEUED")

            apply_retention(db, root=str(self.root), moment=self.moment)

            db.refresh(video)
            self.assertEqual(video.status, "QUEUED")
            self.assertTrue(target.exists())
