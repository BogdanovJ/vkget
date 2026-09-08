from __future__ import annotations

import asyncio
import json
import os
import re

import shutil
import tempfile

from pathlib import Path

from urllib.parse import unquote, urlparse, urlunparse

import httpx

from .config import settings

PLACEHOLDER_TITLES = {"", "Scanning…", "Scanning...", "Subscription"}
_UNUSABLE_VIDEO_TITLES = frozenset(
    {
        "",
        "na",
        "n/a",
        "untitled",
        "unknown",
        "scanning…",
        "scanning...",
        "subscription",
        "video",
    }
)
_VK_ID_RE = re.compile(r"^-?\d+_\d+$")
_VK_SLUG_RE = re.compile(
    r"^(?:video|clip|playlist)[-/]-?\d+(?:_\d+)?$",
    re.I,
)

_VK_COM_HOSTS = {
    "vk.com",
    "www.vk.com",
    "m.vk.com",
    "vk.ru",
    "www.vk.ru",
    "m.vk.ru",
}
_VKVIDEO_HOSTS = {
    "vkvideo.ru",
    "www.vkvideo.ru",
}
_CHALLENGE_HINTS = (
    "403",
    "forbidden",
    "challenge",
    "cloudflare",
    "captcha",
    "just a moment",
    "attention required",
    "access denied",
    "bot protection",
    "cf-ray",
    "please wait",
    "checking your browser",
    "enable javascript",
    "unusual traffic",
    "verify you are",
    "ddos-guard",
    "blocked",
)


def normalize_vk_url(url: str) -> str:
    """Light strip only: trim, https, drop fragment. Keep the pasted host."""
    url = (url or "").strip()
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url

    parsed = urlparse(url)
    scheme = "https" if parsed.scheme in {"http", "https", ""} else parsed.scheme
    host = (parsed.hostname or "").lower()
    if not host:
        return url

    if parsed.port and parsed.port not in {80, 443}:
        netloc = f"{host}:{parsed.port}"
    else:
        netloc = host

    path = parsed.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _host(url: str) -> str:
    return (urlparse(normalize_vk_url(url)).netloc or "").lower()


def _is_vk_host(host: str) -> bool:
    return host in _VK_COM_HOSTS or host in _VKVIDEO_HOSTS


def _replace_host(url: str, new_host: str) -> str:
    parsed = urlparse(normalize_vk_url(url))
    if not parsed.netloc:
        return normalize_vk_url(url)
    return urlunparse(
        (parsed.scheme or "https", new_host, parsed.path, "", parsed.query, "")
    )


def to_vk_com(url: str) -> str:
    url = normalize_vk_url(url)
    host = _host(url)
    if not _is_vk_host(host):
        return url
    return _replace_host(url, "vk.com")


def to_vkvideo(url: str) -> str:
    url = normalize_vk_url(url)
    host = _host(url)
    if not _is_vk_host(host):
        return url
    return _replace_host(url, "vkvideo.ru")


def mirror_url(url: str) -> str:
    host = _host(url)
    if host in _VKVIDEO_HOSTS:
        return to_vk_com(url)
    if host in _VK_COM_HOSTS:
        return to_vkvideo(url)
    return normalize_vk_url(url)


def scan_url_candidates(url: str) -> list[str]:
    """Playlists/channels: vkvideo.ru first, then vk.com."""
    primary = to_vkvideo(url)
    secondary = to_vk_com(url)
    if secondary == primary:
        return [primary]
    return [primary, secondary]


def download_url_candidates(url: str) -> list[str]:
    """Video files: vk.com first, then vkvideo.ru."""
    primary = to_vk_com(url)
    secondary = to_vkvideo(url)
    if secondary == primary:
        return [primary]
    return [primary, secondary]


def is_usable_video_title(title: str | None, external_id: str = "") -> bool:
    """False for empty/NA/url-bit titles that VK --flat-playlist often yields."""
    text = (title or "").strip()
    if not text:
        return False
    if text.casefold() in _UNUSABLE_VIDEO_TITLES:
        return False
    ext = str(external_id or "").strip()
    if ext and text == ext:
        return False
    if ext and text.casefold() == f"video {ext}".casefold():
        return False
    if _VK_ID_RE.fullmatch(text) or _VK_SLUG_RE.fullmatch(text):
        return False
    lowered = text.casefold()
    if lowered.startswith(("http://", "https://", "//")):
        return False
    if "vk.com/" in lowered or "vkvideo.ru/" in lowered:
        return False
    return True


