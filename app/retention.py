"""Delete only files this app recorded, after a retention period."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .models import AppState, Video

RETENTION_KEY = "retention_days"
MAX_RETENTION_DAYS = 36500


def positive_days(raw) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return min(value, MAX_RETENTION_DAYS)


def parse_retention_form(raw: str | None) -> int | None:
    """Blank inherits or clears. 0 keeps. A positive number is days."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    if value <= 0:
        return 0
    return min(value, MAX_RETENTION_DAYS)


def get_common_retention_days(db) -> int | None:
    row = db.get(AppState, RETENTION_KEY)
    if not row:
        return None
    return positive_days(row.value)


def set_common_retention_days(db, days: int | None) -> None:
    stored = "" if not days or days <= 0 else str(min(int(days), MAX_RETENTION_DAYS))
    row = db.get(AppState, RETENTION_KEY)
    if row:
        row.value = stored
    else:
        db.add(AppState(key=RETENTION_KEY, value=stored))
    db.commit()


def effective_retention_days(
    retention_days: int | None,
    common: int | None,
) -> int | None:
    """Positive days to keep a file. None means keep it."""
    days = common if retention_days is None else retention_days
    if days is None or days <= 0:
        return None
    return min(int(days), MAX_RETENTION_DAYS)


def describe_retention(retention_days: int | None, common: int | None) -> str:
    if retention_days is None:
        if common and common > 0:
            return f"COMMON · {common} DAYS"
        return "KEEP"
    if retention_days <= 0:
        return "KEEP"
    return f"{retention_days} DAYS"


def release_recorded_file(local_path: str | None, root: str) -> bool:
    """Unlink a recorded download inside root.

    True means the catalogue row may be expired: the file was removed, or it
    was already gone. False means the path is not a safe file to delete.
    """
    if local_path is None or not str(local_path).strip():
        return True

    path = Path(local_path)
    if not path.is_absolute():
        return False
    try:
        root_resolved = Path(root).resolve()
        resolved = path.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return False
    if resolved == root_resolved or path.is_symlink():
        return False
    if not path.exists():
        return True
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def apply_retention(
    db,
    *,
    root: str | None = None,
    moment: datetime | None = None,
) -> int:
    """Expire completed plugin downloads that are past their retention."""
    root = root or settings.download_root
    moment = moment or datetime.now()
    common = get_common_retention_days(db)
    videos = db.scalars(
        select(Video).where(
            Video.status == "COMPLETED",
            Video.completed_at.is_not(None),
        )
    ).all()

    expired = 0
    for video in videos:
        sub_days = video.subscription.retention_days if video.subscription else None
        days = effective_retention_days(sub_days, common)
        if not days:
            continue
        if video.completed_at > moment - timedelta(days=days):
            continue
        if not release_recorded_file(video.local_path, root):
            continue
        video.status = "EXPIRED"
        video.local_path = None
        expired += 1

    if expired:
        db.commit()
    return expired
