from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError

from .config import settings
from .db import SessionLocal, reset_pool
from .filters import rejection_reason
from .models import AppState, Subscription, Video, format_published
from .notifier import format_video_notice, friendly_failure, notify
from .queue import RUNNABLE_STATUSES, is_queue_paused, next_queue_rank, queue_order
from .retention import apply_retention
from .vpn.runtime import download_with_vpn
from .ytdlp import (
    PLACEHOLDER_TITLES,
    adopt_existing_download,
    dated_filename,
    download_folder_name,
    download_video,
    format_upload_date,
    inspect_playlist_flat,
    is_partial_download,
    is_usable_channel,
    is_usable_video_title,
    label_from_url,
    path_inside_root,
    place_downloaded_file,
    remove_orphan_partials,
    resolve_video_metadata,
    title_from_entry,
)

def now():
    return datetime.now()

def jitter_minutes(low: int, high: int):
    return timedelta(minutes=random.randint(low, high))

def jitter_hours(low: int, high: int):
    return timedelta(minutes=random.randint(low * 60, high * 60))


def entry_recency_key(entry) -> int | None:
    """Sort key for newest-first. Timestamps beat YYYYMMDD dates."""
    if not entry:
        return None
    for field in ("timestamp", "release_timestamp"):
        raw = entry.get(field)
        if raw in (None, ""):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    for field in ("upload_date", "release_date"):
        digits = "".join(ch for ch in str(entry.get(field) or "") if ch.isdigit())[:8]
        if len(digits) == 8:
            try:
                return int(digits)
            except ValueError:
                continue
    return None


def newest_initial_ids(entries, limit: int) -> set[str]:
    """LAST N = N newest videos. Dated lists use dates; else index 0 is newest."""
    if limit <= 0:
        return set()
    usable = [entry for entry in entries if entry and entry.get("id")]
    if not usable:
        return set()
    if any(entry_recency_key(entry) is not None for entry in usable):
        ordered = sorted(
            usable,
            key=lambda entry: entry_recency_key(entry) or 0,
            reverse=True,
        )
        return {str(entry.get("id")) for entry in ordered[:limit]}
    return {str(entry.get("id")) for entry in usable[:limit]}


def published_at_from_upload_date(raw) -> datetime | None:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())[:8]
    if len(digits) != 8:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d")
    except ValueError:
        return None


def published_at_from_entry(entry) -> datetime | None:
    """Publish time from a playlist entry. Dates without a clock stay at midnight."""
    if not entry:
        return None
    for field in ("timestamp", "release_timestamp"):
        raw = entry.get(field)
        if raw in (None, ""):
            continue
        try:
            stamp = int(raw)
        except (TypeError, ValueError):
            continue
        if stamp > 10_000_000_000:
            stamp //= 1000
        try:
            return datetime.fromtimestamp(stamp)
        except (OSError, OverflowError, ValueError):
            continue
    for field in ("upload_date", "release_date"):
        published = published_at_from_upload_date(entry.get(field))
        if published:
            return published
    return None


def select_newest_entry(entries) -> dict | None:
    """Newest dated entry, or the playlist head when nothing is dated."""
    usable = []
    for entry in entries or []:
        if not entry or not entry.get("id"):
            continue
        if not (entry.get("webpage_url") or entry.get("url")):
            continue
        usable.append(entry)
    if not usable:
        return None
    dated = [entry for entry in usable if published_at_from_entry(entry) is not None]
    if dated:
        return max(dated, key=lambda entry: published_at_from_entry(entry))
    return usable[0]


def queued_newest_first(rows: list[dict]) -> list[dict]:
    """Lowest queue rank should be the newest queued video."""
    if any(row.get("_recency") is not None for row in rows):
        return sorted(
            rows,
            key=lambda row: (row.get("_recency") or 0, -(row.get("_index") or 0)),
            reverse=True,
        )
    return sorted(rows, key=lambda row: row.get("_index") or 0)

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

SHARE_SENTINEL = ".vkget-share"
DB_ERRORS = (OperationalError, InterfaceError)
SCHEDULER_SLEEP_SECONDS = 30
DB_BACKOFF_MAX_SECONDS = 300


