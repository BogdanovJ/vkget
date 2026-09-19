from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import text

from .config import settings
from .db import engine
from .models import AppState
from .scheduler import SHARE_SENTINEL, storage_ok

CACHE_KEY = "system_check_cache"
CACHE_HOURS = 6
YTDLP_BIN = os.getenv("YTDLP_BIN", "/usr/local/bin/yt-dlp")
IMAGE_YTDLP_VERSION = os.getenv("YTDLP_VERSION", "")
IMAGE_GIT_SHA = os.getenv("VKGET_GIT_SHA", "")
YTDLP_LATEST_URL = os.getenv(
    "YTDLP_LATEST_URL",
    "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
)
VERSION_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")


@dataclass
class ComponentCheck:
    id: str
    name: str
    ok: bool
    status: str
    current: str
    detail: str
    latest: str = ""


def now() -> datetime:
    return datetime.now()


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = VERSION_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_version(parts: tuple[int, int, int] | None) -> str:
    if not parts:
        return ""
    return f"{parts[0]}.{parts[1]:02d}.{parts[2]:02d}"


def format_git_sha(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text or text.lower() in {"unknown", "none"}:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", text):
        return text[:7].lower()
    return text[:12]


def _image_check() -> ComponentCheck:
    sha = format_git_sha(IMAGE_GIT_SHA)
    if sha:
        return ComponentCheck(
            "image",
            "IMAGE",
            True,
            "OK",
            sha,
            "container build SHA",
        )
    return ComponentCheck(
        "image",
        "IMAGE",
        True,
        "OK",
        "unknown",
        "local or untagged build",
    )


def _run(args: list[str], timeout: float = 8) -> tuple[int, str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    output = (result.stdout or result.stderr or "").strip()
    return result.returncode, output


def _binary_name(args: list[str]) -> str:
    return Path(args[0]).name if args else "binary"


def _binary_check(
    check_id: str,
    name: str,
    args: list[str],
    missing: str,
    *,
    timeout: float = 8,
) -> ComponentCheck:
    if not shutil.which(args[0]) and not Path(args[0]).is_file():
        return ComponentCheck(check_id, name, False, "MISSING", "—", missing)
    code, output = _run(args, timeout=timeout)
    binary = _binary_name(args)
    if code == 124:
        return ComponentCheck(
            check_id,
            name,
            False,
            "TIMEOUT",
            "—",
            f"{binary} did not finish in time",
        )
    first = output.splitlines()[0] if output else ""
    dated = format_version(parse_version(first))
    generic = re.search(r"version\s+(\S+)", first, re.I)
    version = dated or (generic.group(1) if generic else "") or first[:40] or "present"
    if code != 0:
        return ComponentCheck(
            check_id,
            name,
            False,
            "ERROR",
            version if version not in {"timed out", "present"} else "—",
            (output[-200:] if output and output != "timed out" else f"{binary} exited {code}"),
        )
    return ComponentCheck(check_id, name, True, "OK", version, first[:120])


def _compare_ytdlp(current: str, latest: str | None, *, detail: str = "") -> ComponentCheck:
    newest = format_version(parse_version(latest))
    installed = parse_version(current)
    latest_parts = parse_version(newest)
    if installed and latest_parts and installed < latest_parts:
        return ComponentCheck(
            "ytdlp",
            "YT-DLP",
            False,
            "UPDATE",
            format_version(installed),
            "Newer yt-dlp is available. Change YTDLP_VERSION in the Dockerfile, "
            "push to main, then restart the vkget deploy. An in-pod upgrade is "
            "lost on the next rollout.",
            newest,
        )
    note = detail or (
        f"Installed {current}; latest {newest}" if newest else f"Installed {current}"
    )
    return ComponentCheck("ytdlp", "YT-DLP", True, "OK", current, note, newest or "")


def _ytdlp_check(latest: str | None) -> ComponentCheck:
    check = _binary_check(
        "ytdlp",
        "YT-DLP",
        [YTDLP_BIN, "--version"],
        "yt-dlp binary is not installed in this container",
        timeout=20,
    )
    if check.status == "MISSING":
        return check

    image_ver = format_version(parse_version(IMAGE_YTDLP_VERSION))
    if check.ok:
        return _compare_ytdlp(check.current, latest)

    if image_ver:
        note = (
            f"Installed {image_ver} from the image"
            + (f"; latest {format_version(parse_version(latest))}" if latest else "")
            + f". Live probe: {check.detail}."
        )
        return _compare_ytdlp(image_ver, latest, detail=note)

    if latest:
        check.latest = format_version(parse_version(latest)) or ""
    return check


def _cookies_check() -> ComponentCheck:
    path = Path(settings.cookie_file)
    if not path.is_file():
        return ComponentCheck(
            "cookies",
            "COOKIES",
            False,
            "MISSING",
            "—",
            f"No cookies file at {path}",
        )
    if not os.access(path, os.R_OK):
        return ComponentCheck(
            "cookies",
            "COOKIES",
            False,
            "ERROR",
            str(path),
            "Cookies file is not readable",
        )
    size = path.stat().st_size
    if size < 32:
        return ComponentCheck(
            "cookies",
            "COOKIES",
            False,
            "EMPTY",
            str(path),
            "Cookies file is empty or too small",
        )
    return ComponentCheck("cookies", "COOKIES", True, "OK", str(path), f"{size} bytes")


def _storage_check() -> ComponentCheck:
    root = settings.download_root
    if storage_ok():
        return ComponentCheck("storage", "STORAGE", True, "OK", root, "writable and sentinel present")
    sentinel = os.path.join(root, SHARE_SENTINEL)
    if not os.path.isdir(root):
        detail = "download root is missing"
    elif not os.access(root, os.W_OK):
        detail = "download root is not writable"
    elif not os.path.isfile(sentinel):
        detail = (
            "missing .vkget-share sentinel; "
            f"touch {sentinel} on the host share or remount {root}"
        )
    else:
        detail = "storage probe failed"
    return ComponentCheck("storage", "STORAGE", False, "ERROR", root, detail)


def _database_check() -> ComponentCheck:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return ComponentCheck("database", "DATABASE", True, "OK", "connected", "SELECT 1 succeeded")
    except Exception as exc:
        return ComponentCheck(
            "database",
            "DATABASE",
            False,
            "ERROR",
            "—",
            f"{type(exc).__name__}: {exc}"[-180:],
        )


def fetch_latest_ytdlp(timeout: float = 5.0) -> str | None:
    try:
        response = httpx.get(
            YTDLP_LATEST_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "vkget-selfcheck",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    version = format_version(parse_version(tag))
    return version or None


def _read_cache(db) -> dict:
    row = db.get(AppState, CACHE_KEY)
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(db, payload: dict) -> None:
    raw = json.dumps(payload)
    row = db.get(AppState, CACHE_KEY)
    if row:
        row.value = raw
    else:
        db.add(AppState(key=CACHE_KEY, value=raw))
    db.commit()


def _cache_fresh(cache: dict, current: datetime) -> bool:
    raw = cache.get("latest_checked_at")
    if not raw:
        return False
    try:
        checked = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return checked >= current - timedelta(hours=CACHE_HOURS)


def collect_checks(latest_ytdlp: str | None = None) -> list[ComponentCheck]:
    return [
        _image_check(),
        _ytdlp_check(latest_ytdlp),
        _binary_check("ffmpeg", "FFMPEG", ["ffmpeg", "-version"], "ffmpeg is not installed"),
        _binary_check("ffprobe", "FFPROBE", ["ffprobe", "-version"], "ffprobe is not installed"),
        _cookies_check(),
        _storage_check(),
        _database_check(),
        ComponentCheck(
            "python",
            "PYTHON",
            True,
            "OK",
            platform_python(),
            sys.version.splitlines()[0],
        ),
    ]


def platform_python() -> str:
    return sys.version.split()[0]


def _ytdlp_update(items: list) -> bool:
    for item in items:
        if isinstance(item, dict):
            ident, status = item.get("id"), item.get("status")
        else:
            ident, status = item.id, item.status
        if ident == "ytdlp" and status == "UPDATE":
            return True
    return False


def _payload(
    items: list[ComponentCheck],
    *,
    current: datetime,
    latest: str | None,
    latest_error: str,
    latest_checked_at: str,
) -> dict:
    components = [asdict(item) for item in items]
    return {
        "checked_at": current.isoformat(),
        "latest_ytdlp": latest or "",
        "latest_checked_at": latest_checked_at,
        "latest_error": latest_error,
        "components": components,
        "ok": all(item.ok for item in items),
        "attention": any(not item.ok for item in items),
        "ytdlp_update": _ytdlp_update(items),
    }


def run_selfcheck(db, *, refresh_latest: bool = False) -> dict:
    cache = _read_cache(db)
    current = now()
    latest = str(cache.get("latest_ytdlp") or "") or None
    latest_error = str(cache.get("latest_error") or "")
    if refresh_latest or not _cache_fresh(cache, current):
        fetched = fetch_latest_ytdlp()
        if fetched:
            latest = fetched
            latest_error = ""
            cache["latest_ytdlp"] = fetched
            cache["latest_error"] = ""
            cache["latest_checked_at"] = current.isoformat()
        else:
            latest_error = "could not reach the yt-dlp release list"
            cache["latest_error"] = latest_error
            cache["latest_checked_at"] = current.isoformat()
    items = collect_checks(latest)
    payload = _payload(
        items,
        current=current,
        latest=latest,
        latest_error=latest_error,
        latest_checked_at=str(cache.get("latest_checked_at") or current.isoformat()),
    )
    cache.update(payload)
    _write_cache(db, cache)
    return payload


def load_selfcheck(
    db,
    *,
    refresh: bool = False,
    fetch_if_needed: bool = False,
    live: bool = True,
) -> dict:
    cache = _read_cache(db)
    if refresh or (fetch_if_needed and not cache.get("latest_checked_at")):
        return run_selfcheck(db, refresh_latest=True)
    if not live and cache.get("components"):
        components = cache.get("components") or []
        return {
            "checked_at": cache.get("checked_at") or now().isoformat(),
            "latest_ytdlp": cache.get("latest_ytdlp") or "",
            "latest_checked_at": cache.get("latest_checked_at") or "",
            "latest_error": cache.get("latest_error") or "",
            "components": components,
            "ok": bool(cache.get("ok")),
            "attention": bool(cache.get("attention")),
            "ytdlp_update": bool(cache.get("ytdlp_update")) or _ytdlp_update(components),
        }
    latest = str(cache.get("latest_ytdlp") or "") or None
    items = collect_checks(latest)
    payload = _payload(
        items,
        current=now(),
        latest=latest,
        latest_error=str(cache.get("latest_error") or ""),
        latest_checked_at=str(cache.get("latest_checked_at") or ""),
    )
    cache.update(payload)
    _write_cache(db, cache)
    return payload


def selfcheck_summary(report: dict) -> dict:
    wanted = ("ytdlp", "ffmpeg", "storage", "cookies")
    by_id = {item["id"]: item for item in report.get("components") or []}
    return {
        "ok": bool(report.get("ok")),
        "attention": bool(report.get("attention")),
        "status": "ATTENTION" if report.get("attention") else "OK",
        "components": [by_id[key] for key in wanted if key in by_id],
        "checked_at": report.get("checked_at") or "",
    }
