from __future__ import annotations

import asyncio
from datetime import datetime
from urllib import request

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import ensure_schema, get_db
from .models import AppState, Subscription, Video, VpnProfile
from .queue import (
    delete_from_queue,
    is_queue_paused,
    list_queue,
    move_queue_item,
    next_queue_rank,
    pause_item,
    queue_order,
    resume_item,
    retry_now,
    set_queue_paused,
)
from .scheduler import (
    recover_interrupted_downloads,
    scan_subscription,
    scheduler_loop,
    get_global_cooldown,
    now
)
from .vpn.manager import manager
from .vpn.profiles import (
    ProfileError,
    create_profile,
    delete_profile,
    serialize_profile,
    update_profile,
)
from .vpn.settings import (
    pop_flash,
    set_fallback_to_direct,
    set_flash,
    set_selected_profile,
    set_vpn_enabled,
)
from .vpn.status import list_profiles, vpn_dashboard_status
from .vpn.util import format_ago
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
                Video.status.in_(["QUEUED", "FAILED_TEMPORARY", "PAUSED"])
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
        .order_by(*queue_order())
        .limit(1)
    ).first()

    if is_queue_paused(db):
        next_download = {"when": "PAUSED", "title": None}
    elif counts["downloading"]:
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
        queue_rank=next_queue_rank(db),
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
    video.queue_rank = next_queue_rank(db)
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
    videos = list_queue(db)
    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "videos": videos,
            "queue_paused": is_queue_paused(db),
        },
    )


@app.post("/queue/pause")
def queue_pause(db: Session = Depends(get_db)):
    set_queue_paused(db, True)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/resume")
def queue_resume(db: Session = Depends(get_db)):
    set_queue_paused(db, False)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/videos/{video_id}/up")
def queue_move_up(video_id: int, db: Session = Depends(get_db)):
    move_queue_item(db, video_id, -1)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/videos/{video_id}/down")
def queue_move_down(video_id: int, db: Session = Depends(get_db)):
    move_queue_item(db, video_id, 1)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/videos/{video_id}/retry")
def queue_retry_now(video_id: int, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404)
    retry_now(db, video)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/videos/{video_id}/pause")
def queue_pause_item(video_id: int, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404)
    pause_item(db, video)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/videos/{video_id}/resume")
def queue_resume_item(video_id: int, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404)
    resume_item(db, video)
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/videos/{video_id}/delete")
def queue_delete_item(video_id: int, db: Session = Depends(get_db)):
    video = db.get(Video, video_id)
    if not video:
        raise HTTPException(404)
    delete_from_queue(db, video)
    return RedirectResponse("/queue", status_code=303)

def _form_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


async def _config_from_form(config: str, upload: UploadFile | None) -> str:
    if upload and upload.filename:
        raw = await upload.read()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    return config


@app.get("/vpn", response_class=HTMLResponse)
def vpn_page(request: Request, db: Session = Depends(get_db)):
    flash = pop_flash(db)
    return templates.TemplateResponse(
        request=request,
        name="vpn.html",
        context={
            "vpn": vpn_dashboard_status(db),
            "profiles": list_profiles(db),
            "flash": flash,
        },
    )


@app.post("/vpn/enable")
def vpn_enable_form(db: Session = Depends(get_db)):
    set_vpn_enabled(db, True)
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/disable")
async def vpn_disable_form(db: Session = Depends(get_db)):
    await manager.disconnect()
    set_vpn_enabled(db, False)
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/fallback")
def vpn_fallback_form(
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
):
    set_fallback_to_direct(db, _form_bool(enabled))
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/disconnect")
async def vpn_disconnect_form():
    await manager.disconnect()
    return RedirectResponse("/vpn", status_code=303)


@app.get("/vpn/new", response_class=HTMLResponse)
def vpn_new_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="vpn_form.html",
        context={
            "title": "ADD VPN PROFILE",
            "action": "/vpn/profiles",
            "profile": None,
            "error": "",
            "name": "",
            "vpn_type": "wireguard",
            "config": "",
            "enabled": True,
            "is_default": False,
            "fallback_to_direct": True,
        },
    )


