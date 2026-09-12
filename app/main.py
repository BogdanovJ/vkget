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
from .db import ensure_schema, get_db
from .models import AppState, Subscription, Video, VpnEndpoint
from .scheduler import (
    recover_interrupted_downloads,
    scan_subscription,
    scheduler_loop,
    get_global_cooldown,
    now
)
from .vpn.discovery import maybe_refresh_vpn_catalogue
from .vpn.manager import manager, mark_success
from .vpn.status import serialize_endpoint, vpn_dashboard_status
from .vpn.util import format_ago, format_speed
from .ytdlp import (
    inspect_url,
    is_usable_channel,
    is_usable_video_title,
    label_from_url,
    normalize_vk_url,
    resolve_video_metadata,
    title_from_entry,
    to_vkvideo,
)

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
templates.env.filters["vkvideo"] = to_vkvideo
templates.env.filters["ago"] = format_ago
templates.env.filters["speed"] = format_speed

@app.on_event("startup")
async def startup():
    ensure_schema()
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
            "title": next_download_video.display_title(),
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
                "title": (
                    next_scan_sub.display_title() if next_scan_sub else None
                ),
                "id": next_scan_sub.id if next_scan_sub else None,
            },
            "next_download": next_download,
            "max_height": settings.max_height,
            "vpn": vpn_dashboard_status(db, current),
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
    name: str = Form(""),
    initial_last_n: int = Form(3),
    min_duration_minutes: int = Form(10),
    stop_words: str = Form(""),
    watch_future: str | None = Form(None),
    db: Session = Depends(get_db),
):
    source_url = normalize_vk_url(url)
    custom_name = (name or "").strip()
    sub = Subscription(
        source_url=source_url,
        title=(custom_name or label_from_url(source_url))[:500],
        title_is_custom=bool(custom_name),
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

    video_title = title_from_entry(data, external_id)
    if not video_title:
        raw = data.get("title") or data.get("fulltitle") or data.get("alt_title") or ""
        video_title = str(raw).strip() or f"Video {external_id}"

    video = Video(
        source="vk",
        external_id=external_id,
        webpage_url=data.get("webpage_url") or normalized,
        title=video_title[:1000],
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

@app.post("/subscriptions/{sub_id}/profile")
def update_subscription_profile(
    sub_id: int,
    name: str = Form(""),
    min_duration_minutes: int = Form(10),
    stop_words: str = Form(""),
    watch_future: str | None = Form(None),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
):
    sub = db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404)

    custom_name = (name or "").strip()
    if custom_name:
        sub.title = custom_name[:500]
        sub.title_is_custom = True
    else:
        sub.title = label_from_url(sub.source_url)
        sub.title_is_custom = False

    sub.min_duration_seconds = max(min_duration_minutes, 0) * 60
    sub.extra_stop_words = stop_words
    sub.watch_future = watch_future == "on"
    sub.enabled = enabled == "on"
    db.commit()

    return RedirectResponse(
        f"/subscriptions/{sub_id}",
        status_code=303,
    )

@app.post("/subscriptions/{sub_id}/delete")
def delete_subscription(
    sub_id: int,
    db: Session = Depends(get_db),
):
    sub = db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404)

    db.delete(sub)
    db.commit()
    return RedirectResponse("/subscriptions", status_code=303)

@app.post("/subscriptions/{sub_id}/scan")
async def scan_now(sub_id: int):
    await scan_subscription(sub_id)
    return RedirectResponse(
        f"/subscriptions/{sub_id}",
        status_code=303,
    )

@app.post("/videos/{video_id}/download")
async def force_download(
    video_id: int,
    db: Session = Depends(get_db),
):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404)

    video.status = "QUEUED"
    video.ignore_reason = None
    video.next_attempt_at = datetime.now()
    webpage_url = video.webpage_url
    current_title = video.title
    current_channel = video.channel
    current_date = video.upload_date
    external_id = video.external_id
    subscription_id = video.subscription_id
    db.commit()

    meta = await resolve_video_metadata(
        webpage_url,
        title=current_title,
        channel=current_channel,
        upload_date=current_date,
        external_id=external_id,
    )
    video = db.get(Video, video_id)
    if video:
        if is_usable_video_title(meta["title"], external_id):
            video.title = meta["title"][:1000]
        if is_usable_channel(meta["channel"]):
            video.channel = meta["channel"][:500]
        if meta.get("upload_date"):
            video.upload_date = meta["upload_date"]
        db.commit()

    target = (
        f"/subscriptions/{subscription_id}"
        if subscription_id
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

@app.get("/vpn", response_class=HTMLResponse)
def vpn_page(request: Request, db: Session = Depends(get_db)):
    endpoints = db.scalars(
        select(VpnEndpoint).order_by(VpnEndpoint.score.desc(), VpnEndpoint.ip_address.asc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="vpn.html",
        context={
            "vpn": vpn_dashboard_status(db),
            "endpoints": endpoints,
        },
    )


@app.post("/vpn/refresh")
async def vpn_refresh_form():
    await maybe_refresh_vpn_catalogue(force=True)
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/endpoints/{endpoint_id}/enable")
def vpn_enable_endpoint(endpoint_id: int, db: Session = Depends(get_db)):
    row = db.get(VpnEndpoint, endpoint_id)
    if not row:
        raise HTTPException(404)
    row.is_active = True
    db.commit()
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/endpoints/{endpoint_id}/disable")
def vpn_disable_endpoint(endpoint_id: int, db: Session = Depends(get_db)):
    row = db.get(VpnEndpoint, endpoint_id)
    if not row:
        raise HTTPException(404)
    row.is_active = False
    db.commit()
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/endpoints/{endpoint_id}/test")
async def vpn_test_endpoint_form(endpoint_id: int, db: Session = Depends(get_db)):
    await _test_endpoint(endpoint_id, db)
    return RedirectResponse("/vpn", status_code=303)


@app.get("/api/vpn/status")
def api_vpn_status(db: Session = Depends(get_db)):
    return vpn_dashboard_status(db)


@app.get("/api/vpn/endpoints")
def api_vpn_endpoints(db: Session = Depends(get_db)):
    current = now()
    rows = db.scalars(
        select(VpnEndpoint).order_by(VpnEndpoint.score.desc(), VpnEndpoint.ip_address.asc())
    ).all()
    return {
        "endpoints": [serialize_endpoint(row, current) for row in rows],
    }


@app.post("/api/vpn/refresh")
async def api_vpn_refresh():
    stats = await maybe_refresh_vpn_catalogue(force=True)
    return {
        "ok": True,
        "found": getattr(stats, "found", 0),
        "added": getattr(stats, "added", 0),
        "updated": getattr(stats, "updated", 0),
    }


@app.post("/api/vpn/test/{endpoint_id}")
async def api_vpn_test(endpoint_id: int, db: Session = Depends(get_db)):
    result = await _test_endpoint(endpoint_id, db)
    return result


async def _test_endpoint(endpoint_id: int, db: Session):
    row = db.get(VpnEndpoint, endpoint_id)
    if not row:
        raise HTTPException(404)
    geo = await manager.connect_and_verify(row)
    await manager.disconnect()
    if geo:
        mark_success(endpoint_id, geo)
        return {"ok": True, "ip": geo.ip, "country": geo.country}
    row = db.get(VpnEndpoint, endpoint_id)
    return {
        "ok": False,
        "detail": row.failure_reason if row else "test failed",
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}
