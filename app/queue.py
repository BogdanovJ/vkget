from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from .models import AppState, Video

PAUSED_KEY = "queue_paused"
ACTIVE_STATUSES = ("QUEUED", "DOWNLOADING", "FAILED_TEMPORARY", "PAUSED")
RUNNABLE_STATUSES = ("QUEUED", "FAILED_TEMPORARY")


def now() -> datetime:
    return datetime.now()


def queue_order():
    return (Video.queue_rank.asc(), Video.next_attempt_at.asc(), Video.id.asc())


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_queue_paused(db) -> bool:
    row = db.get(AppState, PAUSED_KEY)
    return bool(row and _truthy(row.value))


def set_queue_paused(db, paused: bool) -> None:
    row = db.get(AppState, PAUSED_KEY)
    value = "true" if paused else "false"
    if row:
        row.value = value
    else:
        db.add(AppState(key=PAUSED_KEY, value=value))
    db.commit()


def next_queue_rank(db) -> int:
    current = db.scalar(
        select(func.max(Video.queue_rank)).where(Video.status.in_(ACTIVE_STATUSES))
    )
    return int(current or 0) + 1


def list_queue(db, limit: int = 200) -> list[Video]:
    return list(
        db.scalars(
            select(Video)
            .where(Video.status.in_(ACTIVE_STATUSES))
            .order_by(*queue_order())
            .limit(limit)
        ).all()
    )


def _renumber(items: list[Video]) -> None:
    for rank, item in enumerate(items, start=1):
        item.queue_rank = rank


def move_queue_item(db, video_id: int, delta: int) -> bool:
    items = list_queue(db)
    index = next((i for i, item in enumerate(items) if item.id == video_id), None)
    if index is None:
        return False
    target = index + delta
    if target < 0 or target >= len(items):
        return False
    items[index], items[target] = items[target], items[index]
    _renumber(items)
    db.commit()
    return True


def retry_now(db, video: Video) -> bool:
    if not video or video.status == "DOWNLOADING":
        return False
    video.status = "QUEUED"
    video.next_attempt_at = now()
    video.ignore_reason = None
    items = list_queue(db)
    rest = [item for item in items if item.id != video.id]
    downloading = [item for item in rest if item.status == "DOWNLOADING"]
    waiting = [item for item in rest if item.status != "DOWNLOADING"]
    _renumber(downloading + [video] + waiting)
    db.commit()
    return True


def pause_item(db, video: Video) -> bool:
    if not video or video.status in {"DOWNLOADING", "PAUSED"}:
        return False
    if video.status not in RUNNABLE_STATUSES:
        return False
    video.status = "PAUSED"
    db.commit()
    return True


def resume_item(db, video: Video) -> bool:
    if not video or video.status != "PAUSED":
        return False
    video.status = "QUEUED"
    if video.next_attempt_at is None:
        video.next_attempt_at = now()
    db.commit()
    return True


def delete_from_queue(db, video: Video) -> bool:
    if not video or video.status == "DOWNLOADING":
        return False
    video.status = "IGNORED_MANUAL"
    video.ignore_reason = "manual"
    db.commit()
    return True
