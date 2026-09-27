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
    build_download_output,
    composed_title,
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

    def test_title_from_html_strips_vk_suffix(self):
        from app.ytdlp import title_from_html

        ext = "-1_2"
        self.assertEqual(
            title_from_html(
                '<meta property="og:title" content="Real talk | VK Video">',
                ext,
            ),
            "Real talk",
        )
        self.assertEqual(
            title_from_html("<title>Real talk | VK</title>", ext),
            "Real talk",
        )
        self.assertEqual(title_from_html("<title>NA</title>", ext), "")
        self.assertEqual(
            title_from_html("<title>ВКонтакте | VK Видео</title>", ext),
            "",
        )

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
        self.assertEqual(
            title_from_entry(
                {
                    "title": "NA",
                    "description": "https://vk.com/video-123_456\nReal talk from the description\nMore",
                },
                ext,
            ),
            "Real talk from the description",
        )
        self.assertEqual(
            title_from_entry({"title": "NA", "track": "Named track"}, ext),
            "Named track",
        )

    def test_composed_title_uses_channel_date_or_id(self):
        ext = "-123_456"
        self.assertEqual(
            composed_title("NA", channel="Algebra", upload_date="20240115", external_id=ext),
            "Algebra · 2024-01-15",
        )
        self.assertEqual(
            composed_title("NA", channel="Algebra", external_id=ext),
            "Algebra",
        )
        self.assertEqual(
            composed_title("NA", upload_date="20240115", external_id=ext),
            "Video 2024-01-15",
        )
        self.assertEqual(composed_title("NA", external_id=ext), f"Video {ext}")
        self.assertEqual(
            composed_title("Lecture 4", channel="Algebra", external_id=ext),
            "Lecture 4",
        )

    def test_download_name_uses_id_or_channel_not_untitled(self):
        path = build_download_output(
            folder="Subscription",
            title="Video -214484275_456239461",
            video_id="-214484275_456239461",
            upload_date="NA",
        )
        self.assertEqual(
            path.name,
            "Video -214484275_456239461 [-214484275_456239461].%(ext)s",
        )
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

    async def test_metadata_uses_og_title_when_json_has_no_name(self):
        inspect = AsyncMock(return_value={"id": "-1_2", "title": "NA"})
        page = AsyncMock(return_value="Talk from the page")
        with patch("app.ytdlp.inspect_url", inspect), patch(
            "app.ytdlp.fetch_page_title", page
        ):
            meta = await resolve_video_metadata(
                "https://vk.com/video-1_2",
                title="NA",
                channel="Subscription",
                upload_date=None,
                external_id="-1_2",
            )
        self.assertEqual(meta["title"], "Talk from the page")
        page.assert_awaited_once()

    async def test_fetch_page_title_reads_og_title(self):
        from app.ytdlp import fetch_page_title

        class FakeResponse:
            status_code = 200
            text = '<meta property="og:title" content="Page talk | VK">'

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None, cookies=None):
                return FakeResponse()

        with patch("app.ytdlp.httpx.AsyncClient", return_value=FakeClient()):
            title = await fetch_page_title("https://vk.com/video-1_2", "-1_2")
        self.assertEqual(title, "Page talk")

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
            sub = db.get(Subscription, sub_id)
            self.assertEqual(sub.newest_video_id, "-1_1")
            self.assertEqual(sub.newest_video_title, "Real queued lecture")
            self.assertEqual(sub.newest_video_at, datetime(2024, 1, 15))

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
        inspect = AsyncMock(
            return_value={
                "title": "should not replace",
                "channel": "",
                "upload_date": "20240302",
            }
        )

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.inspect_playlist_flat",
            AsyncMock(return_value=playlist),
        ), patch("app.scheduler.resolve_video_metadata", inspect):
            from app.scheduler import scan_subscription

            await scan_subscription(sub_id, initial=True)

        inspect.assert_awaited_once()
        with self.Session() as db:
            video = db.scalar(select(Video))
            sub = db.get(Subscription, sub_id)
            self.assertEqual(video.title, "Already named")
            self.assertEqual(video.status, "QUEUED")
            self.assertEqual(sub.newest_video_id, "-1_9")
            self.assertEqual(sub.newest_video_title, "Already named")
            self.assertEqual(sub.newest_video_at, datetime(2024, 3, 2))
            self.assertIn("NEWEST 02 Mar 2024", sub.last_scan_result)

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

        inspect = AsyncMock(
            return_value={
                "title": "Newest",
                "channel": "Algebra",
                "upload_date": "20240601",
            }
        )
        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.inspect_playlist_flat",
            AsyncMock(return_value=playlist),
        ), patch("app.scheduler.resolve_video_metadata", inspect):
            from app.scheduler import scan_subscription

            await scan_subscription(sub_id, initial=True)

        inspect.assert_awaited_once()
        self.assertEqual(inspect.await_args.args[0], "https://vk.com/video-1_new")
        with self.Session() as db:
            videos = {
                v.external_id: v
                for v in db.scalars(select(Video)).all()
            }
            sub = db.get(Subscription, sub_id)
            self.assertEqual(videos["-1_new"].status, "QUEUED")
            self.assertEqual(videos["-1_mid"].status, "QUEUED")
            self.assertEqual(videos["-1_old"].status, "IGNORED_INITIAL_HISTORY")
            self.assertEqual(videos["-1_oldest"].status, "IGNORED_INITIAL_HISTORY")
            self.assertLess(videos["-1_new"].queue_rank, videos["-1_mid"].queue_rank)
            self.assertEqual(sub.newest_video_title, "Newest")
            self.assertEqual(sub.newest_video_at, datetime(2024, 6, 1))

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
            sub = db.get(Subscription, sub_id)
            self.assertEqual(sub.newest_video_id, "-1_new")
            self.assertEqual(sub.newest_video_title, "New")
            self.assertEqual(sub.newest_video_at, datetime(2024, 1, 1))
            self.assertIn("NEWEST 01 Jan 2024", sub.last_scan_result)

    async def test_known_undated_head_still_records_publish_time(self):
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
            db.flush()
            db.add(
                Video(
                    subscription_id=sub.id,
                    source="vk",
                    external_id="-1_known",
                    webpage_url="https://vk.com/video-1_known",
                    title="Already stored",
                    channel="Algebra",
                    status="COMPLETED",
                )
            )
            db.commit()
            sub_id = sub.id

        playlist = {
            "title": "Algebra",
            "entries": [
                {
                    "id": "-1_known",
                    "url": "https://vk.com/video-1_known",
                    "title": "NA",
                }
            ],
        }
        inspect = AsyncMock(
            return_value={
                "title": "КСТАТИ #113",
                "channel": "VK Видео",
                "upload_date": "20260919",
            }
        )
        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.inspect_playlist_flat",
            AsyncMock(return_value=playlist),
        ), patch("app.scheduler.resolve_video_metadata", inspect):
            from app.scheduler import scan_subscription

            await scan_subscription(sub_id)

        inspect.assert_awaited_once()
        with self.Session() as db:
            sub = db.get(Subscription, sub_id)
            self.assertEqual(sub.newest_video_title, "КСТАТИ #113")
            self.assertEqual(sub.newest_video_at, datetime(2026, 9, 19))
            self.assertIn("NEWEST 19 Sep 2026", sub.last_scan_result)
            self.assertIn("NO NEW VIDEOS", sub.last_scan_result)


class PublishedStampTests(unittest.TestCase):
    def test_unix_timestamp_keeps_clock_time(self):
        from app.scheduler import published_at_from_entry

        published = published_at_from_entry({"timestamp": 1789808400})
        self.assertEqual(published, datetime.fromtimestamp(1789808400))

    def test_line_joins_date_and_title(self):
        sub = Subscription(
            source_url="https://vk.com/playlist/-1_2",
            title="Shows",
            newest_video_id="-1_9",
            newest_video_title="КСТАТИ #113",
            newest_video_at=datetime(2026, 9, 19, 9, 0),
        )
        self.assertEqual(sub.newest_video_line(), "19 Sep 2026, 09:00 · КСТАТИ #113")

    def test_date_only_omits_midnight(self):
        sub = Subscription(
            source_url="https://vk.com/playlist/-1_2",
            title="Shows",
            newest_video_title="New",
            newest_video_at=datetime(2024, 1, 1),
        )
        self.assertEqual(sub.newest_video_line(), "01 Jan 2024 · New")


if __name__ == "__main__":
    unittest.main()
