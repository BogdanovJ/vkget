from __future__ import annotations

import asyncio
from datetime import datetime
from urllib import request

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from .models import AppState, Subscription, Video
from .scheduler import (
    recover_interrupted_downloads,
    scan_subscription,
    scheduler_loop,
    get_global_cooldown,
    now
)
from .ytdlp import inspect_url, normalize_vk_url

app = FastAPI(title="VKGET")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def format_stamp(value: datetime | None) -> str:
    if value is None:
        return "NONE"
    return value.strftime("%d %b, %H:%M")


def format_when(value: datetime | None, current: datetime | None = None) -> str:
    if value is None:
        return "NONE"
    if value <= (current or now()):
        return "NOW"
    return format_stamp(value)


templates.env.filters["stamp"] = format_stamp
templates.env.filters["when"] = format_when

@app.on_event("startup")
async def startup():
    Base.metadata.create_all(engine)
    recover_interrupted_downloads()
    asyncio.create_task(scheduler_loop())

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    counts = {
        "subscriptions": db.scalar(select(func.count()).select_from(Subscription)) or 0,
        "queued": db.scalar(
            select(func.count()).select_from(Video).where(
                Video.status.in_(["QUEUED", "FAILED_TEMPORARY"])
            )
        ) or 0,
        "downloading": db.scalar(
            select(func.count()).select_from(Video).where(Video.status == "DOWNLOADING")
        ) or 0,
        "completed": db.scalar(
            select(func.count()).select_from(Video).where(Video.status == "COMPLETED")
        ) or 0,
    }

    recent = db.scalars(
        select(Video).order_by(Video.created_at.desc()).limit(12)
    ).all()

    subs = db.scalars(
        select(Subscription).order_by(Subscription.created_at.desc()).limit(8)
    ).all()

    current = now()
    cooldown = get_global_cooldown(db)
    if cooldown and cooldown <= current:
        cooldown = None

    next_scan_sub = db.scalars(
        select(Subscription)
        .where(
            Subscription.enabled.is_(True),
            Subscription.next_scan_at.is_not(None),
        )
        .order_by(Subscription.next_scan_at.asc())
        .limit(1)
    ).first()

    next_download_video = db.scalars(
        select(Video)
        .where(Video.status.in_(["QUEUED", "FAILED_TEMPORARY"]))
        .order_by(Video.next_attempt_at.asc())
        .limit(1)
    ).first()

    if counts["downloading"]:
        next_download = {"when": "IN PROGRESS", "title": None}
    elif not next_download_video:
        next_download = {"when": "NONE", "title": None}
    else:
        ready_at = next_download_video.next_attempt_at or current
        if cooldown and cooldown > ready_at:
            ready_at = cooldown
        next_download = {
            "when": format_when(ready_at, current),
            "title": next_download_video.title,
        }

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "counts": counts,
            "recent": recent,
            "subs": subs,
            "next_scan": {
                "when": (
                    format_when(next_scan_sub.next_scan_at, current)
                    if next_scan_sub
                    else "NONE"
                ),
                "title": next_scan_sub.title if next_scan_sub else None,
                "id": next_scan_sub.id if next_scan_sub else None,
            },
            "next_download": next_download,
            "max_height": settings.max_height,
        },
    )

@app.get("/subscriptions", response_class=HTMLResponse)
def subscriptions(request: Request, db: Session = Depends(get_db)):
    subs = db.scalars(
        select(Subscription).order_by(Subscription.created_at.desc())
    ).all()

    # /subscriptions
    return templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={"subs": subs},
    )

@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request):
    # /add
    return templates.TemplateResponse(
        request=request,
        name="add.html",
        context={},
    )

@app.post("/subscriptions")
async def add_subscription(
    url: str = Form(...),
    initial_last_n: int = Form(3),
    min_duration_minutes: int = Form(10),
    stop_words: str = Form(""),
    watch_future: str | None = Form(None),
    db: Session = Depends(get_db),
):
    sub = Subscription(
        source_url=normalize_vk_url(url),
        title="Scanning…",
        initial_last_n=max(initial_last_n, 0),
        watch_future=watch_future == "on",
        min_duration_seconds=max(min_duration_minutes, 0) * 60,
        extra_stop_words=stop_words,
        next_scan_at=datetime.now(),
    )

    db.add(sub)
    db.commit()
    db.refresh(sub)

    await scan_subscription(sub.id, initial=True)

    return RedirectResponse(
        f"/subscriptions/{sub.id}",
        status_code=303,
    )

@app.post("/one-off")
async def add_one_off(
    url: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized = normalize_vk_url(url)

    try:
        data = await inspect_url(normalized)
    except Exception as exc:
        raise HTTPException(400, str(exc))

    external_id = str(data.get("id") or "")
    if not external_id:
        raise HTTPException(400, "Could not determine video ID")

    existing = db.scalar(
        select(Video).where(
            Video.source == "vk",
            Video.external_id == external_id,
        )
    )
    if existing:
        return RedirectResponse("/queue", status_code=303)

    video = Video(
        source="vk",
        external_id=external_id,
        webpage_url=data.get("webpage_url") or normalized,
        title=(data.get("title") or external_id)[:1000],
        channel=(
            data.get("channel")
            or data.get("uploader")
            or "_single"
        )[:500],
        duration=data.get("duration"),
        upload_date=data.get("upload_date"),
        status="QUEUED",
        next_attempt_at=datetime.now(),
    )

    db.add(video)
    db.commit()

    return RedirectResponse("/queue", status_code=303)

@app.get("/subscriptions/{sub_id}", response_class=HTMLResponse)
def subscription_detail(
    sub_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    sub = db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404)

    videos = db.scalars(
        select(Video)
        .where(Video.subscription_id == sub_id)
        .order_by(Video.created_at.desc())
        .limit(500)
    ).all()

    # individual subscription page
    return templates.TemplateResponse(
        request=request,
        name="subscription.html",
        context={
            "sub": sub,
            "videos": videos,
        },
    )

@app.post("/subscriptions/{sub_id}/scan")
async def scan_now(sub_id: int):
    await scan_subscription(sub_id)
    return RedirectResponse(
        f"/subscriptions/{sub_id}",
        status_code=303,
    )

@app.post("/videos/{video_id}/download")
def force_download(
    video_id: int,
    db: Session = Depends(get_db),
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404)

    video.status = "QUEUED"
    video.ignore_reason = None
    video.next_attempt_at = datetime.now()
    db.commit()

    target = (
        f"/subscriptions/{video.subscription_id}"
        if video.subscription_id
        else "/queue"
    )
    return RedirectResponse(target, status_code=303)

@app.post("/videos/{video_id}/ignore")
def ignore_video(
    video_id: int,
    db: Session = Depends(get_db),
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404)

    video.status = "IGNORED_MANUAL"
    video.ignore_reason = "manual"
    db.commit()

    target = (
        f"/subscriptions/{video.subscription_id}"
        if video.subscription_id
        else "/queue"
    )
    return RedirectResponse(target, status_code=303)

@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request, db: Session = Depends(get_db)):
    videos = db.scalars(
        select(Video)
        .where(Video.status.in_(["QUEUED", "DOWNLOADING", "FAILED_TEMPORARY"]))
        .order_by(Video.next_attempt_at.asc())
        .limit(200)
    ).all()

    # /queue
    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={"videos": videos},
    )

@app.get("/healthz")
def healthz():
    return {"ok": True}
