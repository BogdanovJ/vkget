from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .filters import rejection_reason
from .models import AppState, Subscription, Video
from .notifier import notify
from .ytdlp import download_video, inspect_playlist_flat

def now():
    return datetime.now()

def jitter_minutes(low: int, high: int):
    return timedelta(minutes=random.randint(low, high))

def jitter_hours(low: int, high: int):
    return timedelta(minutes=random.randint(low * 60, high * 60))

def classify_error(error: str):
    e = (error or "").lower()

    if "429" in e or "too many requests" in e:
        return "RATE_LIMIT"
    if "403" in e or "forbidden" in e:
        return "BLOCK_OR_AUTH"
    if "cookie" in e or "login" in e or "authentication" in e:
        return "AUTH"
    if (
        "timed out" in e
        or "connection reset" in e
        or "remote end closed" in e
        or "read timeout" in e
    ):
        return "TIMEOUT"

    return "TEMPORARY"

def retry_time(attempts: int, kind: str):
    t = now()

    if kind == "RATE_LIMIT":
        return t + jitter_hours(8, 16)
    if kind in {"BLOCK_OR_AUTH", "AUTH"}:
        return t + jitter_hours(12, 24)
    if attempts <= 1:
        return t + jitter_minutes(30, 60)
    if attempts == 2:
        return t + jitter_hours(1, 3)
    if attempts == 3:
        return t + jitter_hours(4, 8)

    return t + jitter_hours(8, 14)

def set_global_cooldown(db, until: datetime):
    row = db.get(AppState, "global_cooldown_until")

    if row:
        row.value = until.isoformat()
    else:
        db.add(AppState(key="global_cooldown_until", value=until.isoformat()))

def get_global_cooldown(db):
    row = db.get(AppState, "global_cooldown_until")

    if not row or not row.value:
        return None

    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None

def storage_ok():
    root = settings.download_root

    if not os.path.isdir(root) or not os.access(root, os.W_OK):
        return False

    sentinel = os.path.join(root, ".vkget-share")
    if not os.path.isfile(sentinel):
        return False
    if not os.access(root, os.W_OK):
        return False
    
    probe = os.path.join(root, ".vkget-write-test")

    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return True
    except OSError:
        return False

def recover_interrupted_downloads():
    with SessionLocal() as db:
        jobs = db.scalars(
            select(Video).where(Video.status == "DOWNLOADING")
        ).all()

        if not jobs:
            return

        retry_at = now()

        for job in jobs:
            job.status = "FAILED_TEMPORARY"
            job.last_error = "Download interrupted by application restart"
            job.next_attempt_at = retry_at

        db.commit()

async def scan_subscription(subscription_id: int, initial: bool = False):
    with SessionLocal() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub or not sub.enabled:
            return
        source_url = sub.source_url

    try:
        data = await inspect_playlist_flat(source_url)
    except Exception as exc:
        with SessionLocal() as db:
            sub = db.get(Subscription, subscription_id)
            if sub:
                sub.last_error = str(exc)[:2000]
                sub.last_scan_at = now()
                sub.next_scan_at = now() + jitter_hours(
                    settings.discovery_min_hours,
                    settings.discovery_max_hours,
                )
                db.commit()
        return

    entries = data.get("entries") or []
    title = (
        data.get("title")
        or data.get("channel")
        or data.get("uploader")
        or "Subscription"
    )

    with SessionLocal() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return

        sub.title = title[:500]

        known = db.scalar(
            select(Video.id)
            .where(Video.subscription_id == sub.id)
            .limit(1)
        )
        is_initial = initial or known is None

        initial_allowed = set()
        if is_initial and sub.initial_last_n > 0:
            for entry in entries[-sub.initial_last_n:]:
                if entry and entry.get("id"):
                    initial_allowed.add(str(entry["id"]))

        for entry in entries:
            if not entry:
                continue

            external_id = str(entry.get("id") or "")
            webpage_url = entry.get("webpage_url") or entry.get("url")

            if not external_id or not webpage_url:
                continue

            existing = db.scalar(
                select(Video.id).where(
                    Video.source == "vk",
                    Video.external_id == external_id,
                )
            )
            if existing:
                continue

            video_title = entry.get("title") or f"Video {external_id}"
            duration = entry.get("duration")
            channel = (
                entry.get("channel")
                or entry.get("uploader")
                or title
                or "Unknown"
            )

            status = "NEW"
            ignore_reason = None

            if is_initial and external_id not in initial_allowed:
                status = "IGNORED_INITIAL_HISTORY"
                ignore_reason = "initial history"
            else:
                ignore_reason = rejection_reason(
                    video_title,
                    duration,
                    sub.min_duration_seconds,
                    sub.extra_stop_words,
                )

                if ignore_reason:
                    status = "IGNORED_FILTER"
                elif not sub.watch_future and not is_initial:
                    status = "IGNORED_MANUAL"
                    ignore_reason = "future downloads disabled"
                else:
                    status = "QUEUED"

            db.add(
                Video(
                    subscription_id=sub.id,
                    source="vk",
                    external_id=external_id,
                    webpage_url=webpage_url,
                    title=video_title[:1000],
                    channel=channel[:500],
                    duration=duration,
                    upload_date=entry.get("upload_date"),
                    status=status,
                    ignore_reason=ignore_reason,
                    next_attempt_at=(
                        now() + jitter_minutes(2, 15)
                        if status == "QUEUED"
                        else None
                    ),
                )
            )

        sub.last_scan_at = now()
        sub.last_error = None
        sub.next_scan_at = now() + jitter_hours(
            settings.discovery_min_hours,
            settings.discovery_max_hours,
        )
        db.commit()

