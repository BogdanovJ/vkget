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
from app.notifier import format_video_notice


class FormatVideoNoticeTests(unittest.TestCase):
    def test_success_includes_name_channel_url_and_path(self):
        text = format_video_notice(
            ok=True,
            title="Lecture 4 — Linear maps",
            channel="Algebra",
            height=720,
            path="/downloads/Algebra/2024-01-15 - Lecture 4 — Linear maps [-1_2].mp4",
            page_url="https://vk.com/video-1_2",
            external_id="-1_2",
        )
        self.assertEqual(
            text,
            "\n".join(
                [
                    "✅ VKGET",
                    "Lecture 4 — Linear maps",
                    "Algebra · ≤720p",
                    "https://vkvideo.ru/video-1_2",
                    "/downloads/Algebra/2024-01-15 - Lecture 4 — Linear maps [-1_2].mp4",
                ]
            ),
        )

    def test_hides_url_bit_title_and_placeholder_channel(self):
        text = format_video_notice(
            ok=True,
            title="Video -214484275_456239461",
            channel="Subscription",
            height=720,
            path="/downloads/Named playlist/2024-01-15 - Untitled [-214484275_456239461].mp4",
            page_url="https://vk.com/video-214484275_456239461",
            external_id="-214484275_456239461",
        )
        lines = text.splitlines()
        self.assertEqual(lines[0], "✅ VKGET")
        self.assertEqual(lines[1], "Untitled")
        self.assertEqual(lines[2], "Downloaded ≤720p")
        self.assertIn("https://vkvideo.ru/video-214484275_456239461", text)
        self.assertNotIn("Subscription ·", text)

    def test_failure_includes_name_url_and_retry(self):
        retry = datetime(2026, 9, 8, 21, 0)
        text = format_video_notice(
            ok=False,
            title="Lecture 4 — Linear maps",
            channel="Algebra",
            page_url="https://vk.com/video-1_2",
            detail="VK appears rate-limited.",
            retry_at=retry,
            external_id="-1_2",
        )
        self.assertEqual(
            text,
            "\n".join(
                [
                    "⚠ VKGET",
                    "Lecture 4 — Linear maps",
                    "Algebra",
                    "VK appears rate-limited.",
                    "https://vkvideo.ru/video-1_2",
                    "Next attempt: 2026-09-08 21:00",
                ]
            ),
        )


class DownloadNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    async def test_download_enriches_names_then_notifies(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Algebra",
                enabled=True,
            )
            db.add(sub)
            db.flush()
            video = Video(
                subscription_id=sub.id,
                source="vk",
                external_id="-214484275_456239461",
                webpage_url="https://vk.com/video-214484275_456239461",
                title="Video -214484275_456239461",
                channel="Subscription",
                upload_date=None,
                status="QUEUED",
                next_attempt_at=datetime.now(),
            )
            db.add(video)
            db.commit()
            video_id = video.id

        notices: list[str] = []
        download_kwargs: dict = {}

        async def fake_meta(url, **kwargs):
            return {
                "title": "Lecture 4 — Linear maps",
                "channel": "Algebra",
                "upload_date": "20240115",
            }

        async def fake_download(url, channel, title="", video_id="", upload_date=None, folder=None):
            download_kwargs.update(
                {
                    "url": url,
                    "channel": channel,
                    "title": title,
                    "video_id": video_id,
                    "upload_date": upload_date,
                    "folder": folder,
                }
            )
            return (
                0,
                "/downloads/Algebra/2024-01-15 - Lecture 4 — Linear maps [-214484275_456239461].mp4",
                "ok",
            )

        async def fake_notify(text):
            notices.append(text)

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.resolve_video_metadata", side_effect=fake_meta
        ), patch(
            "app.scheduler.download_video", side_effect=fake_download
        ), patch(
            "app.scheduler.notify", side_effect=fake_notify
        ), patch(
            "app.scheduler.settings"
        ) as fake_settings:
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 30
            from app.scheduler import run_one_download

            await run_one_download()

        self.assertEqual(download_kwargs["title"], "Lecture 4 — Linear maps")
        self.assertEqual(download_kwargs["channel"], "Algebra")
        self.assertEqual(download_kwargs["folder"], "Algebra")
        self.assertEqual(download_kwargs["upload_date"], "20240115")
        self.assertEqual(len(notices), 1)
        self.assertIn("Lecture 4 — Linear maps", notices[0])
        self.assertIn("Algebra · ≤720p", notices[0])
        self.assertIn("https://vkvideo.ru/video-214484275_456239461", notices[0])
        self.assertNotIn("Video -214484275_456239461", notices[0])
        self.assertNotIn("/downloads/Subscription/", notices[0])

        with self.Session() as db:
            job = db.get(Video, video_id)
            self.assertEqual(job.status, "COMPLETED")
            self.assertEqual(job.title, "Lecture 4 — Linear maps")
            self.assertEqual(job.channel, "Algebra")
            self.assertEqual(job.upload_date, "20240115")

    async def test_download_uses_subscription_name_when_channel_missing(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Named playlist",
                enabled=True,
            )
            db.add(sub)
            db.flush()
            db.add(
                Video(
                    subscription_id=sub.id,
                    source="vk",
                    external_id="-1_9",
                    webpage_url="https://vk.com/video-1_9",
                    title="Already named lecture",
                    channel="Subscription",
                    upload_date="20240115",
                    status="QUEUED",
                    next_attempt_at=datetime.now(),
                )
            )
            db.commit()

        download_kwargs: dict = {}

        async def fake_meta(url, **kwargs):
            return {
                "title": kwargs.get("title") or "",
                "channel": kwargs.get("channel") or "",
                "upload_date": kwargs.get("upload_date") or "",
            }

        async def fake_download(url, channel, title="", video_id="", upload_date=None, folder=None):
            download_kwargs["channel"] = channel
            download_kwargs["title"] = title
            download_kwargs["folder"] = folder
            return 0, "/downloads/Named playlist/file.mp4", "ok"

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.resolve_video_metadata", side_effect=fake_meta
        ), patch(
            "app.scheduler.download_video", side_effect=fake_download
        ), patch(
            "app.scheduler.notify", new_callable=AsyncMock
        ), patch(
            "app.scheduler.settings"
        ) as fake_settings:
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 30
            from app.scheduler import run_one_download

            await run_one_download()

        self.assertEqual(download_kwargs["folder"], "Named playlist")
        self.assertEqual(download_kwargs["channel"], "Named playlist")
        self.assertEqual(download_kwargs["title"], "Already named lecture")

        with self.Session() as db:
            job = db.scalar(select(Video))
            self.assertEqual(job.channel, "Named playlist")

    async def test_download_folder_stays_on_subscription_when_uploader_differs(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Algebra course",
                enabled=True,
            )
            db.add(sub)
            db.flush()
            db.add(
                Video(
                    subscription_id=sub.id,
                    source="vk",
                    external_id="-1_2",
                    webpage_url="https://vk.com/video-1_2",
                    title="Lecture 4",
                    channel="VK Uploader",
                    upload_date="20240115",
                    status="QUEUED",
                    next_attempt_at=datetime.now(),
                )
            )
            db.commit()

        download_kwargs: dict = {}

        async def fake_download(url, channel, title="", video_id="", upload_date=None, folder=None):
            download_kwargs.update(
                {"channel": channel, "title": title, "folder": folder}
            )
            return 0, "/downloads/Algebra course/2024-01-15 - Lecture 4 [-1_2].mp4", "ok"

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.download_video", side_effect=fake_download
        ), patch(
            "app.scheduler.notify", new_callable=AsyncMock
        ), patch(
            "app.scheduler.settings"
        ) as fake_settings:
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 30
            from app.scheduler import run_one_download

            await run_one_download()

        self.assertEqual(download_kwargs["folder"], "Algebra course")
        self.assertEqual(download_kwargs["channel"], "VK Uploader")
        self.assertEqual(download_kwargs["title"], "Lecture 4")


if __name__ == "__main__":
    unittest.main()
