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
from .notifier import format_video_notice, notify
from .ytdlp import (
    PLACEHOLDER_TITLES,
    download_video,
    inspect_playlist_flat,
    is_usable_channel,
    is_usable_video_title,
    label_from_url,
    resolve_video_metadata,
    title_from_entry,
)

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

def scan_summary(
    *,
    entries: list,
    skipped_incomplete: int,
    skipped_existing: int,
    queued: int,
    ignored: int,
) -> str:
    if not entries:
        return "EMPTY PLAYLIST"
    if queued == 0 and ignored == 0 and skipped_incomplete == 0:
        if skipped_existing:
            return f"NO NEW VIDEOS · {skipped_existing} already known"
        return "NO NEW VIDEOS"
    parts = []
    if queued:
        parts.append(f"QUEUED {queued}")
    if ignored:
        parts.append(f"IGNORED {ignored}")
    if skipped_existing:
        parts.append(f"KNOWN {skipped_existing}")
    if skipped_incomplete:
        parts.append(f"SKIPPED {skipped_incomplete}")
    if queued:
        return " · ".join(parts)
    return "NO NEW DOWNLOADS · " + " · ".join(parts)

async def scan_subscription(subscription_id: int, initial: bool = False):
    with SessionLocal() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return
        if not sub.enabled:
            sub.last_scan_result = "SKIPPED · DISABLED"
            db.commit()
            return
        source_url = sub.source_url

    try:
        data = await inspect_playlist_flat(source_url)
    except Exception as exc:
        short = str(exc).strip().splitlines()[0][:180] or "scan error"
        with SessionLocal() as db:
            sub = db.get(Subscription, subscription_id)
            if sub:
                if (sub.title or "").strip() in PLACEHOLDER_TITLES:
                    sub.title = label_from_url(sub.source_url)
                sub.last_error = str(exc)[:2000]
                sub.last_scan_result = f"FAILED · {short}"
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

        if not sub.has_custom_title():
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

        skipped_incomplete = 0
        skipped_existing = 0
        queued = 0
        ignored = 0
        new_videos: list[dict] = []
        inspect_indexes: list[int] = []

        for entry in entries:
            if not entry:
                skipped_incomplete += 1
                continue

            external_id = str(entry.get("id") or "")
            webpage_url = entry.get("webpage_url") or entry.get("url")

            if not external_id or not webpage_url:
                skipped_incomplete += 1
                continue

            existing = db.scalar(
                select(Video.id).where(
                    Video.source == "vk",
                    Video.external_id == external_id,
                )
            )
            if existing:
                skipped_existing += 1
                continue

            video_title = title_from_entry(entry, external_id)
            if not video_title:
                raw = (
                    entry.get("title")
                    or entry.get("fulltitle")
                    or entry.get("alt_title")
                    or ""
                )
                video_title = str(raw).strip() or f"Video {external_id}"
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

            if status == "QUEUED":
                queued += 1
                if not is_usable_video_title(video_title, external_id):
                    inspect_indexes.append(len(new_videos))
            else:
                ignored += 1

            new_videos.append(
                {
                    "subscription_id": sub.id,
                    "source": "vk",
                    "external_id": external_id,
                    "webpage_url": webpage_url,
                    "title": video_title[:1000],
                    "channel": channel[:500],
                    "duration": duration,
                    "upload_date": entry.get("upload_date"),
                    "status": status,
                    "ignore_reason": ignore_reason,
                    "next_attempt_at": (
                        now() + jitter_minutes(2, 15)
                        if status == "QUEUED"
                        else None
                    ),
                }
            )

        scan_at = now()
        result = scan_summary(
            entries=entries,
            skipped_incomplete=skipped_incomplete,
            skipped_existing=skipped_existing,
            queued=queued,
            ignored=ignored,
        )
        next_at = now() + jitter_hours(
            settings.discovery_min_hours,
            settings.discovery_max_hours,
        )
        db.commit()

    for index in inspect_indexes:
        row = new_videos[index]
        meta = await resolve_video_metadata(
            row["webpage_url"],
            title=row["title"],
            channel=row["channel"],
            upload_date=row.get("upload_date"),
            external_id=row["external_id"],
        )
        if is_usable_video_title(meta["title"], row["external_id"]):
            row["title"] = meta["title"][:1000]
        if is_usable_channel(meta["channel"]):
            row["channel"] = meta["channel"][:500]
        if meta.get("upload_date"):
            row["upload_date"] = meta["upload_date"]

    with SessionLocal() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return
        for row in new_videos:
            db.add(Video(**row))
        sub.last_scan_at = scan_at
        sub.last_error = None
        sub.last_scan_result = result
        sub.next_scan_at = next_at
        db.commit()

