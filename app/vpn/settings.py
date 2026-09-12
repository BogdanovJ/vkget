from __future__ import annotations

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import AppState, VpnProfile


ENABLED_KEY = "vpn_enabled"
FALLBACK_KEY = "vpn_fallback_to_direct"
SELECTED_KEY = "vpn_selected_profile_id"
FLASH_KEY = "vpn_flash"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _get_value(db, key: str) -> str | None:
    row = db.get(AppState, key)
    if not row:
        return None
    return row.value


def _set_value(db, key: str, value: str) -> None:
    row = db.get(AppState, key)
    if row:
        row.value = value
    else:
        db.add(AppState(key=key, value=value))


def is_vpn_enabled(db=None) -> bool:
    def read(session) -> bool:
        stored = _get_value(session, ENABLED_KEY)
        if stored is None or stored == "":
            return bool(getattr(settings, "vpn_enabled", False))
        return _truthy(stored)

    if db is not None:
        return read(db)
    with SessionLocal() as owned:
        return read(owned)


def set_vpn_enabled(db, enabled: bool) -> None:
    _set_value(db, ENABLED_KEY, "true" if enabled else "false")
    db.commit()


def fallback_to_direct(db=None, profile: VpnProfile | None = None) -> bool:
    def read(session) -> bool:
        stored = _get_value(session, FALLBACK_KEY)
        if stored is None or stored == "":
            global_on = bool(getattr(settings, "vpn_fallback_to_direct", True))
        else:
            global_on = _truthy(stored)
        if profile is not None and not profile.fallback_to_direct:
            return False
        return global_on

    if db is not None:
        return read(db)
    with SessionLocal() as owned:
        return read(owned)


def set_fallback_to_direct(db, enabled: bool) -> None:
    _set_value(db, FALLBACK_KEY, "true" if enabled else "false")
    db.commit()


def selected_profile_id(db) -> int | None:
    raw = _get_value(db, SELECTED_KEY)
    if raw and raw.isdigit():
        return int(raw)
    row = db.scalar(select(VpnProfile).where(VpnProfile.is_default.is_(True)))
    return row.id if row else None


def selected_profile(db=None) -> VpnProfile | None:
    def read(session) -> VpnProfile | None:
        profile_id = selected_profile_id(session)
        if profile_id:
            row = session.get(VpnProfile, profile_id)
            if row and row.enabled:
                return row
        return session.scalar(
            select(VpnProfile)
            .where(VpnProfile.enabled.is_(True), VpnProfile.is_default.is_(True))
        )

    if db is not None:
        return read(db)
    with SessionLocal() as owned:
        return read(owned)


def set_selected_profile(db, profile_id: int | None) -> None:
    if profile_id is None:
        _set_value(db, SELECTED_KEY, "")
        db.commit()
        return
    rows = list(db.scalars(select(VpnProfile)).all())
    for row in rows:
        row.is_default = row.id == profile_id
    _set_value(db, SELECTED_KEY, str(profile_id))
    db.commit()


def set_flash(db, message: str) -> None:
    _set_value(db, FLASH_KEY, message)
    db.commit()


def pop_flash(db) -> str:
    row = db.get(AppState, FLASH_KEY)
    if not row or not row.value:
        return ""
    message = row.value
    row.value = ""
    db.commit()
    return message


def proxy_url() -> str:
    return str(getattr(settings, "vpn_proxy_url", "") or "").strip()


def gateway_base_url() -> str:
    return str(getattr(settings, "vpn_gateway_url", "") or "").rstrip("/")
