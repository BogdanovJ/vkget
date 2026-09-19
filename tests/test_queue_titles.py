from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Subscription, Video
from app.ytdlp import (
    is_usable_channel,
    is_usable_video_title,
    resolve_video_metadata,
    resolve_video_title,
    title_from_entry,
)


class UsableTitleTests(unittest.TestCase):
    def test_rejects_url_bits_and_placeholders(self):
        ext = "-123_456"
        cases = [
            "",
            "NA",
            "n/a",
            "Untitled",
            ext,
            f"Video {ext}",
            "https://vk.com/video-123_456",
            "https://vkvideo.ru/video-123_456",
            "video-123_456",
            "playlist/-123_456",
        ]
        for title in cases:
            with self.subTest(title=title):
                self.assertFalse(is_usable_video_title(title, ext))

    def test_accepts_human_title(self):
        self.assertTrue(
            is_usable_video_title("Lecture 4 — Linear maps", "-123_456")
        )

    def test_rejects_placeholder_channels(self):
        self.assertFalse(is_usable_channel("Subscription"))
        self.assertFalse(is_usable_channel("Unknown"))
        self.assertFalse(is_usable_channel("NA"))
        self.assertFalse(is_usable_channel(""))
        self.assertTrue(is_usable_channel("Algebra channel"))

    def test_title_from_entry_prefers_usable_fields(self):
        ext = "-123_456"
        self.assertEqual(
            title_from_entry(
                {"title": "NA", "fulltitle": "Real name", "alt_title": "Alt"},
                ext,
            ),
            "Real name",
        )
        self.assertEqual(
            title_from_entry({"title": "From title", "fulltitle": "NA"}, ext),
            "From title",
        )
        self.assertEqual(
            title_from_entry(
                {"title": "-123_456", "fulltitle": "NA", "alt_title": "Alt name"},
                ext,
            ),
            "Alt name",
        )
        self.assertEqual(
            title_from_entry({"title": "NA", "fulltitle": ext}, ext),
            "",
        )


class ResolveTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_inspect_when_title_is_usable(self):
        with patch("app.ytdlp.inspect_url", new_callable=AsyncMock) as inspect:
            title = await resolve_video_title(
                "https://vk.com/video-1_2",
                "Already a real title",
                "-1_2",
            )
        self.assertEqual(title, "Already a real title")
        inspect.assert_not_called()

    async def test_inspects_when_title_is_url_bit(self):
        inspect = AsyncMock(
            return_value={"id": "-1_2", "title": "Inspected lecture"}
        )
        with patch("app.ytdlp.inspect_url", inspect):
            title = await resolve_video_title(
                "https://vk.com/video-1_2",
                "NA",
                "-1_2",
            )
        self.assertEqual(title, "Inspected lecture")
        inspect.assert_awaited_once()

    async def test_metadata_fills_title_channel_and_date(self):
        inspect = AsyncMock(
            return_value={
                "id": "-1_2",
                "title": "NA",
                "fulltitle": "Inspected lecture",
                "channel": "Algebra",
                "upload_date": "20240115",
            }
        )
        with patch("app.ytdlp.inspect_url", inspect):
            meta = await resolve_video_metadata(
                "https://vk.com/video-1_2",
                title="Video -1_2",
                channel="Subscription",
                upload_date=None,
                external_id="-1_2",
            )
        self.assertEqual(meta["title"], "Inspected lecture")
        self.assertEqual(meta["channel"], "Algebra")
        self.assertEqual(meta["upload_date"], "20240115")
        inspect.assert_awaited_once()

    async def test_metadata_uses_timestamp_when_upload_date_missing(self):
        inspect = AsyncMock(
            return_value={
                "id": "-1_2",
                "title": "Dated lecture",
                "channel": "Algebra",
                "timestamp": 1705276800,
            }
        )
        with patch("app.ytdlp.inspect_url", inspect):
            meta = await resolve_video_metadata(
                "https://vk.com/video-1_2",
                title="Dated lecture",
                channel="Algebra",
                upload_date="NA",
                external_id="-1_2",
            )
        self.assertEqual(meta["upload_date"], "20240115")

    async def test_metadata_skips_inspect_when_complete(self):
        with patch("app.ytdlp.inspect_url", new_callable=AsyncMock) as inspect:
            meta = await resolve_video_metadata(
                "https://vk.com/video-1_2",
                title="Already a real title",
                channel="Algebra",
                upload_date="20240115",
                external_id="-1_2",
            )
        self.assertEqual(meta["title"], "Already a real title")
        inspect.assert_not_called()


class ScanQueueTitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    async def test_scan_inspects_only_queued_placeholder_titles(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Algebra",
                initial_last_n=1,
                min_duration_seconds=0,
                extra_stop_words="",
                watch_future=True,
                enabled=True,
                next_scan_at=datetime.now(),
            )
            db.add(sub)
            db.commit()
            sub_id = sub.id

        playlist = {
            "title": "Algebra",
            "entries": [
                {
                    "id": "-1_1",
                    "url": "https://vk.com/video-1_1",
                    "title": "NA",
                },
                {
                    "id": "-1_2",
                    "url": "https://vk.com/video-1_2",
                    "title": "-1_2",
                },
            ],
        }
        inspected: list[str] = []

        async def fake_resolve(url, *, title="", channel="", upload_date=None, external_id=""):
            inspected.append(url)
            return {
                "title": "Real queued lecture",
                "channel": "Algebra channel",
                "upload_date": "20240115",
            }

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.inspect_playlist_flat",
            AsyncMock(return_value=playlist),
        ), patch(
            "app.scheduler.resolve_video_metadata",
            side_effect=fake_resolve,
        ):
            from app.scheduler import scan_subscription

            await scan_subscription(sub_id, initial=True)

        self.assertEqual(inspected, ["https://vk.com/video-1_1"])
        with self.Session() as db:
            videos = {
                v.external_id: v
                for v in db.scalars(select(Video)).all()
            }
            self.assertEqual(videos["-1_1"].status, "QUEUED")
            self.assertEqual(videos["-1_1"].title, "Real queued lecture")
            self.assertEqual(videos["-1_1"].channel, "Algebra channel")
            self.assertEqual(videos["-1_1"].upload_date, "20240115")
            self.assertEqual(videos["-1_1"].display_title(), "Real queued lecture")
            self.assertEqual(videos["-1_2"].status, "IGNORED_INITIAL_HISTORY")
            self.assertEqual(videos["-1_2"].title, "-1_2")

    async def test_scan_does_not_inspect_queued_human_title(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Algebra",
                initial_last_n=3,
                min_duration_seconds=0,
                extra_stop_words="",
                watch_future=True,
                enabled=True,
                next_scan_at=datetime.now(),
            )
            db.add(sub)
            db.commit()
            sub_id = sub.id

        playlist = {
            "title": "Algebra",
            "entries": [
                {
                    "id": "-1_9",
                    "url": "https://vk.com/video-1_9",
                    "title": "Already named",
                },
            ],
        }
        inspect = AsyncMock(return_value={"title": "should not run"})

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.inspect_playlist_flat",
            AsyncMock(return_value=playlist),
        ), patch("app.scheduler.resolve_video_metadata", inspect):
            from app.scheduler import scan_subscription

            await scan_subscription(sub_id, initial=True)

        inspect.assert_not_called()
        with self.Session() as db:
            video = db.scalar(select(Video))
            self.assertEqual(video.title, "Already named")
            self.assertEqual(video.status, "QUEUED")

    async def test_undated_last_n_takes_playlist_head(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Algebra",
                initial_last_n=2,
                min_duration_seconds=0,
                extra_stop_words="",
                watch_future=True,
                enabled=True,
                next_scan_at=datetime.now(),
            )
            db.add(sub)
            db.commit()
            sub_id = sub.id

        playlist = {
            "title": "Algebra",
            "entries": [
                {"id": "-1_new", "url": "https://vk.com/video-1_new", "title": "Newest"},
                {"id": "-1_mid", "url": "https://vk.com/video-1_mid", "title": "Second"},
                {"id": "-1_old", "url": "https://vk.com/video-1_old", "title": "Older"},
                {"id": "-1_oldest", "url": "https://vk.com/video-1_oldest", "title": "Oldest"},
            ],
        }

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.inspect_playlist_flat",
            AsyncMock(return_value=playlist),
        ), patch(
            "app.scheduler.resolve_video_metadata",
            new=AsyncMock(side_effect=AssertionError("should not inspect")),
        ):
            from app.scheduler import scan_subscription

            await scan_subscription(sub_id, initial=True)

        with self.Session() as db:
            videos = {
                v.external_id: v
                for v in db.scalars(select(Video)).all()
            }
            self.assertEqual(videos["-1_new"].status, "QUEUED")
            self.assertEqual(videos["-1_mid"].status, "QUEUED")
            self.assertEqual(videos["-1_old"].status, "IGNORED_INITIAL_HISTORY")
            self.assertEqual(videos["-1_oldest"].status, "IGNORED_INITIAL_HISTORY")
            self.assertLess(videos["-1_new"].queue_rank, videos["-1_mid"].queue_rank)

    async def test_dated_last_n_uses_upload_dates(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Algebra",
                initial_last_n=2,
                min_duration_seconds=0,
                extra_stop_words="",
                watch_future=True,
                enabled=True,
                next_scan_at=datetime.now(),
            )
            db.add(sub)
            db.commit()
            sub_id = sub.id

        playlist = {
            "title": "Algebra",
            "entries": [
                {
                    "id": "-1_old",
                    "url": "https://vk.com/video-1_old",
                    "title": "Old",
                    "upload_date": "20220101",
                },
                {
                    "id": "-1_mid",
                    "url": "https://vk.com/video-1_mid",
                    "title": "Mid",
                    "upload_date": "20230101",
                },
                {
                    "id": "-1_new",
                    "url": "https://vk.com/video-1_new",
                    "title": "New",
                    "upload_date": "20240101",
                },
                {
                    "id": "-1_older",
                    "url": "https://vk.com/video-1_older",
                    "title": "Older",
                    "upload_date": "20210101",
                },
            ],
        }

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.inspect_playlist_flat",
            AsyncMock(return_value=playlist),
        ), patch(
            "app.scheduler.resolve_video_metadata",
            new=AsyncMock(side_effect=AssertionError("should not inspect")),
        ):
            from app.scheduler import scan_subscription

            await scan_subscription(sub_id, initial=True)

        with self.Session() as db:
            videos = {
                v.external_id: v
                for v in db.scalars(select(Video)).all()
            }
            self.assertEqual(videos["-1_new"].status, "QUEUED")
            self.assertEqual(videos["-1_mid"].status, "QUEUED")
            self.assertEqual(videos["-1_old"].status, "IGNORED_INITIAL_HISTORY")
            self.assertEqual(videos["-1_older"].status, "IGNORED_INITIAL_HISTORY")
            self.assertLess(videos["-1_new"].queue_rank, videos["-1_mid"].queue_rank)


if __name__ == "__main__":
    unittest.main()