def _needs_metadata_inspect(job: Video) -> bool:
    return (
        not is_usable_video_title(job.title, job.external_id)
        or not is_usable_channel(job.channel)
        or not (job.upload_date or "").strip()
    )


def _apply_resolved_metadata(
    job: Video,
    meta: dict,
    *,
    folder_fallback: str = "",
) -> None:
    title = (meta.get("title") or "").strip()
    if is_usable_video_title(title, job.external_id):
        job.title = title[:1000]
    channel = (meta.get("channel") or "").strip()
    if is_usable_channel(channel):
        job.channel = channel[:500]
    elif folder_fallback.strip() and not is_usable_channel(job.channel):
        job.channel = folder_fallback.strip()[:500]
    date = (meta.get("upload_date") or "").strip()
    if date:
        job.upload_date = date


async def resolve_placeholder_queued_titles(limit: int = 1):
    """Fill real titles/channel/date for queued rows that still store URL bits."""
    with SessionLocal() as db:
        jobs = db.scalars(
            select(Video)
            .where(Video.status.in_(["QUEUED", "FAILED_TEMPORARY"]))
            .order_by(Video.next_attempt_at.asc())
            .limit(50)
        ).all()
        targets = []
        for job in jobs:
            if not _needs_metadata_inspect(job):
                continue
            targets.append(
                (
                    job.id,
                    job.webpage_url,
                    job.title,
                    job.channel,
                    job.upload_date,
                    job.external_id,
                )
            )
            if len(targets) >= limit:
                break

    for video_id, url, title, channel, upload_date, external_id in targets:
        meta = await resolve_video_metadata(
            url,
            title=title,
            channel=channel,
            upload_date=upload_date,
            external_id=external_id,
        )
        with SessionLocal() as db:
            job = db.get(Video, video_id)
            if job and job.status in {"QUEUED", "FAILED_TEMPORARY"}:
                _apply_resolved_metadata(job, meta)
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

        video_id = job.id
        url = job.webpage_url
        channel = job.channel
        title = job.title
        external_id = job.external_id
        upload_date = job.upload_date
        sub = job.subscription
        sub_label = sub.display_title() if sub else ""
        needs_inspect = _needs_metadata_inspect(job)

    meta = {
        "title": (title or "").strip(),
        "channel": (channel or "").strip(),
        "upload_date": (upload_date or "").strip(),
    }
    if needs_inspect:
        meta = await resolve_video_metadata(
            url,
            title=title,
            channel=channel,
            upload_date=upload_date,
            external_id=external_id,
        )

    with SessionLocal() as db:
        job = db.get(Video, video_id)
        if not job or job.status not in {"QUEUED", "FAILED_TEMPORARY"}:
            return

        _apply_resolved_metadata(job, meta, folder_fallback=sub_label)
        job.status = "DOWNLOADING"
        job.attempts += 1
        job.next_attempt_at = None

        url = job.webpage_url
        channel = job.channel
        title = job.title
        external_id = job.external_id
        upload_date = job.upload_date
        attempts = job.attempts
        notice_title = job.display_title()
        db.commit()

    download_title = (
        title if is_usable_video_title(title, external_id) else "Untitled"
    )

    try:
        rc, final_path, log = await download_video(
            url,
            channel,
            title=download_title,
            video_id=external_id,
            upload_date=upload_date,
        )
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
            format_video_notice(
                ok=False,
                title=notice_title,
                channel=channel,
                page_url=url,
                detail=f"Download process failed: {type(exc).__name__}",
                retry_at=retry_at,
                external_id=external_id,
            )
        )
        return

    with SessionLocal() as db:
        job = db.get(Video, video_id)
        if not job:
            return

        notice_title = job.display_title()
        channel = job.channel
        url = job.webpage_url
        external_id = job.external_id

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
                format_video_notice(
                    ok=True,
                    title=notice_title,
                    channel=channel,
                    height=settings.max_height,
                    path=final_path or "",
                    page_url=url,
                    external_id=external_id,
                )
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
                format_video_notice(
                    ok=False,
                    title=notice_title,
                    channel=channel,
                    page_url=url,
                    detail="VK appears rate-limited.",
                    retry_at=retry_at,
                    external_id=external_id,
                )
            )
        elif kind in {"BLOCK_OR_AUTH", "AUTH"}:
            await notify(
                format_video_notice(
                    ok=False,
                    title=notice_title,
                    channel=channel,
                    page_url=url,
                    detail="Authentication or access problem.",
                    retry_at=retry_at,
                    external_id=external_id,
                )
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

            await resolve_placeholder_queued_titles()
            await run_one_download()

        except Exception as exc:
            print("scheduler error:", exc, flush=True)

        await asyncio.sleep(30)