def is_mount_point(path: str) -> bool:
    """True when path is a real mount, including same-fs bind mounts."""
    real = os.path.realpath(path)
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                prefix = line.split(" - ", 1)[0]
                parts = prefix.split()
                if len(parts) < 5:
                    continue
                mount_point = parts[4].replace("\\040", " ")
                if mount_point == real:
                    return True
    except OSError:
        pass
    return os.path.ismount(real)


def prepare_download_share(root: str | None = None) -> bool:
    """Create .vkget-share when /downloads is a mounted, writable share."""
    root = root or settings.download_root
    if not os.path.isdir(root) or not os.access(root, os.W_OK):
        return False

    sentinel = os.path.join(root, SHARE_SENTINEL)
    if os.path.isfile(sentinel):
        return True
    if not is_mount_point(root):
        return False

    try:
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write("ok\n")
        return True
    except OSError:
        return False


def storage_ok():
    root = settings.download_root

    if not os.path.isdir(root) or not os.access(root, os.W_OK):
        return False
    if not prepare_download_share(root):
        return False

    probe = os.path.join(root, ".vkget-write-test")

    try:
        with open(probe, "w", encoding="utf-8") as f:
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
        summary = " · ".join(parts)
    else:
        summary = "NO NEW DOWNLOADS · " + " · ".join(parts)
    return summary


def with_newest_stamp(summary: str, published: datetime | None) -> str:
    stamp = format_published(published)
    if not stamp:
        return summary
    return f"{summary} · NEWEST {stamp}"


async def resolve_newest_seen(
    head: dict | None,
    new_videos: list[dict],
    inspected_ids: set[str],
) -> dict | None:
    """Id, title, and publish time of the newest playlist entry."""
    if not head:
        return None
    external_id = str(head.get("id") or "")
    url = head.get("webpage_url") or head.get("url") or ""
    if not external_id or not url:
        return None
    title = title_from_entry(head, external_id)
    published = published_at_from_entry(head)
    row = next(
        (item for item in new_videos if item.get("external_id") == external_id),
        None,
    )
    if row and is_usable_video_title(row.get("title"), external_id):
        title = row["title"]
    if published is None and row:
        published = published_at_from_upload_date(row.get("upload_date"))
    if published is None and external_id not in inspected_ids:
        meta = await resolve_video_metadata(
            url,
            title=title,
            channel=(head.get("channel") or head.get("uploader") or ""),
            upload_date=head.get("upload_date"),
            external_id=external_id,
        )
        if not is_usable_video_title(title, external_id):
            resolved = (meta.get("title") or "").strip()
            if is_usable_video_title(resolved, external_id):
                title = resolved
        published = published_at_from_upload_date(meta.get("upload_date")) or published
    return {"id": external_id, "title": title, "published": published}


def apply_newest_seen(sub: Subscription, seen: dict | None) -> None:
    if not seen or not seen.get("id"):
        sub.newest_video_id = None
        sub.newest_video_title = None
        sub.newest_video_at = None
        return
    sub.newest_video_id = str(seen["id"])[:300]
    title = (seen.get("title") or "").strip()
    sub.newest_video_title = title[:1000] or None
    sub.newest_video_at = seen.get("published")

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
            initial_allowed = newest_initial_ids(entries, sub.initial_last_n)

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
                    "_recency": entry_recency_key(entry),
                    "_index": len(new_videos),
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

    head = select_newest_entry(entries)
    inspected_ids = {
        new_videos[index]["external_id"]
        for index in inspect_indexes
        if index < len(new_videos)
    }

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

    seen = await resolve_newest_seen(head, new_videos, inspected_ids)
    result = with_newest_stamp(result, seen.get("published") if seen else None)

    with SessionLocal() as db:
        sub = db.get(Subscription, subscription_id)
        if not sub:
            return
        rank = next_queue_rank(db)
        queued_rows = queued_newest_first(
            [row for row in new_videos if row.get("status") == "QUEUED"]
        )
        for row in queued_rows:
            row["queue_rank"] = rank
            rank += 1
        for row in new_videos:
            row.pop("_recency", None)
            row.pop("_index", None)
            db.add(Video(**row))
        apply_newest_seen(sub, seen)
        sub.last_scan_at = scan_at
        sub.last_error = None
        sub.last_scan_result = result
        sub.next_scan_at = next_at
        db.commit()