def title_from_entry(entry: dict | None, external_id: str = "") -> str:
    if not isinstance(entry, dict):
        return ""
    for key in ("title", "fulltitle", "alt_title"):
        value = entry.get(key)
        if isinstance(value, str) and is_usable_video_title(value, external_id):
            return value.strip()
    return ""


def label_from_url(url: str) -> str:
    normalized = normalize_vk_url(url or "")
    parsed = urlparse(normalized)
    parts = [p for p in unquote(parsed.path or "").strip("/").split("/") if p]
    for part in reversed(parts):
        if part.startswith("@"):
            return part[:500]
    if parts:
        return parts[-1][:500]
    host_path = f"{parsed.netloc}{parsed.path}".strip("/")
    return (host_path or normalized or "Subscription")[:500]


def looks_like_bot_protection(error: str) -> bool:
    e = (error or "").lower()
    return any(hint in e for hint in _CHALLENGE_HINTS)


def flaresolverr_enabled() -> bool:
    return bool((settings.flaresolverr_url or "").strip())


def _flaresolverr_endpoint() -> str:
    base = (settings.flaresolverr_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _netscape_cookie_line(cookie: dict) -> str | None:
    name = cookie.get("name")
    if not name:
        return None
    domain = cookie.get("domain") or ""
    if not domain:
        return None
    include_sub = "TRUE" if domain.startswith(".") else "FALSE"
    path = cookie.get("path") or "/"
    secure = "TRUE" if cookie.get("secure") else "FALSE"
    raw_expires = cookie.get("expiry", cookie.get("expires", 0))
    try:
        expires = int(float(raw_expires or 0))
    except (TypeError, ValueError):
        expires = 0
    if expires < 0:
        expires = 0
    value = cookie.get("value") or ""
    return (
        f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}"
    )


def _write_cookie_file(extra_cookies: list[dict] | None = None) -> tuple[str | None, str | None]:
    """Return (cookie_path, cleanup_path). cleanup_path is deleted by the caller."""
    base = settings.cookie_file if os.path.exists(settings.cookie_file) else None
    extra = extra_cookies or []

    if extra:
        fd, path = tempfile.mkstemp(prefix="vkget-flare-", suffix=".txt")
        os.close(fd)
        lines: list[str] = ["# Netscape HTTP Cookie File", ""]
        if base:
            with open(base, encoding="utf-8", errors="replace") as handle:
                body = handle.read()
            for line in body.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
                    continue
                lines.append(line)
        for cookie in extra:
            row = _netscape_cookie_line(cookie)
            if row:
                lines.append(row)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path, path

    if base:
        runtime = os.path.join(tempfile.gettempdir(), "vkget-cookies.txt")
        shutil.copy2(base, runtime)
        return runtime, None

    return None, None


def common_args(
    extra_cookies: list[dict] | None = None,
    user_agent: str | None = None,
) -> tuple[list[str], str | None]:
    args = ["/usr/local/bin/yt-dlp"]
    cookie_path, cleanup = _write_cookie_file(extra_cookies)
    if cookie_path:
        args += ["--cookies", cookie_path]
    if user_agent:
        args += ["--user-agent", user_agent]
    return args, cleanup


async def _run(args: list[str], timeout: int | None = None):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def fetch_flaresolverr_cookies(url: str) -> tuple[list[dict], str | None]:
    if not flaresolverr_enabled():
        raise RuntimeError("FlareSolverr is not configured")

    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
    }
    timeout = httpx.Timeout(90.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(_flaresolverr_endpoint(), json=payload)
        response.raise_for_status()
        data = response.json()

    if data.get("status") != "ok":
        raise RuntimeError(data.get("message") or "FlareSolverr failed")

    solution = data.get("solution") or {}
    cookies = solution.get("cookies") or []
    user_agent = solution.get("userAgent") or None
    if not cookies:
        raise RuntimeError("FlareSolverr returned no cookies")
    print(f"vkget: FlareSolverr cookies for {url}", flush=True)
    return cookies, user_agent


async def _ytdlp_json(
    url: str,
    extra: list[str],
    timeout: int,
    extra_cookies: list[dict] | None = None,
    user_agent: str | None = None,
) -> dict:
    cleanup = None
    try:
        args, cleanup = common_args(extra_cookies, user_agent)
        args += extra + [url]
        rc, out, err = await _run(args, timeout=timeout)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip() or f"yt-dlp exited {rc}")
        return json.loads(out)
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