async def run_one_download():
    with SessionLocal() as db:
        cooldown = get_global_cooldown(db)
        if cooldown and cooldown > now():
            return

        job = db.scalar(
            select(Video)
            .where(
                Video.status.in_(["QUEUED", "FAILED_TEMPORARY"]),
                Video.next_attempt_at <= now(),
            )
            .order_by(Video.next_attempt_at.asc())
            .limit(1)
        )

        if not job:
            return

        if not storage_ok():
            until = now() + jitter_hours(2, 4)
            set_global_cooldown(db, until)
            db.commit()
            await notify(
                "⚠ VKGET\n"
                "Download storage is unavailable or not writable.\n"
                f"Paused until approximately {until:%Y-%m-%d %H:%M}."
            )
            return

        job.status = "DOWNLOADING"
        job.attempts += 1
        job.next_attempt_at = None

        video_id = job.id
        url = job.webpage_url
        channel = job.channel
        title = job.title
        attempts = job.attempts

        db.commit()

    try:
        rc, final_path, log = await download_video(url, channel)
    except Exception as exc:
        retry_at = retry_time(attempts, "TRANSIENT")

        with SessionLocal() as db:
            job = db.get(Video, video_id)
            if job:
                job.status = "FAILED_TEMPORARY"
                job.last_error = f"{type(exc).__name__}: {exc}"[-4000:]
                job.next_attempt_at = retry_at
                db.commit()

        await notify(
            "⚠ VKGET\n"
            f"{title}\n"
            f"Download process failed: {type(exc).__name__}\n"
            f"Next attempt: {retry_at:%Y-%m-%d %H:%M}"
        )
        return

    with SessionLocal() as db:
        job = db.get(Video, video_id)
        if not job:
            return

        if rc == 0:
            job.status = "COMPLETED"
            job.local_path = final_path
            job.completed_at = now()
            job.last_error = None

            set_global_cooldown(
                db,
                now() + jitter_minutes(
                    settings.min_gap_minutes,
                    settings.max_gap_minutes,
                ),
            )
            db.commit()

            await notify(
                "✅ VKGET\n"
                f"{title}\n"
                f"Downloaded ≤{settings.max_height}p\n"
                f"{final_path or ''}"
            )
            return

        kind = classify_error(log)
        retry_at = retry_time(attempts, kind)

        job.status = "FAILED_TEMPORARY"
        job.last_error = log[-4000:]
        job.next_attempt_at = retry_at

        if kind in {"RATE_LIMIT", "BLOCK_OR_AUTH", "AUTH"}:
            set_global_cooldown(db, retry_at)

        db.commit()

        if kind == "RATE_LIMIT":
            await notify(
                "⚠ VKGET\n"
                "VK appears rate-limited.\n"
                f"{title}\n"
                f"Next attempt: {retry_at:%Y-%m-%d %H:%M}"
            )
        elif kind in {"BLOCK_OR_AUTH", "AUTH"}:
            await notify(
                "⚠ VKGET\n"
                "Authentication or access problem.\n"
                f"{title}\n"
                f"Next attempt: {retry_at:%Y-%m-%d %H:%M}"
            )

async def scheduler_loop():
    while True:
        try:
            current = now()

            with SessionLocal() as db:
                due_ids = [
                    sub.id
                    for sub in db.scalars(
                        select(Subscription)
                        .where(
                            Subscription.enabled == True,
                            Subscription.next_scan_at <= current,
                        )
                        .limit(3)
                    ).all()
                ]

            for sub_id in due_ids:
                await scan_subscription(sub_id)

            await run_one_download()

        except Exception as exc:
            print("scheduler error:", exc, flush=True)

        await asyncio.sleep(30)
