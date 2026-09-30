from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, inspect, select, text
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
    if "newest_video_id" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN newest_video_id VARCHAR(300) NULL"
                )
            )
        columns.add("newest_video_id")
    if "newest_video_title" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN newest_video_title VARCHAR(1000) NULL"
                )
            )
        columns.add("newest_video_title")
    if "newest_video_at" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN newest_video_at DATETIME NULL"
                )
            )
        columns.add("newest_video_at")
    if "retention_days" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN retention_days INTEGER NULL"
                )
            )
    ensure_subscription_identity(engine)
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


def _suffixed_source_key(key: str, row_id: int) -> str:
    suffix = f"#{row_id}"
    return f"{key[:500 - len(suffix)]}{suffix}"


def _source_key_is_unique(inspector) -> bool:
    try:
        indexes = inspector.get_indexes("subscriptions")
    except Exception:
        indexes = []
    for idx in indexes:
        if idx.get("unique") and list(idx.get("column_names") or []) == ["source_key"]:
            return True
    try:
        uniques = inspector.get_unique_constraints("subscriptions")
    except Exception:
        uniques = []
    for constraint in uniques:
        if list(constraint.get("column_names") or []) == ["source_key"]:
            return True
    return False


def backfill_subscription_source_keys(bind) -> None:
    """Give every subscription one identity. A repeated channel keeps a suffix."""
    from .ytdlp import subscription_source_key

    Session = sessionmaker(bind=bind, expire_on_commit=False)
    with Session() as db:
        rows = db.scalars(select_subscriptions_by_id()).all()
        used: set[str] = set()
        changed = False
        for sub in rows:
            canonical = subscription_source_key(sub.source_url or "")
            current = (sub.source_key or "").strip()
            preferred = current or canonical
            if not preferred:
                continue
            if preferred in used:
                base = canonical or preferred
                preferred = _suffixed_source_key(base, sub.id)
                bump = sub.id
                while preferred in used:
                    bump += 1
                    preferred = _suffixed_source_key(base, bump)
            if preferred != current:
                sub.source_key = preferred
                changed = True
            used.add(preferred)
        if changed:
            db.commit()


def select_subscriptions_by_id():
    from .models import Subscription

    return select(Subscription).order_by(Subscription.id.asc())


def ensure_subscription_identity(bind) -> None:
    """Add source_key on older databases and keep it unique."""
    inspector = inspect(bind)
    if "subscriptions" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("subscriptions")}
    if "source_key" not in columns:
        with bind.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN source_key VARCHAR(500) NULL"
                )
            )
        inspector.clear_cache()
    backfill_subscription_source_keys(bind)
    inspector.clear_cache()
    if _source_key_is_unique(inspector):
        return
    try:
        with bind.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_subscription_source_key "
                    "ON subscriptions (source_key)"
                )
            )
    except Exception as exc:
        print(f"vkget: subscription identity index skipped: {exc}", flush=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
