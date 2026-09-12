from __future__ import annotations

from datetime import datetime

import httpx
from sqlalchemy import func, select

from ..config import settings
from ..models import AppState, Video, VpnEndpoint
from .discovery import DISCOVERY_STATE_KEY
from .fallback import vpn_mode
from .manager import get_best_endpoint
from .util import format_ago, format_speed, now


def gateway_status_sync() -> dict:
    base = str(getattr(settings, "vpn_gateway_url", "") or "").rstrip("/")
    if not base:
        return {"connected": False, "available": False, "detail": "unconfigured"}
    try:
        with httpx.Client(timeout=1.5) as client:
            response = client.get(f"{base}/status")
            response.raise_for_status()
            data = response.json()
            data["available"] = True
            return data
    except Exception as exc:
        return {"connected": False, "available": False, "detail": str(exc)}


def last_discovery_at(db) -> datetime | None:
    row = db.get(AppState, DISCOVERY_STATE_KEY)
    if not row or not row.value:
        return None
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


def vpn_dashboard_status(db, current: datetime | None = None) -> dict:
    when = current or now()
    mode = vpn_mode()
    gateway = gateway_status_sync()
    available = db.scalar(
        select(func.count()).select_from(VpnEndpoint).where(
            VpnEndpoint.is_active.is_(True),
            VpnEndpoint.is_stale.is_(False),
        )
    ) or 0
    connected_ip = gateway.get("endpoint_ip")
    current_row = None
    if connected_ip:
        current_row = db.scalar(
            select(VpnEndpoint).where(VpnEndpoint.ip_address == connected_ip)
        )
    if current_row is None:
        current_row = get_best_endpoint(db, current=when)

    if mode == "off":
        status = "Disabled"
    elif gateway.get("connected"):
        status = "Connected"
    else:
        status = "Disconnected"

    return {
        "mode": mode,
        "status": status,
        "connected": bool(gateway.get("connected")),
        "gateway_available": bool(gateway.get("available")),
        "endpoint_ip": getattr(current_row, "ip_address", None) if current_row else None,
        "source": current_row.display_source() if current_row else "—",
        "reported_speed": format_speed(
            getattr(current_row, "reported_speed_bps", None) if current_row else None
        ),
        "measured_latency": (
            f"{current_row.measured_latency_ms} ms"
            if current_row and current_row.measured_latency_ms is not None
            else (
                f"{current_row.reported_ping_ms} ms"
                if current_row and current_row.reported_ping_ms is not None
                else "—"
            )
        ),
        "last_discovery": format_ago(last_discovery_at(db), when),
        "available_count": available,
        "download_in_progress": bool(
            db.scalar(
                select(func.count()).select_from(Video).where(Video.status == "DOWNLOADING")
            )
        ),
    }


def serialize_endpoint(row: VpnEndpoint, current: datetime | None = None) -> dict:
    when = current or now()
    return {
        "id": row.id,
        "ip_address": row.ip_address,
        "hostname": row.hostname,
        "source": row.source,
        "sources": row.sources,
        "display_source": row.display_source(),
        "protocol": row.display_protocol(),
        "reported_speed_bps": row.reported_speed_bps,
        "reported_speed": format_speed(row.reported_speed_bps),
        "reported_ping_ms": row.reported_ping_ms,
        "reported_sessions": row.reported_sessions,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "last_seen": format_ago(row.last_seen_at, when),
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "last_tested": format_ago(row.last_checked_at, when),
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_success": format_ago(row.last_success_at, when),
        "failed_connections": row.failed_connections,
        "consecutive_failures": row.consecutive_failures,
        "status": row.display_status(when),
        "score": row.score,
        "is_active": row.is_active,
        "is_available": row.is_available,
        "is_stale": row.is_stale,
        "failure_reason": row.failure_reason,
    }