def _needs_metadata_inspect(job: Video) -> bool:
    return (
        not is_usable_video_title(job.title, job.external_id)
        or not is_usable_channel(job.channel)
        or not format_upload_date(job.upload_date)
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
    if format_upload_date(date):
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

async def run_one_download() -> bool:
    with SessionLocal() as db:
        cooldown = get_global_cooldown(db)
        if cooldown and cooldown > now():
            return False

        if is_queue_paused(db):
            return False

        job = db.scalar(
            select(Video)
            .where(
                Video.status.in_(RUNNABLE_STATUSES),
                Video.next_attempt_at <= now(),
            )
            .order_by(*queue_order())
            .limit(1)
        )

        if not job:
            return False

        if not storage_ok():
            until = now() + jitter_hours(2, 4)
            set_global_cooldown(db, until)
            db.commit()
            await notify(
                "⚠ VKGET\n"
                "Download storage is unavailable or not writable.\n"
                f"Paused until approximately {until:%Y-%m-%d %H:%M}."
            )
            return False

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
        if not job or job.status not in RUNNABLE_STATUSES:
            return False

        _apply_resolved_metadata(job, meta, folder_fallback=sub_label)
        url = job.webpage_url
        channel = job.channel
        title = job.title
        external_id = job.external_id
        upload_date = job.upload_date
        notice_title = job.display_title()
        attempts = job.attempts
        db.commit()

    folder = download_folder_name(
        subscription_title=sub_label,
        channel=channel,
    )
    adopted = adopt_existing_download(
        settings.download_root,
        video_id=external_id,
        title=title,
        upload_date=upload_date,
        folder=folder,
    )
    if adopted:
        with SessionLocal() as db:
            job = db.get(Video, video_id)
            if not job or job.status not in RUNNABLE_STATUSES:
                return False
            notice_title = job.display_title()
            channel = job.channel
            url = job.webpage_url
            external_id = job.external_id
            job.status = "COMPLETED"
            job.local_path = adopted
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
                path=adopted,
                page_url=url,
                external_id=external_id,
            )
        )
        return True

    with SessionLocal() as db:
        job = db.get(Video, video_id)
        if not job or job.status not in RUNNABLE_STATUSES:
            return False
        job.status = "DOWNLOADING"
        job.attempts += 1
        attempts = job.attempts
        job.next_attempt_at = None
        db.commit()

    try:
        rc, final_path, log = await download_with_vpn(
            download_video,
            url,
            channel,
            title=title,
            video_id=external_id,
            upload_date=upload_date,
            folder=folder,
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
                detail=friendly_failure(exc),
                retry_at=retry_at,
                external_id=external_id,
            )
        )
        return True

    with SessionLocal() as db:
        job = db.get(Video, video_id)
        if not job:
            return True

        notice_title = job.display_title()
        channel = job.channel
        url = job.webpage_url
        external_id = job.external_id

        if rc == 0 and final_path and os.path.isfile(final_path):
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
            return True

        kind = classify_error(log or "")
        if rc == 0:
            kind = "TEMPORARY"
            log = (log or "Download finished without a file.")
        retry_at = retry_time(attempts, kind)

        job.status = "FAILED_TEMPORARY"
        job.last_error = (log or "")[-4000:]
        job.next_attempt_at = retry_at

        if kind in {"RATE_LIMIT", "BLOCK_OR_AUTH", "AUTH"}:
            set_global_cooldown(db, retry_at)

        db.commit()

        if kind == "RATE_LIMIT":
            detail = "VK appears rate-limited."
        elif kind in {"BLOCK_OR_AUTH", "AUTH"}:
            detail = "Authentication or access problem."
        else:
            detail = friendly_failure(log)
        await notify(
            format_video_notice(
                ok=False,
                title=notice_title,
                channel=channel,
                page_url=url,
                detail=detail,
                retry_at=retry_at,
                external_id=external_id,
            )
        )
    return True



