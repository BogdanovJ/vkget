from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


def engine_kwargs(url: str) -> dict:
    kwargs = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    if url.startswith("mysql"):
        kwargs["connect_args"] = {"connect_timeout": 10}
    return kwargs


engine = create_engine(settings.database_url, **engine_kwargs(settings.database_url))

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass


def reset_pool() -> None:
    engine.dispose()


async def wait_for_database(
    init,
    *,
    sleep=None,
    initial: float = 2.0,
    maximum: float = 30.0,
):
    """Run init() until the database accepts connections."""
    if sleep is None:
        sleep = asyncio.sleep
    delay = initial
    while True:
        try:
            init()
            return
        except Exception as exc:
            print(f"vkget: database not ready: {exc}", flush=True)
            reset_pool()
            await sleep(delay)
            delay = min(delay * 2, maximum)


def ensure_schema():
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    if "subscriptions" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("subscriptions")}
    if "title_is_custom" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN title_is_custom BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        columns.add("title_is_custom")
    if "last_scan_result" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE subscriptions ADD COLUMN last_scan_result TEXT NULL")
            )
        columns.add("last_scan_result")
    if "retention_days" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN retention_days INTEGER NULL"
                )
            )
    if "videos" in inspector.get_table_names():
        video_columns = {col["name"] for col in inspector.get_columns("videos")}
        if "queue_rank" not in video_columns:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE videos "
                        "ADD COLUMN queue_rank INTEGER NOT NULL DEFAULT 0"
                    )
                )
    # vpn_endpoints is left in place if it already exists. New installs use vpn_profiles.

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