async def _try_hosts_json(
    candidates: list[str],
    extra: list[str],
    timeout: int,
) -> dict:
    errors: list[str] = []
    for candidate in candidates:
        try:
            return await _ytdlp_json(candidate, extra, timeout)
        except Exception as exc:
            err = str(exc).strip() or type(exc).__name__
            errors.append(f"{candidate}: {err}")
            if not (flaresolverr_enabled() and looks_like_bot_protection(err)):
                continue
            try:
                cookies, user_agent = await fetch_flaresolverr_cookies(candidate)
                return await _ytdlp_json(
                    candidate,
                    extra,
                    timeout,
                    extra_cookies=cookies,
                    user_agent=user_agent,
                )
            except Exception as flare_exc:
                errors.append(f"FlareSolverr {candidate}: {flare_exc}")
    raise RuntimeError(" | ".join(errors) or "yt-dlp failed")


async def inspect_url(url: str) -> dict:
    extra = [
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    return await _try_hosts_json(download_url_candidates(url), extra, 120)


async def resolve_video_title(
    url: str,
    current: str,
    external_id: str = "",
) -> str:
    """Inspect a single video URL only when the stored/playlist title is unusable."""
    current = (current or "").strip()
    if is_usable_video_title(current, external_id):
        return current
    try:
        info = await inspect_url(url)
    except Exception:
        return current
    resolved = title_from_entry(info, str(info.get("id") or external_id))
    return resolved or current


async def inspect_playlist_flat(url: str) -> dict:
    extra = [
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    return await _try_hosts_json(scan_url_candidates(url), extra, 180)


def safe_component(value: str) -> str:
    value = (value or "Unknown").strip()
    value = re.sub(r'[\/\\:*?"<>|%$]', "_", value)
    value = re.sub(r"\s+", " ", value)
    return value[:180].strip(" .") or "Unknown"


async def _download_once(
    url: str,
    output: str,
    fmt: str,
    extra_cookies: list[dict] | None = None,
    user_agent: str | None = None,
):
    cleanup = None
    try:
        args, cleanup = common_args(extra_cookies, user_agent)
        args += [
            "--format", fmt,
            "--merge-output-format", "mp4",
            "--concurrent-fragments", "1",
            "--limit-rate", settings.download_rate,
            "--retries", "3",
            "--fragment-retries", "3",
            "--socket-timeout", "30",
            "--continue",
            "--part",
            "--no-overwrites",
            "--newline",
            "--print", "after_move:filepath",
            "--output", output,
            url,
        ]
        rc, out, err = await _run(args, timeout=4 * 60 * 60)
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        final_path = lines[-1] if lines and rc == 0 else None
        return rc, final_path, (err + "\n" + out).strip()
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


async def download_video(
    url: str,
    channel: str,
    title: str = "",
    video_id: str = "",
    upload_date: str | None = None,
):
    folder = Path(settings.download_root) / safe_component(channel)
    folder.mkdir(parents=True, exist_ok=True)

    fmt = (
        f"bestvideo[height<={settings.max_height}]+bestaudio/"
        f"best[height<={settings.max_height}]/best"
    )

    raw_date = (upload_date or "").strip()
    if len(raw_date) >= 8 and raw_date[:8].isdigit():
        date_part = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    elif raw_date:
        date_part = safe_component(raw_date)[:16]
    else:
        date_part = "NA"

    stem = (
        f"{date_part} - {safe_component(title or 'video')} "
        f"[{safe_component(video_id or 'id')}]"
    )
    output = str(folder / f"{stem}.%(ext)s")

    last_rc = 1
    last_path = None
    last_log = ""

    for candidate in download_url_candidates(url):
        try:
            rc, final_path, log = await _download_once(candidate, output, fmt)
        except asyncio.TimeoutError:
            last_rc = 1
            last_path = None
            last_log = f"{candidate}: timed out"
            continue

        if rc == 0:
            return rc, final_path, log

        last_rc, last_path, last_log = rc, final_path, log
        if not (flaresolverr_enabled() and looks_like_bot_protection(log)):
            continue

        try:
            cookies, user_agent = await fetch_flaresolverr_cookies(candidate)
            rc, final_path, log = await _download_once(
                candidate,
                output,
                fmt,
                extra_cookies=cookies,
                user_agent=user_agent,
            )
        except asyncio.TimeoutError:
            last_log = f"{last_log}\n{candidate}: timed out after FlareSolverr".strip()
            continue
        except Exception as flare_exc:
            last_log = f"{last_log}\nFlareSolverr {candidate}: {flare_exc}".strip()
            continue

        if rc == 0:
            return rc, final_path, log
        last_rc, last_path, last_log = rc, final_path, log

    return last_rc, last_path, last_log
