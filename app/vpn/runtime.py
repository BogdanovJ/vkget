from __future__ import annotations

from ..db import SessionLocal
from ..models import VpnProfile
from .manager import manager
from .profiles import mark_profile_failure, mark_profile_success
from .settings import fallback_to_direct, is_vpn_enabled, proxy_url, selected_profile


async def download_with_vpn(download_fn, url: str, *args, **kwargs):
    if not is_vpn_enabled():
        print("vkget: VPN disabled, using direct download", flush=True)
        return await download_fn(url, *args, **kwargs)

    with SessionLocal() as db:
        profile = selected_profile(db)
        allow_fallback = fallback_to_direct(db, profile)
        profile_id = profile.id if profile else None

    if not profile_id:
        print("vkget: VPN is enabled, but no VPN profile is selected", flush=True)
        if allow_fallback:
            print("vkget: VPN fallback enabled, retrying direct", flush=True)
            return await download_fn(url, *args, **kwargs)
        print("vkget: direct fallback disabled", flush=True)
        return 1, None, "VPN enabled but no profile is selected"

    with SessionLocal() as db:
        profile = db.get(VpnProfile, profile_id)
        if not profile or not profile.enabled:
            print("vkget: VPN is enabled, but no VPN profile is selected", flush=True)
            if allow_fallback:
                print("vkget: VPN fallback enabled, retrying direct", flush=True)
                return await download_fn(url, *args, **kwargs)
            print("vkget: direct fallback disabled", flush=True)
            return 1, None, "VPN enabled but no profile is selected"

        result = await manager.connect(profile)
        if not result.ok:
            print(f"vkget: VPN connection failed: {result.detail}", flush=True)
            mark_profile_failure(db, profile.id, result.detail)
            if allow_fallback:
                print("vkget: VPN fallback enabled, retrying direct", flush=True)
                return await download_fn(url, *args, **kwargs)
            print("vkget: direct fallback disabled", flush=True)
            return 1, None, result.detail

        print("vkget: yt-dlp using VPN proxy", flush=True)
        try:
            rc, path, log = await download_fn(
                url,
                *args,
                proxy=proxy_url(),
                **kwargs,
            )
        except TypeError:
            await manager.disconnect()
            print("vkget: download function does not support VPN proxy", flush=True)
            if allow_fallback:
                print("vkget: VPN fallback enabled, retrying direct", flush=True)
                return await download_fn(url, *args, **kwargs)
            return 1, None, "download function does not support VPN proxy"
        except Exception as exc:
            await manager.disconnect()
            detail = f"{type(exc).__name__}: {exc}"
            print(f"vkget: VPN-routed download failed: {detail}", flush=True)
            mark_profile_failure(db, profile.id, detail)
            if allow_fallback:
                print("vkget: disconnecting VPN", flush=True)
                print("vkget: retrying download directly", flush=True)
                return await download_fn(url, *args, **kwargs)
            print("vkget: direct fallback disabled", flush=True)
            raise

        if rc == 0:
            mark_profile_success(db, profile.id)
            await manager.disconnect()
            return rc, path, log

        print(
            f"vkget: VPN-routed download failed: {(log or 'download failed')[-200:]}",
            flush=True,
        )
        print("vkget: disconnecting VPN", flush=True)
        await manager.disconnect()
        mark_profile_failure(db, profile.id, (log or "VPN download failed")[-400:])
        if allow_fallback:
            print("vkget: retrying download directly", flush=True)
            return await download_fn(url, *args, **kwargs)
        print("vkget: direct fallback disabled", flush=True)
        return rc, path, log
