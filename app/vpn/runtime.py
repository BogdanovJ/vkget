from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
from .fallback import vpn_eligible_failure, vpn_mode
from .manager import get_best_endpoint, manager, mark_failure, mark_success, proxy_url


@dataclass
class VpnDownloadResult:
    attempted: bool = False
    succeeded: bool = False
    rc: int = 1
    path: str | None = None
    log: str = ""
    endpoint_ids: list[int] = field(default_factory=list)


def _max_attempts() -> int:
    value = getattr(settings, "vpn_max_endpoint_attempts", 3)
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return 3


async def try_vpn_download(download_fn, url: str, *args, **kwargs) -> VpnDownloadResult:
    result = VpnDownloadResult()
    tried: set[int] = set()
    logs: list[str] = []

    for attempt in range(_max_attempts()):
        endpoint = get_best_endpoint(exclude_ids=tried)
        if not endpoint:
            if attempt == 0:
                print("vkget: VPN fallback unavailable: no Russian endpoints", flush=True)
            break
        tried.add(endpoint.id)
        result.attempted = True
        result.endpoint_ids.append(endpoint.id)
        print(
            f"vkget: VPN endpoint selected: {endpoint.ip_address} score={endpoint.score}",
            flush=True,
        )
        geo = await manager.connect_and_verify(endpoint)
        if not geo:
            logs.append(f"{endpoint.ip_address}: connect/verify failed")
            if attempt + 1 < _max_attempts():
                print("vkget: VPN rotating endpoint", flush=True)
            continue

        try:
            rc, path, log = await download_fn(
                url,
                *args,
                proxy=proxy_url(),
                **kwargs,
            )
        except TypeError:
            # Test doubles and older callers may not accept proxy=.
            await manager.disconnect()
            rc, path, log = 1, None, "download function does not support VPN proxy"
        except Exception as exc:
            await manager.disconnect()
            mark_failure(endpoint.id, f"{type(exc).__name__}: {exc}")
            logs.append(f"{endpoint.ip_address}: {type(exc).__name__}: {exc}")
            if attempt + 1 < _max_attempts():
                print("vkget: VPN rotating endpoint", flush=True)
            continue

        if rc == 0:
            mark_success(endpoint.id, geo)
            await manager.disconnect()
            result.succeeded = True
            result.rc = 0
            result.path = path
            result.log = log
            return result

        combined = log or "VPN download failed"
        mark_failure(endpoint.id, combined[-400:])
        logs.append(f"{endpoint.ip_address}: {combined[-400:]}")
        await manager.disconnect()
        if not vpn_eligible_failure(combined):
            result.rc = rc
            result.path = path
            result.log = "\n".join(logs)
            return result
        if attempt + 1 < _max_attempts():
            print("vkget: VPN rotating endpoint", flush=True)

    await manager.disconnect()
    result.log = "\n".join(logs) if logs else "VPN fallback unavailable"
    return result


async def download_with_vpn_fallback(download_fn, url: str, *args, **kwargs):
    mode = vpn_mode()
    if mode == "always":
        vpn = await try_vpn_download(download_fn, url, *args, **kwargs)
        if vpn.succeeded:
            print("vkget: VK VPN download succeeded", flush=True)
            return vpn.rc, vpn.path, vpn.log
        print(
            "vkget: VPN unavailable or failed, trying direct download",
            flush=True,
        )
        rc, path, log = await download_fn(url, *args, **kwargs)
        if vpn.attempted and vpn.log:
            log = f"{log}\n{vpn.log}".strip()
        return rc, path, log

    rc, path, log = await download_fn(url, *args, **kwargs)
    if rc == 0 or mode == "off":
        return rc, path, log
    if not vpn_eligible_failure(log):
        return rc, path, log

    print("vkget: VK direct download failed, trying Russian VPN", flush=True)
    vpn = await try_vpn_download(download_fn, url, *args, **kwargs)
    if vpn.succeeded:
        print("vkget: VK VPN fallback succeeded", flush=True)
        return vpn.rc, vpn.path, vpn.log
    if vpn.log:
        log = f"{log}\n{vpn.log}".strip()
    return rc, path, log
