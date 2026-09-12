from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from ..models import VpnProfile
from .ovpn import OvpnError, sanitize_ovpn
from .settings import set_selected_profile
from .util import now
from .wireguard import WireGuardError, sanitize_wireguard


class ProfileError(ValueError):
    pass


def normalize_vpn_type(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"wg", "wireguard"}:
        return "wireguard"
    if raw in {"ovpn", "openvpn"}:
        return "openvpn"
    raise ProfileError("VPN type must be OpenVPN or WireGuard")


def validate_config(vpn_type: str, config_text: str) -> str:
    kind = normalize_vpn_type(vpn_type)
    try:
        if kind == "wireguard":
            return sanitize_wireguard(config_text).text
        return sanitize_ovpn(config_text).text
    except (OvpnError, WireGuardError) as exc:
        raise ProfileError(str(exc)) from exc


def create_profile(
    db,
    *,
    name: str,
    vpn_type: str,
    config_text: str,
    enabled: bool = True,
    is_default: bool = False,
    fallback_to_direct: bool = True,
) -> VpnProfile:
    title = (name or "").strip()
    if not title:
        raise ProfileError("Profile name is required")
    kind = normalize_vpn_type(vpn_type)
    cleaned = validate_config(kind, config_text)
    stamp = now()
    row = VpnProfile(
        name=title[:200],
        vpn_type=kind,
        config_text=cleaned,
        enabled=enabled,
        is_default=False,
        fallback_to_direct=fallback_to_direct,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if is_default or not db.scalar(select(VpnProfile).where(VpnProfile.is_default.is_(True))):
        set_selected_profile(db, row.id)
        db.refresh(row)
    return row


def update_profile(
    db,
    profile: VpnProfile,
    *,
    name: str | None = None,
    vpn_type: str | None = None,
    config_text: str | None = None,
    enabled: bool | None = None,
    is_default: bool | None = None,
    fallback_to_direct: bool | None = None,
) -> VpnProfile:
    if name is not None:
        title = name.strip()
        if not title:
            raise ProfileError("Profile name is required")
        profile.name = title[:200]
    if vpn_type is not None:
        profile.vpn_type = normalize_vpn_type(vpn_type)
    if config_text is not None:
        profile.config_text = validate_config(profile.vpn_type, config_text)
    if enabled is not None:
        profile.enabled = enabled
        if not enabled and profile.is_default:
            profile.is_default = False
    if fallback_to_direct is not None:
        profile.fallback_to_direct = fallback_to_direct
    profile.updated_at = now()
    db.commit()
    if is_default:
        set_selected_profile(db, profile.id)
    elif enabled is False:
        other = db.scalar(
            select(VpnProfile).where(
                VpnProfile.enabled.is_(True),
                VpnProfile.id != profile.id,
            )
        )
        set_selected_profile(db, other.id if other else None)
    db.refresh(profile)
    return profile


def delete_profile(db, profile: VpnProfile) -> None:
    selected = profile.is_default
    db.delete(profile)
    db.commit()
    if selected:
        other = db.scalar(select(VpnProfile).where(VpnProfile.enabled.is_(True)))
        set_selected_profile(db, other.id if other else None)


def mark_profile_success(
    db,
    profile_id: int,
    *,
    exit_ip: str | None = None,
    country: str | None = None,
    latency_ms: int | None = None,
    current: datetime | None = None,
) -> None:
    row = db.get(VpnProfile, profile_id)
    if not row:
        return
    stamp = current or now()
    row.last_connected_at = stamp
    row.last_error = None
    row.last_exit_ip = exit_ip
    row.last_exit_country = country
    row.last_latency_ms = latency_ms
    row.successful_connections = (row.successful_connections or 0) + 1
    row.updated_at = stamp
    db.commit()


def mark_profile_failure(db, profile_id: int, reason: str) -> None:
    row = db.get(VpnProfile, profile_id)
    if not row:
        return
    stamp = now()
    row.last_failed_at = stamp
    row.last_error = (reason or "failed")[:1000]
    row.failed_connections = (row.failed_connections or 0) + 1
    row.updated_at = stamp
    db.commit()


def serialize_profile(row: VpnProfile, *, include_config: bool = False) -> dict:
    data = {
        "id": row.id,
        "name": row.name,
        "vpn_type": row.vpn_type,
        "display_type": row.display_type(),
        "enabled": row.enabled,
        "is_default": row.is_default,
        "fallback_to_direct": row.fallback_to_direct,
        "last_connected_at": row.last_connected_at.isoformat() if row.last_connected_at else None,
        "last_failed_at": row.last_failed_at.isoformat() if row.last_failed_at else None,
        "last_error": row.last_error,
        "last_exit_ip": row.last_exit_ip,
        "last_exit_country": row.last_exit_country,
        "last_latency_ms": row.last_latency_ms,
        "successful_connections": row.successful_connections,
        "failed_connections": row.failed_connections,
    }
    if include_config:
        data["config"] = row.config_text
    return data