_KEEP_PARTIAL_STATUSES = ("DOWNLOADING", "QUEUED", "FAILED_TEMPORARY", "PAUSED")
_rename_settled: set[int] = set()


def release_fragment_completions() -> int:
    """Queue a download again when the saved file is only an unmerged stream.

    VK throttles a transfer until it stops. The next attempt continues the same
    parts. An audio piece such as [{id}].fdash_sep-11.m4a is not the video.
    """
    released = 0
    with SessionLocal() as db:
        jobs = db.scalars(
            select(Video).where(
                Video.status == "COMPLETED",
                Video.local_path.is_not(None),
            )
        ).all()
        for job in jobs:
            if not is_partial_download(Path(job.local_path or "")):
                continue
            job.status = "QUEUED"
            job.local_path = None
            job.completed_at = None
            job.last_error = None
            job.next_attempt_at = now()
            released += 1
        if released:
            db.commit()
    return released


async def sweep_orphan_partials() -> list[str]:
    """Remove leftover temps while nothing is downloading."""
    if not storage_ok():
        return []
    with SessionLocal() as db:
        downloading = db.scalar(
            select(Video.id).where(Video.status == "DOWNLOADING").limit(1)
        )
        if downloading:
            return []
        protected = {
            str(item)
            for item in db.scalars(
                select(Video.external_id).where(
                    Video.status.in_(_KEEP_PARTIAL_STATUSES)
                )
            ).all()
            if item
        }
    removed = remove_orphan_partials(settings.download_root, protected)
    if removed:
        print(
            "vkget: removed leftover partial downloads: "
            + ", ".join(os.path.basename(item) for item in removed),
            flush=True,
        )
    return removed


async def rename_one_completed_file() -> bool:
    """Rename one tracked file that is missing `{title} [{id}]-{date}`."""
    if not storage_ok():
        return False
    root = settings.download_root
    with SessionLocal() as db:
        jobs = db.scalars(
            select(Video)
            .where(
                Video.status == "COMPLETED",
                Video.local_path.is_not(None),
            )
            .order_by(Video.id.asc())
        ).all()
        target = None
        for job in jobs:
            if job.id in _rename_settled:
                continue
            local = job.local_path or ""
            if dated_filename(local, job.external_id):
                _rename_settled.add(job.id)
                continue
            if not path_inside_root(local, root):
                _rename_settled.add(job.id)
                continue
            target = (
                job.id,
                job.webpage_url,
                job.external_id,
                job.title,
                job.upload_date,
                local,
            )
            break
    if not target:
        return False

    video_pk, url, external_id, title, upload_date, local = target
    # Omit channel so a stored channel name cannot skip the yt-dlp extract.
    meta = await resolve_video_metadata(
        url,
        title=title,
        channel="",
        upload_date=upload_date,
        external_id=external_id,
    )
    new_path = place_downloaded_file(
        local,
        title=meta.get("title") or title,
        video_id=external_id,
        upload_date=meta.get("upload_date") or upload_date,
    )
    if new_path == local or not os.path.isfile(new_path):
        _rename_settled.add(video_pk)
        return False

    with SessionLocal() as db:
        job = db.get(Video, video_pk)
        if not job or job.status != "COMPLETED" or job.local_path != local:
            _rename_settled.add(video_pk)
            return False
        job.local_path = new_path
        db.commit()
    _rename_settled.add(video_pk)
    return True


async def scheduler_loop():
    delay = SCHEDULER_SLEEP_SECONDS
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
            release_fragment_completions()
            downloaded = await run_one_download()
            if not downloaded:
                await rename_one_completed_file()
            await sweep_orphan_partials()
            with SessionLocal() as db:
                apply_retention(db)
            delay = SCHEDULER_SLEEP_SECONDS

        except DB_ERRORS as exc:
            print("scheduler database error:", exc, flush=True)
            reset_pool()
            await asyncio.sleep(delay)
            delay = min(delay * 2, DB_BACKOFF_MAX_SECONDS)
            continue
        except Exception as exc:
            print("scheduler error:", exc, flush=True)
            delay = SCHEDULER_SLEEP_SECONDS

        await asyncio.sleep(SCHEDULER_SLEEP_SECONDS)
