from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import delete_subscription
from app.models import Subscription, Video


class DeleteSubscriptionTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    def test_delete_removes_subscription_and_catalogue(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Algebra",
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
                    channel="Algebra",
                    status="QUEUED",
                    next_attempt_at=datetime.now(),
                )
            )
            db.commit()
            sub_id = sub.id

        with self.Session() as db:
            response = delete_subscription(sub_id, db)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/subscriptions")

        with self.Session() as db:
            self.assertIsNone(db.get(Subscription, sub_id))
            self.assertEqual(db.scalars(select(Video)).all(), [])

    def test_delete_missing_subscription_is_404(self):
        with self.Session() as db:
            with self.assertRaises(Exception) as ctx:
                delete_subscription(999, db)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delete_leaves_unrelated_rows(self):
        with self.Session() as db:
            keep = Subscription(
                source_url="https://vk.com/playlist/-9_9",
                title="Keep me",
                enabled=True,
            )
            drop = Subscription(
                source_url="https://vk.com/playlist/-1_2",
                title="Drop me",
                enabled=True,
            )
            db.add_all([keep, drop])
            db.flush()
            db.add(
                Video(
                    subscription_id=keep.id,
                    source="vk",
                    external_id="-9_9",
                    webpage_url="https://vk.com/video-9_9",
                    title="Keep lecture",
                    channel="Keep me",
                    status="QUEUED",
                )
            )
            db.add(
                Video(
                    subscription_id=drop.id,
                    source="vk",
                    external_id="-1_2",
                    webpage_url="https://vk.com/video-1_2",
                    title="Drop lecture",
                    channel="Drop me",
                    status="QUEUED",
                )
            )
            db.commit()
            keep_id = keep.id
            drop_id = drop.id

        with self.Session() as db:
            delete_subscription(drop_id, db)

        with self.Session() as db:
            self.assertIsNone(db.get(Subscription, drop_id))
            self.assertIsNotNone(db.get(Subscription, keep_id))
            videos = db.scalars(select(Video)).all()
            self.assertEqual(len(videos), 1)
            self.assertEqual(videos[0].external_id, "-9_9")


if __name__ == "__main__":
    unittest.main()
