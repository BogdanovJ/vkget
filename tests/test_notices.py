from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Subscription, Video
from app.notifier import escape_markdown, format_video_notice, friendly_failure


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
                    "*Downloaded*",
                    "",
                    "*Lecture 4 — Linear maps*",
                    "Algebra · ≤720p",
                    "[Open](https://vkvideo.ru/video-1_2)",
                    "`/downloads/Algebra/2024-01-15 - Lecture 4 — Linear maps \\[-1\\_2].mp4`",
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
        self.assertEqual(lines[0], "*Downloaded*")
        self.assertEqual(lines[2], r"*Video -214484275\_456239461*")
        self.assertEqual(lines[3], "Downloaded ≤720p")
        self.assertIn("[Open](https://vkvideo.ru/video-214484275_456239461)", text)
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
                    "*Failed*",
                    "",
                    "*Lecture 4 — Linear maps*",
                    "Algebra",
                    "VK appears rate-limited.",
                    "[Open](https://vkvideo.ru/video-1_2)",
                    "Next attempt: 2026-09-08 21:00",
                ]
            ),
        )


    def test_markdown_escapes_title_but_not_the_link_target(self):
        text = format_video_notice(
            ok=True,
            title="Talk *a_b` [c]",
            channel="Algebra",
            height=720,
            path="/downloads/a_b/file.mp4",
            page_url="https://vk.com/video-1_2",
            external_id="-1_2",
        )
        self.assertIn(r"*Talk \*a\_b\` \[c]*", text)
        self.assertIn("[Open](https://vkvideo.ru/video-1_2)", text)
        self.assertNotIn(r"\[Open]", text)
        self.assertIn(r"`/downloads/a\_b/file.mp4`", text)
        self.assertNotIn("✅", text)
        self.assertNotIn("⚠", text)


class FriendlyFailureTests(unittest.TestCase):
    def test_disk_conflicts_are_sentences(self):
        exists = friendly_failure(
            "OSError: [Errno 17] File exists: '/downloads/_single/[-1_2].mp4'"
        )
        full = friendly_failure("OSError: [Errno 28] No space left on device")
        other = friendly_failure("ERROR: unable to download video data: HTTP Error 404")
        self.assertEqual(exists, "This video is already in the download folder.")
        self.assertEqual(full, "The download disk is full.")
        self.assertEqual(other, "unable to download video data: HTTP Error 404.")
        for text in (exists, full, other):
            self.assertNotIn("OSError", text)
            self.assertNotIn("Errno", text)
            self.assertNotIn("ERROR:", text)

    def test_blank_and_class_name_use_fallback(self):
        self.assertEqual(
            friendly_failure("OSError"),
            "The download failed and will be tried again.",
        )
        self.assertEqual(
            friendly_failure(TimeoutError()),
            "The download timed out.",
        )


class DownloadNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._vpn_db = patch("app.vpn.settings.SessionLocal", self.Session)
        self._vpn_db.start()

    def tearDown(self):
        self._vpn_db.stop()
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
        saved = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        saved.write(b"video")
        saved.close()

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
                saved.name,
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

        self.addCleanup(lambda: os.path.exists(saved.name) and os.unlink(saved.name))
        self.assertEqual(download_kwargs["title"], "Lecture 4 — Linear maps")
        self.assertEqual(download_kwargs["channel"], "Algebra")
        self.assertEqual(download_kwargs["folder"], "Algebra")
        self.assertEqual(download_kwargs["upload_date"], "20240115")
        self.assertEqual(len(notices), 1)
        self.assertIn("Lecture 4 — Linear maps", notices[0])
        self.assertIn("Algebra · ≤720p", notices[0])
        self.assertIn(escape_markdown(saved.name), notices[0])
        self.assertIn("https://vkvideo.ru/video-214484275_456239461", notices[0])
        self.assertNotIn("Video -214484275_456239461", notices[0])
        self.assertNotIn("/downloads/Subscription/", notices[0])

        with self.Session() as db:
            job = db.get(Video, video_id)
            self.assertEqual(job.status, "COMPLETED")
            self.assertEqual(job.title, "Lecture 4 — Linear maps")
            self.assertEqual(job.channel, "Algebra")
            self.assertEqual(job.upload_date, "20240115")

    async def test_success_without_a_file_is_not_completed(self):
        with self.Session() as db:
            video = Video(
                source="vk",
                external_id="-1_2",
                webpage_url="https://vk.com/video-1_2",
                title="Talk",
                channel="Channel",
                upload_date="20240115",
                status="QUEUED",
                next_attempt_at=datetime.now(),
            )
            db.add(video)
            db.commit()
            video_id = video.id

        async def fake_download(*args, **kwargs):
            return 0, None, "ok"

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.resolve_video_metadata",
            new=AsyncMock(return_value={"title": "Talk", "channel": "Channel", "upload_date": "20240115"}),
        ), patch(
            "app.scheduler.download_video", side_effect=fake_download
        ), patch("app.scheduler.notify", new=AsyncMock()), patch(
            "app.scheduler.settings"
        ) as fake_settings:
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 30
            from app.scheduler import run_one_download

            await run_one_download()

        with self.Session() as db:
            job = db.get(Video, video_id)
            self.assertEqual(job.status, "FAILED_TEMPORARY")
            self.assertFalse(job.local_path)

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

    async def test_exception_notice_is_a_sentence(self):
        with self.Session() as db:
            db.add(
                Video(
                    source="vk",
                    external_id="-1_2",
                    webpage_url="https://vk.com/video-1_2",
                    title="Lecture 4",
                    channel="Algebra",
                    upload_date="20240115",
                    status="QUEUED",
                    next_attempt_at=datetime.now(),
                )
            )
            db.commit()

        notices: list[str] = []

        async def fake_download(*args, **kwargs):
            raise OSError(17, "File exists")

        async def fake_notify(text):
            notices.append(text)

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.download_video", side_effect=fake_download
        ), patch(
            "app.scheduler.notify", side_effect=fake_notify
        ), patch(
            "app.scheduler.settings"
        ) as fake_settings, patch(
            "app.scheduler.adopt_existing_download", return_value=None
        ):
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 30
            fake_settings.download_root = "/downloads"
            from app.scheduler import run_one_download

            await run_one_download()

        self.assertEqual(len(notices), 1)
        self.assertIn("This video is already in the download folder.", notices[0])
        self.assertNotIn("OSError", notices[0])
        with self.Session() as db:
            job = db.scalar(select(Video))
            self.assertEqual(job.status, "FAILED_TEMPORARY")
            self.assertIn("File exists", job.last_error)
            self.assertNotIn("OSError", notices[0])

    async def test_temporary_failure_is_notified(self):
        with self.Session() as db:
            db.add(
                Video(
                    source="vk",
                    external_id="-1_4",
                    webpage_url="https://vk.com/video-1_4",
                    title="Lecture 5",
                    channel="Algebra",
                    upload_date="20240115",
                    status="QUEUED",
                    next_attempt_at=datetime.now(),
                )
            )
            db.commit()

        notices: list[str] = []

        async def fake_download(*args, **kwargs):
            return 1, None, "ERROR: OSError: [Errno 28] No space left on device"

        async def fake_notify(text):
            notices.append(text)

        with patch("app.scheduler.SessionLocal", self.Session), patch(
            "app.scheduler.storage_ok", return_value=True
        ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
            "app.scheduler.download_video", side_effect=fake_download
        ), patch(
            "app.scheduler.notify", side_effect=fake_notify
        ), patch(
            "app.scheduler.settings"
        ) as fake_settings, patch(
            "app.scheduler.adopt_existing_download", return_value=None
        ):
            fake_settings.max_height = 720
            fake_settings.min_gap_minutes = 15
            fake_settings.max_gap_minutes = 30
            fake_settings.download_root = "/downloads"
            from app.scheduler import run_one_download

            await run_one_download()

        self.assertEqual(len(notices), 1)
        self.assertIn("The download disk is full.", notices[0])
        self.assertIn("Next attempt:", notices[0])
        self.assertNotIn("OSError", notices[0])

    async def test_existing_file_completes_without_download(self):
        root = tempfile.mkdtemp()
        try:
            saved = Path(root) / "Manual" / "Lecture [-1_2]-2024-01-15.mp4"
            saved.parent.mkdir()
            saved.write_bytes(b"already")
            with self.Session() as db:
                db.add(
                    Video(
                        source="vk",
                        external_id="-1_2",
                        webpage_url="https://vk.com/video-1_2",
                        title="Lecture",
                        channel="Algebra",
                        upload_date="20240115",
                        status="QUEUED",
                        attempts=0,
                        next_attempt_at=datetime.now(),
                    )
                )
                db.commit()

            notices: list[str] = []

            async def fake_download(*args, **kwargs):
                raise AssertionError("yt-dlp should not run")

            async def fake_notify(text):
                notices.append(text)

            with patch("app.scheduler.SessionLocal", self.Session), patch(
                "app.scheduler.storage_ok", return_value=True
            ), patch("app.scheduler.get_global_cooldown", return_value=None), patch(
                "app.scheduler.download_video", side_effect=fake_download
            ), patch(
                "app.scheduler.notify", side_effect=fake_notify
            ), patch(
                "app.scheduler.settings"
            ) as fake_settings:
                fake_settings.max_height = 720
                fake_settings.min_gap_minutes = 15
                fake_settings.max_gap_minutes = 30
                fake_settings.download_root = root
                from app.scheduler import run_one_download

                await run_one_download()

            self.assertEqual(len(notices), 1)
            self.assertIn(escape_markdown(str(saved)), notices[0])
            self.assertTrue(notices[0].startswith("*Downloaded*"))
            with self.Session() as db:
                job = db.scalar(select(Video))
                self.assertEqual(job.status, "COMPLETED")
                self.assertEqual(job.local_path, str(saved))
                self.assertEqual(job.attempts, 0)
            self.assertEqual(saved.read_bytes(), b"already")
        finally:
            for path in sorted(Path(root).rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            os.rmdir(root)


if __name__ == "__main__":
    unittest.main()
