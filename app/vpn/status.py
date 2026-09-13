from __future__ import annotations

import httpx
from sqlalchemy import select

from ..models import VpnProfile
from .profiles import serialize_profile
from .settings import fallback_to_direct, gateway_base_url, is_vpn_enabled, selected_profile
from .util import format_ago, now


def explain_gateway_failure(exc: BaseException | str | None) -> tuple[str, str]:
    """Map probe errors to a short UI label and operator hint. Never return raw errno."""
    text = str(exc or "").strip().lower()
    if not text or text == "unconfigured":
        return "NOT CONFIGURED", "VPN_GATEWAY_URL is empty"
    if any(
        needle in text
        for needle in (
            "name or service not known",
            "nodename nor servname",
            "name resolution",
            "temporary failure in name resolution",
            "no address associated with hostname",
        )
    ):
        return "NOT RUNNING", "vkget-vpn-gateway is not deployed in this cluster"
    if "connection refused" in text:
        return "NOT RUNNING", "vkget-vpn-gateway is not accepting connections"
    if "timed out" in text or "timeout" in text:
        return "UNREACHABLE", "gateway did not respond"
    return "OFFLINE", "gateway is not reachable"


def gateway_status_sync() -> dict:
    base = gateway_base_url()
    if not base:
        label, detail = explain_gateway_failure("unconfigured")
        return {
            "connected": False,
            "available": False,
            "label": label,
            "detail": detail,
        }
    try:
        with httpx.Client(timeout=1.5) as client:
            response = client.get(f"{base}/status")
            response.raise_for_status()
            data = response.json()
            data["available"] = True
            data["label"] = "ONLINE"
            return data
    except Exception as exc:
        label, detail = explain_gateway_failure(exc)
        return {
            "connected": False,
            "available": False,
            "label": label,
            "detail": detail,
        }


def vpn_dashboard_status(db, current=None) -> dict:
    when = current or now()
    enabled = is_vpn_enabled(db)
    profile = selected_profile(db)
    gateway = gateway_status_sync()
    fallback = fallback_to_direct(db, profile)
    return {
        "enabled": enabled,
        "fallback_to_direct": fallback,
        "status": "ON" if enabled else "OFF",
        "profile": serialize_profile(profile) if profile else None,
        "profile_name": profile.name if profile else None,
        "profile_type": profile.display_type() if profile else None,
        "gateway_available": bool(gateway.get("available")),
        "gateway_label": gateway.get("label") or ("ONLINE" if gateway.get("available") else "OFFLINE"),
        "gateway_detail": gateway.get("detail") or "",
        "connected": bool(gateway.get("connected")),
        "exit_ip": gateway.get("endpoint_ip") or (profile.last_exit_ip if profile else None),
        "exit_country": profile.last_exit_country if profile else None,
        "last_connected": format_ago(profile.last_connected_at, when) if profile else "NEVER",
        "last_error": profile.last_error if profile else None,
    }


def list_profiles(db) -> list[VpnProfile]:
    return list(
        db.scalars(select(VpnProfile).order_by(VpnProfile.name.asc(), VpnProfile.id.asc())).all()
    )
