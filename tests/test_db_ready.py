from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.db import engine_kwargs, wait_for_database
from app.scheduler import (
    DB_BACKOFF_MAX_SECONDS,
    SCHEDULER_SLEEP_SECONDS,
    scheduler_loop,
)


class EngineKwargsTests(unittest.TestCase):
    def test_mysql_gets_connect_timeout(self):
        kwargs = engine_kwargs("mysql+pymysql://vkget:x@mariadb:3306/vkget")
        self.assertTrue(kwargs["pool_pre_ping"])
        self.assertEqual(kwargs["connect_args"]["connect_timeout"], 10)

    def test_sqlite_skips_mysql_connect_args(self):
        kwargs = engine_kwargs("sqlite:////tmp/vkget.db")
        self.assertNotIn("connect_args", kwargs)


class WaitForDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_until_init_succeeds(self):
        calls = {"n": 0}

        def init():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionRefusedError("Can't connect to MySQL server")

        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        with patch("app.db.reset_pool") as reset:
            await wait_for_database(init, sleep=fake_sleep, initial=2, maximum=30)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(reset.call_count, 2)


class SchedulerDatabaseBackoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_refused_disposes_pool_and_backs_off(self):
        error = OperationalError(
            "(pymysql.err.OperationalError) (2003, "
            "\"Can't connect to MySQL server on 'mariadb.mariadb.svc.cluster.local'\")",
            None,
            None,
        )
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 2:
                raise asyncio.CancelledError()

        with patch("app.scheduler.SessionLocal", side_effect=error), patch(
            "app.scheduler.reset_pool"
        ) as reset, patch("app.scheduler.asyncio.sleep", side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler_loop()

        self.assertEqual(sleeps[0], SCHEDULER_SLEEP_SECONDS)
        self.assertEqual(sleeps[1], SCHEDULER_SLEEP_SECONDS * 2)
        self.assertGreaterEqual(reset.call_count, 2)
        self.assertLessEqual(sleeps[1], DB_BACKOFF_MAX_SECONDS)
