from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.db import Base, backfill_subscription_source_keys, ensure_subscription_identity
from app.main import add_subscription, delete_subscription, find_subscription_for_url
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


class AddSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    async def _add(self, url, *, name="", initial_last_n=3, scan=None):
        from unittest.mock import AsyncMock, patch

        with self.Session() as db, patch(
            "app.main.scan_subscription",
            scan if scan is not None else AsyncMock(),
        ) as scanned:
            response = await add_subscription(
                url,
                name,
                initial_last_n,
                10,
                "",
                "on",
                db,
            )
        return response, scanned

    async def test_new_channel_is_saved_and_scanned(self):
        response, scanned = await self._add("https://vk.com/@Shows/")
        self.assertEqual(response.status_code, 303)
        scanned.assert_awaited_once()
        with self.Session() as db:
            rows = db.scalars(select(Subscription)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].source_key, "vkvideo.ru/@shows")
            self.assertEqual(rows[0].source_url, "https://vk.com/@Shows")
            self.assertEqual(
                response.headers["location"],
                f"/subscriptions/{rows[0].id}",
            )
            self.assertIs(scanned.await_args.kwargs.get("initial"), True)

    async def test_same_channel_on_the_other_host_stays_one_row(self):
        first, first_scan = await self._add(
            "https://vk.com/playlist/-9_2",
            name="Algebra",
            initial_last_n=3,
        )
        second, second_scan = await self._add(
            "https://m.vkvideo.ru/video/playlist/-9_2?z=video-1_2&ref=feed",
            name="Duplicate",
            initial_last_n=10,
        )
        self.assertEqual(first.status_code, 303)
        first_scan.assert_awaited_once()
        second_scan.assert_not_awaited()
        with self.Session() as db:
            rows = db.scalars(select(Subscription)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].title, "Algebra")
            self.assertEqual(rows[0].initial_last_n, 3)
            self.assertEqual(
                second.headers["location"],
                f"/subscriptions/{rows[0].id}?already=1",
            )

    async def test_different_playlist_is_its_own_subscription(self):
        await self._add("https://vkvideo.ru/playlist/-1_2")
        await self._add("https://vk.com/playlist/-1_3")
        with self.Session() as db:
            rows = db.scalars(select(Subscription).order_by(Subscription.id.asc())).all()
            self.assertEqual(
                [row.source_key for row in rows],
                ["vkvideo.ru/playlist/-1_2", "vkvideo.ru/playlist/-1_3"],
            )

    async def test_blank_url_is_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            await self._add("   ")
        self.assertEqual(ctx.exception.status_code, 400)
        with self.Session() as db:
            self.assertEqual(db.scalars(select(Subscription)).all(), [])


class SubscriptionIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.path)

    def _insert_without_key(self, url: str, title: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO subscriptions ("
                    "source_url, source_key, title, enabled, initial_last_n, "
                    "watch_future, min_duration_seconds, extra_stop_words, "
                    "created_at, title_is_custom"
                    ") VALUES ("
                    ":url, NULL, :title, 1, 3, 1, 600, '', :created, 0"
                    ")"
                ),
                {"url": url, "title": title, "created": datetime.now()},
            )

    def test_backfill_keeps_the_oldest_channel_canonical(self):
        self._insert_without_key("https://vk.com/@shows", "First")
        self._insert_without_key("https://vkvideo.ru/video/@shows?ref=1", "Second")
        self._insert_without_key("https://vk.com/playlist/-1_9", "Other")
        backfill_subscription_source_keys(self.engine)
        with self.Session() as db:
            rows = db.scalars(select(Subscription).order_by(Subscription.id.asc())).all()
            self.assertEqual(rows[0].source_key, "vkvideo.ru/@shows")
            self.assertEqual(rows[1].source_key, f"vkvideo.ru/@shows#{rows[1].id}")
            self.assertEqual(rows[2].source_key, "vkvideo.ru/playlist/-1_9")
        ensure_subscription_identity(self.engine)
        ensure_subscription_identity(self.engine)
        with self.Session() as db:
            rows = db.scalars(select(Subscription).order_by(Subscription.id.asc())).all()
            self.assertEqual(rows[0].source_key, "vkvideo.ru/@shows")
            self.assertEqual(len(rows), 3)

    def test_missing_column_is_added(self):
        with self.engine.begin() as conn:
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name='subscriptions'")
            ).scalar()
            legacy = ddl.replace("\tsource_key VARCHAR(500), \n", "")
            legacy = legacy.replace(", \n\tUNIQUE (source_key)", "")
            self.assertNotIn("source_key", legacy)
            conn.execute(text("DROP TABLE subscriptions"))
            conn.execute(text(legacy))
            conn.execute(
                text(
                    "INSERT INTO subscriptions ("
                    "source_url, title, enabled, initial_last_n, "
                    "watch_future, min_duration_seconds, extra_stop_words, "
                    "created_at, title_is_custom"
                    ") VALUES ("
                    "'https://vk.com/playlist/-4_4', 'Legacy', 1, 3, 1, 600, '', "
                    "'2020-01-01 00:00:00', 0"
                    ")"
                )
            )
        ensure_subscription_identity(self.engine)
        with self.Session() as db:
            row = db.scalars(select(Subscription)).one()
            self.assertEqual(row.source_key, "vkvideo.ru/playlist/-4_4")
        ensure_subscription_identity(self.engine)

    def test_lookup_uses_the_saved_url_when_the_key_was_disambiguated(self):
        with self.Session() as db:
            sub = Subscription(
                source_url="https://vk.com/@shows",
                title="Shows",
                source_key="vkvideo.ru/@shows#4",
            )
            db.add(sub)
            db.commit()
            found = find_subscription_for_url(
                db,
                "https://vkvideo.ru/video/@shows?z=video-1_2",
            )
            self.assertEqual(found.id, sub.id)


if __name__ == "__main__":
    unittest.main()
