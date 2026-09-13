from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AppState, Video
from app.queue import (
    delete_from_queue,
    is_queue_paused,
    list_queue,
    move_queue_item,
    pause_item,
    resume_item,
    retry_now,
    set_queue_paused,
)


def _video(external_id: str, *, status="QUEUED", rank=0, minutes=0, title=None):
    return Video(
        source="vk",
        external_id=external_id,
        webpage_url=f"https://vk.com/video{external_id}",
        title=title or f"Talk {external_id}",
        channel="Channel",
        upload_date="20240115",
        status=status,
        queue_rank=rank,
        next_attempt_at=datetime.now() + timedelta(minutes=minutes),
    )


class QueueControlTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    def test_reorder_swaps_neighbors(self):
        with self.Session() as db:
            db.add_all(
                [
                    _video("-1_1", rank=1, title="First"),
                    _video("-1_2", rank=2, title="Second"),
                    _video("-1_3", rank=3, title="Third"),
                ]
            )
            db.commit()
            second = db.query(Video).filter_by(external_id="-1_2").one()
            self.assertTrue(move_queue_item(db, second.id, -1))
            titles = [item.display_title() for item in list_queue(db)]
            self.assertEqual(titles, ["Second", "First", "Third"])
            self.assertEqual([item.queue_rank for item in list_queue(db)], [1, 2, 3])

    def test_retry_now_moves_to_front(self):
        with self.Session() as db:
            later = _video("-1_9", rank=2, minutes=60, status="FAILED_TEMPORARY")
            db.add_all([_video("-1_1", rank=1, title="Front"), later])
            db.commit()
            db.refresh(later)
            self.assertTrue(retry_now(db, later))
            items = list_queue(db)
            self.assertEqual(items[0].external_id, "-1_9")
            self.assertEqual(items[0].status, "QUEUED")
            self.assertLessEqual(items[0].next_attempt_at, datetime.now())

    def test_pause_and_resume_item(self):
        with self.Session() as db:
            video = _video("-1_4", rank=1)
            db.add(video)
            db.commit()
            db.refresh(video)
            self.assertTrue(pause_item(db, video))
            self.assertEqual(video.status, "PAUSED")
            self.assertTrue(resume_item(db, video))
            self.assertEqual(video.status, "QUEUED")

    def test_delete_ignores_without_removing_row(self):
        with self.Session() as db:
            video = _video("-1_5", rank=1)
            db.add(video)
            db.commit()
            db.refresh(video)
            video_id = video.id
            self.assertTrue(delete_from_queue(db, video))
            row = db.get(Video, video_id)
            self.assertEqual(row.status, "IGNORED_MANUAL")
            self.assertEqual(list_queue(db), [])

    def test_global_pause_flag(self):
        with self.Session() as db:
            self.assertFalse(is_queue_paused(db))
            set_queue_paused(db, True)
            self.assertTrue(is_queue_paused(db))
            set_queue_paused(db, False)
            self.assertFalse(is_queue_paused(db))


class QueueSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    async def test_global_pause_skips_download(self):
        with self.Session() as db:
            db.add(_video("-1_1", rank=1))
            db.add(AppState(key="queue_paused", value="true"))
            db.commit()

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.download_with_vpn", new=AsyncMock()) as download:
            from app.scheduler import run_one_download

            ran = await run_one_download()
        self.assertFalse(ran)
        download.assert_not_called()

    async def test_paused_item_is_skipped_for_due_neighbor(self):
        with self.Session() as db:
            db.add(_video("-1_1", rank=1, status="PAUSED", title="Paused"))
            db.add(_video("-1_2", rank=2, title="Ready"))
            db.commit()

        seen: list[str] = []

        async def fake_download(download_fn, url, *args, **kwargs):
            seen.append(url)
            return 0, "/tmp/ok.mp4", "ok"

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.download_with_vpn", side_effect=fake_download
        ), patch("app.scheduler.notify", new=AsyncMock()), patch(
            "app.scheduler.settings"
        ) as fake_settings:
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 15
            from app.scheduler import run_one_download

            await run_one_download()

        self.assertEqual(seen, ["https://vk.com/video-1_2"])

    async def test_rank_beats_later_due_time(self):
        with self.Session() as db:
            db.add(_video("-1_late", rank=2, minutes=-5, title="Later rank"))
            db.add(_video("-1_first", rank=1, minutes=0, title="First rank"))
            db.commit()

        seen: list[str] = []

        async def fake_download(download_fn, url, *args, **kwargs):
            seen.append(url)
            return 0, "/tmp/ok.mp4", "ok"

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.download_with_vpn", side_effect=fake_download
        ), patch("app.scheduler.notify", new=AsyncMock()), patch(
            "app.scheduler.settings"
        ) as fake_settings:
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 15
            from app.scheduler import run_one_download

            await run_one_download()

        self.assertEqual(seen, ["https://vk.com/video-1_first"])
