from __future__ import annotations

import asyncio
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import env_int
from app.scheduler import claim_download, jitter_hours, jitter_minutes


class JitterTests(unittest.TestCase):
    def test_equal_bounds_are_that_value(self):
        self.assertEqual(jitter_minutes(15, 15), timedelta(minutes=15))
        self.assertEqual(jitter_hours(3, 3), timedelta(hours=3))

    def test_inverted_bounds_are_swapped(self):
        gap = jitter_minutes(30, 15)
        self.assertGreaterEqual(gap, timedelta(minutes=15))
        self.assertLessEqual(gap, timedelta(minutes=30))
        hours = jitter_hours(6, 3)
        self.assertGreaterEqual(hours, timedelta(hours=3))
        self.assertLessEqual(hours, timedelta(hours=6))


class ConfigParseTests(unittest.TestCase):
    def test_non_integer_exits_with_the_variable_name(self):
        with patch.dict(os.environ, {"MAX_HEIGHT": "abc"}):
            with self.assertRaises(SystemExit) as ctx:
                env_int("MAX_HEIGHT", 720, minimum=144, maximum=720)
        self.assertIn("MAX_HEIGHT", str(ctx.exception))

    def test_empty_keeps_the_default_and_zero_is_clamped(self):
        with patch.dict(os.environ, {"MAX_HEIGHT": ""}):
            self.assertEqual(env_int("MAX_HEIGHT", 720, minimum=144, maximum=720), 720)
        with patch.dict(os.environ, {"MAX_HEIGHT": "0"}):
            self.assertEqual(env_int("MAX_HEIGHT", 720, minimum=144, maximum=720), 144)
        with patch.dict(os.environ, {"MAX_HEIGHT": "1080"}):
            self.assertEqual(env_int("MAX_HEIGHT", 720, minimum=144, maximum=720), 720)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db import Base
        from app.models import Video

        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.Video = Video

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    def test_second_claim_loses(self):
        with self.Session() as db:
            video = self.Video(
                source="vk",
                external_id="-1_2",
                webpage_url="https://vk.com/video-1_2",
                title="Talk",
                channel="Channel",
                status="QUEUED",
                attempts=0,
            )
            db.add(video)
            db.commit()
            video_id = video.id
            first = claim_download(db, video_id)
            second = claim_download(db, video_id)
            row = db.get(self.Video, video_id)
        self.assertEqual(first, 1)
        self.assertIsNone(second)
        self.assertEqual(row.status, "DOWNLOADING")
        self.assertEqual(row.attempts, 1)


class ManifestTests(unittest.TestCase):
    def test_storage_selector_is_under_node_selector(self):
        text = (Path(__file__).resolve().parents[1] / "k8s" / "deployment.yaml").read_text()
        self.assertIn("nodeSelector:\n        vkget-storage: \"true\"", text)
        self.assertNotRegex(text, r"(?m)^      vkget-storage:")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_starts_one_scheduler_task_and_cancels_it(self):
        from app.main import app

        started: list[asyncio.Task] = []

        async def fake_background():
            started.append(asyncio.current_task())
            await asyncio.Event().wait()

        with patch("app.main._run_background", fake_background), patch(
            "app.main.prepare_download_share"
        ):
            with TestClient(app) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                self.assertEqual(len(started), 1)
                self.assertFalse(started[0].done())
            self.assertTrue(started[0].done())
            self.assertTrue(started[0].cancelled())

    def test_readyz_runs_select_1(self):
        from sqlalchemy import create_engine

        from app.main import app

        engine = create_engine("sqlite://")
        async def idle():
            await asyncio.Event().wait()

        with patch("app.main.engine", engine), patch(
            "app.main._run_background", idle
        ), patch("app.main.prepare_download_share"):
            with TestClient(app) as client:
                self.assertEqual(client.get("/readyz").json(), {"ok": True})
                with patch.object(engine, "connect", side_effect=RuntimeError("down")):
                    self.assertEqual(client.get("/readyz").status_code, 503)
