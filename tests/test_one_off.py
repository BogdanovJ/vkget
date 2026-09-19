from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import add_one_off
from app.models import Video
from app.queue import list_queue, pop_queue_flash


def _inspect(external_id="-220754053_456246718", title="One off talk"):
    return {
        "id": external_id,
        "webpage_url": f"https://vkvideo.ru/video{external_id}",
        "title": title,
        "channel": "Lectures",
        "upload_date": "20240115",
    }


def _video(external_id, *, status="QUEUED", title=None, ignore_reason=None):
    return Video(
        source="vk",
        external_id=external_id,
        webpage_url=f"https://vk.com/video{external_id}",
        title=title or f"Talk {external_id}",
        channel="Channel",
        upload_date="20240115",
        status=status,
        ignore_reason=ignore_reason,
        queue_rank=1,
        next_attempt_at=datetime.now(),
    )


class OneOffQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    async def test_new_url_is_queued_at_front(self):
        with self.Session() as db:
            db.add(_video("-1_old", title="Already waiting"))
            db.commit()

        with self.Session() as db, patch(
            "app.main.inspect_url",
            AsyncMock(return_value=_inspect()),
        ):
            response = await add_one_off(
                "https://vkvideo.ru/video-220754053_456246718",
                db,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/queue")
        with self.Session() as db:
            items = list_queue(db)
            self.assertEqual(
                [item.external_id for item in items],
                ["-220754053_456246718", "-1_old"],
            )
            self.assertEqual(items[0].status, "QUEUED")
            self.assertEqual(pop_queue_flash(db), "Queued: One off talk")

    async def test_ignored_filter_is_requeued(self):
        with self.Session() as db:
            db.add(
                _video(
                    "-220754053_456246718",
                    status="IGNORED_FILTER",
                    title="Too short",
                    ignore_reason="duration",
                )
            )
            db.commit()

        with self.Session() as db, patch(
            "app.main.inspect_url",
            AsyncMock(return_value=_inspect(title="Requested again")),
        ):
            await add_one_off(
                "https://vkvideo.ru/video-220754053_456246718",
                db,
            )

        with self.Session() as db:
            video = db.scalar(select(Video))
            self.assertEqual(video.status, "QUEUED")
            self.assertIsNone(video.ignore_reason)
            self.assertEqual(list_queue(db), [video])
            self.assertEqual(pop_queue_flash(db), "Queued: Too short")

    async def test_ignored_initial_history_is_requeued(self):
        with self.Session() as db:
            db.add(
                _video(
                    "-220754053_456246718",
                    status="IGNORED_INITIAL_HISTORY",
                    title="Old lecture",
                    ignore_reason="initial history",
                )
            )
            db.commit()

        with self.Session() as db, patch(
            "app.main.inspect_url",
            AsyncMock(return_value=_inspect()),
        ):
            await add_one_off(
                "https://vkvideo.ru/video-220754053_456246718",
                db,
            )

        with self.Session() as db:
            video = db.scalar(select(Video))
            self.assertEqual(video.status, "QUEUED")
            self.assertEqual([item.id for item in list_queue(db)], [video.id])

    async def test_completed_is_requeued(self):
        with self.Session() as db:
            db.add(
                _video(
                    "-220754053_456246718",
                    status="COMPLETED",
                    title="Already saved",
                )
            )
            db.commit()

        with self.Session() as db, patch(
            "app.main.inspect_url",
            AsyncMock(return_value=_inspect()),
        ):
            await add_one_off(
                "https://vkvideo.ru/video-220754053_456246718",
                db,
            )

        with self.Session() as db:
            video = db.scalar(select(Video))
            self.assertEqual(video.status, "QUEUED")
            self.assertEqual(list_queue(db), [video])
            self.assertTrue(pop_queue_flash(db).startswith("Queued:"))

    async def test_downloading_is_left_unchanged(self):
        with self.Session() as db:
            db.add(
                _video(
                    "-220754053_456246718",
                    status="DOWNLOADING",
                    title="In flight",
                )
            )
            db.commit()

        with self.Session() as db, patch(
            "app.main.inspect_url",
            AsyncMock(return_value=_inspect()),
        ):
            response = await add_one_off(
                "https://vkvideo.ru/video-220754053_456246718",
                db,
            )

        self.assertEqual(response.headers["location"], "/queue")
        with self.Session() as db:
            video = db.scalar(select(Video))
            self.assertEqual(video.status, "DOWNLOADING")
            self.assertEqual(pop_queue_flash(db), "Already downloading: In flight")


if __name__ == "__main__":
    unittest.main()