@app.post("/vpn/profiles")
async def vpn_create_profile(
    request: Request,
    name: str = Form(""),
    vpn_type: str = Form("openvpn"),
    config: str = Form(""),
    enabled: str = Form("off"),
    is_default: str = Form("off"),
    fallback_to_direct: str = Form("off"),
    upload: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    config_text = await _config_from_form(config, upload)
    try:
        create_profile(
            db,
            name=name,
            vpn_type=vpn_type,
            config_text=config_text,
            enabled=_form_bool(enabled),
            is_default=_form_bool(is_default),
            fallback_to_direct=_form_bool(fallback_to_direct),
        )
    except ProfileError as exc:
        return templates.TemplateResponse(
            request=request,
            name="vpn_form.html",
            status_code=400,
            context={
                "title": "ADD VPN PROFILE",
                "action": "/vpn/profiles",
                "profile": None,
                "error": str(exc),
                "name": name,
                "vpn_type": vpn_type,
                "config": config_text,
                "enabled": True,
                "is_default": _form_bool(is_default),
                "fallback_to_direct": _form_bool(fallback_to_direct),
            },
        )
    return RedirectResponse("/vpn", status_code=303)


@app.get("/vpn/profiles/{profile_id}/edit", response_class=HTMLResponse)
def vpn_edit_page(profile_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request=request,
        name="vpn_form.html",
        context={
            "title": "EDIT VPN PROFILE",
            "action": f"/vpn/profiles/{row.id}",
            "profile": row,
            "error": "",
            "name": row.name,
            "vpn_type": row.vpn_type,
            "config": row.config_text,
            "enabled": row.enabled,
            "is_default": row.is_default,
            "fallback_to_direct": row.fallback_to_direct,
        },
    )


@app.post("/vpn/profiles/{profile_id}")
async def vpn_update_profile(
    profile_id: int,
    request: Request,
    name: str = Form(""),
    vpn_type: str = Form("openvpn"),
    config: str = Form(""),
    enabled: str = Form("off"),
    is_default: str = Form("off"),
    fallback_to_direct: str = Form("off"),
    upload: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    config_text = await _config_from_form(config, upload)
    try:
        update_profile(
            db,
            row,
            name=name,
            vpn_type=vpn_type,
            config_text=config_text,
            enabled=_form_bool(enabled),
            is_default=_form_bool(is_default),
            fallback_to_direct=_form_bool(fallback_to_direct),
        )
    except ProfileError as exc:
        return templates.TemplateResponse(
            request=request,
            name="vpn_form.html",
            status_code=400,
            context={
                "title": "EDIT VPN PROFILE",
                "action": f"/vpn/profiles/{row.id}",
                "profile": row,
                "error": str(exc),
                "name": name,
                "vpn_type": vpn_type,
                "config": config_text,
                "enabled": _form_bool(enabled),
                "is_default": _form_bool(is_default),
                "fallback_to_direct": _form_bool(fallback_to_direct),
            },
        )
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/profiles/{profile_id}/use")
def vpn_use_profile(profile_id: int, db: Session = Depends(get_db)):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    if not row.enabled:
        set_flash(db, "Enable the profile before selecting it.")
        return RedirectResponse("/vpn", status_code=303)
    set_selected_profile(db, row.id)
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/profiles/{profile_id}/delete")
async def vpn_delete_profile(profile_id: int, db: Session = Depends(get_db)):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    if row.is_default:
        await manager.disconnect()
    delete_profile(db, row)
    return RedirectResponse("/vpn", status_code=303)


@app.post("/vpn/profiles/{profile_id}/test")
async def vpn_test_profile_form(profile_id: int, db: Session = Depends(get_db)):
    result = await _test_profile(profile_id, db)
    if result.get("ok"):
        set_flash(
            db,
            "Connection successful\n"
            f"External IP: {result.get('ip') or '—'}\n"
            f"Country: {result.get('country') or '—'}\n"
            f"Latency: {result.get('latency_ms') or '—'} ms",
        )
    else:
        set_flash(db, f"Connection failed\n{result.get('detail') or 'test failed'}")
    return RedirectResponse("/vpn", status_code=303)


@app.get("/api/vpn/status")
def api_vpn_status(db: Session = Depends(get_db)):
    return vpn_dashboard_status(db)


@app.get("/api/vpn/profiles")
def api_vpn_profiles(db: Session = Depends(get_db)):
    return {"profiles": [serialize_profile(row) for row in list_profiles(db)]}


@app.post("/api/vpn/profiles")
async def api_vpn_create(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    try:
        row = create_profile(
            db,
            name=str(payload.get("name") or ""),
            vpn_type=str(payload.get("vpn_type") or payload.get("type") or ""),
            config_text=str(payload.get("config") or ""),
            enabled=bool(payload.get("enabled", True)),
            is_default=bool(payload.get("is_default", False)),
            fallback_to_direct=bool(payload.get("fallback_to_direct", True)),
        )
    except ProfileError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "id": row.id}


@app.get("/api/vpn/profiles/{profile_id}")
def api_vpn_get_profile(profile_id: int, db: Session = Depends(get_db)):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    return serialize_profile(row, include_config=True)


@app.post("/api/vpn/profiles/{profile_id}")
async def api_vpn_update(profile_id: int, request: Request, db: Session = Depends(get_db)):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    payload = await request.json()
    try:
        update_profile(
            db,
            row,
            name=payload.get("name"),
            vpn_type=payload.get("vpn_type") or payload.get("type"),
            config_text=payload.get("config"),
            enabled=payload.get("enabled"),
            is_default=payload.get("is_default"),
            fallback_to_direct=payload.get("fallback_to_direct"),
        )
    except ProfileError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "id": row.id}


@app.post("/api/vpn/profiles/{profile_id}/delete")
async def api_vpn_delete(profile_id: int, db: Session = Depends(get_db)):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    if row.is_default:
        await manager.disconnect()
    delete_profile(db, row)
    return {"ok": True}


@app.post("/api/vpn/profiles/{profile_id}/test")
async def api_vpn_test(profile_id: int, db: Session = Depends(get_db)):
    return await _test_profile(profile_id, db)


@app.post("/api/vpn/profiles/{profile_id}/connect")
async def api_vpn_connect(profile_id: int, db: Session = Depends(get_db)):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    if not row.enabled:
        raise HTTPException(400, "profile is disabled")
    set_selected_profile(db, row.id)
    result = await manager.connect(row)
    if not result.ok:
        raise HTTPException(502, result.detail)
    return {"ok": True}


@app.post("/api/vpn/disconnect")
async def api_vpn_disconnect():
    await manager.disconnect()
    return {"ok": True, "connected": False}


@app.post("/api/vpn/enable")
def api_vpn_enable(db: Session = Depends(get_db)):
    set_vpn_enabled(db, True)
    return {"ok": True, "enabled": True}


@app.post("/api/vpn/disable")
async def api_vpn_disable(db: Session = Depends(get_db)):
    await manager.disconnect()
    set_vpn_enabled(db, False)
    return {"ok": True, "enabled": False}


async def _test_profile(profile_id: int, db: Session):
    row = db.get(VpnProfile, profile_id)
    if not row:
        raise HTTPException(404)
    result = await manager.connect_and_probe(row)
    await manager.disconnect()
    if result.ok and result.geo:
        return {
            "ok": True,
            "ip": result.geo.ip,
            "country": result.geo.country,
            "latency_ms": result.latency_ms,
        }
    return {"ok": False, "detail": result.detail or "test failed"}


@app.get("/healthz")
def healthz():
    return {"ok": True}
