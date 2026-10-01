from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time

import shutil
import tempfile

from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

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
        "vk",
        "vk video",
        "vk видео",
        "вконтакте",
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


_IDENTITY_QUERY_KEYS = frozenset({"album_id", "section"})
_AT_PATH_RE = re.compile(r"^/(?:video|videos)/(@[^/]+)$", re.I)
_PLAYLIST_PATH_RE = re.compile(r"^/(?:video/)?playlist/(-?\d+_\d+)$", re.I)


def _vk_identity_host(host: str) -> str | None:
    """Collapse vk.com / vk.ru / vkvideo.ru, including www, m, and new."""
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for prefix in ("m.", "new."):
        if host.startswith(prefix):
            rest = host[len(prefix) :]
            if rest in {"vk.com", "vk.ru", "vkvideo.ru"}:
                host = rest
                break
    if host in {"vk.com", "vk.ru", "vkvideo.ru"}:
        return "vkvideo.ru"
    return None


def _canonical_vk_subscription_path(path: str) -> str:
    """One path for the channel page and for a playlist id."""
    match = _AT_PATH_RE.match(path)
    if match:
        return "/" + match.group(1)
    match = _PLAYLIST_PATH_RE.match(path)
    if match:
        return "/playlist/" + match.group(1)
    return path


def _identity_query(query: str) -> str:
    """Keep album selectors. Drop overlay and tracking parameters."""
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(query or "", keep_blank_values=False):
        if key.lower() in _IDENTITY_QUERY_KEYS and value:
            kept.append((key.lower(), value))
    kept.sort()
    return urlencode(kept)


def subscription_source_key(url: str) -> str:
    """Identity of a playlist or channel.

    Host mirrors, tracking queries, and a video opened on top of the channel
    are the same subscription. A different playlist id is not.
    """
    normalized = normalize_vk_url(url)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    path = unquote(parsed.path or "")
    if len(path) > 1:
        path = path.rstrip("/")
    canonical_host = _vk_identity_host(host)
    if canonical_host:
        host = canonical_host
        path = _canonical_vk_subscription_path(path)
    path = path.casefold()
    key = f"{host}{path}"
    query = _identity_query(parsed.query)
    if query:
        key = f"{key}?{query}"
    return key[:500]


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


def _is_reserved_name(text: str) -> bool:
    """Placeholder labels, including the last-resort `_single` folder token."""
    lowered = text.casefold()
    return lowered in _UNUSABLE_VIDEO_TITLES or lowered == "_single"


def is_usable_channel(name: str | None) -> bool:
    text = (name or "").strip()
    if not text:
        return False
    if _is_reserved_name(text):
        return False
    if _VK_ID_RE.fullmatch(text) or _VK_SLUG_RE.fullmatch(text):
        return False
    return True


def is_usable_folder(name: str | None) -> bool:
    """Folders may be playlist IDs; they may not be scan leftovers like Subscription."""
    text = (name or "").strip()
    if not text or text in {".", ".."}:
        return False
    return not _is_reserved_name(text)


def download_folder_name(*, subscription_title: str = "", channel: str = "") -> str:
    """Keep one subscription's files together; never use Subscription/Unknown/NA."""
    for candidate in (subscription_title, channel):
        text = (candidate or "").strip()
        if is_usable_folder(text):
            return text
    return "_single"


def composed_title(
    title: str | None = "",
    *,
    channel: str = "",
    upload_date: str | None = None,
    external_id: str = "",
) -> str:
    """Human name for the UI. Untitled only when nothing else exists."""
    text = (title or "").strip()
    if is_usable_video_title(text, external_id):
        return text
    date_part = format_upload_date(upload_date)
    channel_name = (channel or "").strip()
    if not is_usable_channel(channel_name):
        channel_name = ""
    if channel_name and date_part:
        return f"{channel_name} · {date_part}"
    if channel_name:
        return channel_name
    if date_part:
        return f"Video {date_part}"
    ext = str(external_id or "").strip()
    if ext:
        return f"Video {ext}"
    return "Untitled"


def filename_title(
    title: str | None,
    external_id: str = "",
    *,
    channel: str = "",
    upload_date: str | None = None,
) -> str:
    """File title from yt-dlp's name. Never the channel."""
    del channel, upload_date
    text = (title or "").strip()
    if is_usable_video_title(text, external_id):
        return text
    ext = str(external_id or "").strip()
    if ext:
        return f"Video {ext}"
    return "Untitled"


def format_upload_date(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    digits = text.replace("-", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def download_stem(
    title: str,
    video_id: str,
    upload_date: str | None = None,
    channel: str = "",
) -> str:
    """{title} [{id}]-{YYYY-MM-DD}. The date suffix is omitted when unknown."""
    del channel
    date_part = format_upload_date(upload_date)
    name = safe_component(
        filename_title(title, video_id),
        default="Untitled",
    )
    vid = safe_component(video_id or "id", default="id")[:80]
    suffix = f" [{vid}]"
    if date_part:
        suffix = f"{suffix}-{date_part}"
    # yt-dlp writes "{stem}.%(ext)s" before the real extension replaces it.
    template_extra = len(".%(ext)s".encode("utf-8"))
    room = 255 - template_extra - len(suffix.encode("utf-8"))
    name = truncate_utf8(name, max(1, room)).rstrip(" .") or "Untitled"
    stem = f"{name}{suffix}"
    while len((stem + ".%(ext)s").encode("utf-8")) > 255 and name:
        name = truncate_utf8(name, len(name.encode("utf-8")) - 1).rstrip(" .")
        stem = f"{name}{suffix}" if name else suffix.strip()
    return stem


def dated_filename(path: str, video_id: str) -> bool:
    """True when the file is already `{title} [{id}]-{YYYY-MM-DD}.ext`."""
    vid = safe_component(video_id or "", default="")
    if not vid:
        return False
    name = Path(path or "").name
    return bool(
        re.search(
            rf" \[{re.escape(vid)}\]-\d{{4}}-\d{{2}}-\d{{2}}\.[^.]+$",
            name,
        )
    )


def _download_dir(folder: str) -> Path:
    dir_name = safe_component(folder, default="_single")
    if not is_usable_folder(dir_name):
        dir_name = "_single"
    return Path(settings.download_root) / dir_name


def build_partial_output(*, folder: str, video_id: str) -> Path:
    """Stable in-progress name so retries find the same partial."""
    vid = safe_component(video_id or "id", default="id")[:80]
    return _download_dir(folder) / f"[{vid}].%(ext)s"


def build_download_output(
    *,
    folder: str,
    title: str,
    video_id: str,
    upload_date: str | None = None,
    channel: str = "",
) -> Path:
    stem = download_stem(title, video_id, upload_date, channel=channel)
    return _download_dir(folder) / f"{stem}.%(ext)s"


_NO_DATA_BLOCKS = "did not get any data blocks"
_TINY_PART_BYTES = 64 * 1024
_EMPTY_MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".m4a", ".mp3"}


def download_output_prefix(output: str) -> str:
    name = Path(output).name
    if name.endswith(".%(ext)s"):
        return name[: -len(".%(ext)s")]
    return Path(output).stem


def iter_download_artifacts(output: str):
    parent = Path(output).parent
    prefix = download_output_prefix(output)
    if not prefix or not parent.is_dir():
        return
    for path in parent.iterdir():
        if path.is_file() and path.name.startswith(prefix):
            yield path


# yt-dlp names an unmerged stream "{stem}.f{format_id}.{ext}".
# VK format ids are not only digits: dash_sep-11, url720, hls-0.
_FRAGMENT_RE = re.compile(
    r"\.f(?:\d+|dash[_-][^./]+|url\d+|hls[_-][^./]+)\.",
    re.I,
)
_ID_IN_BRACKETS_RE = re.compile(r"\[(-?\d+_\d+)\]")
_MEDIA_SUFFIXES = _EMPTY_MEDIA_SUFFIXES | {".mov", ".m4v"}
_AUDIO_ONLY_SUFFIXES = {".m4a", ".mp3"}


def is_partial_download(path: Path) -> bool:
    """yt-dlp temps: .part, .ytdl, unmerged DASH/HLS streams, and .part-Frag pieces.

    A finished audio stream such as [{id}].fdash_sep-11.m4a is only one half of
    the video. It stays on disk so the next attempt can continue, and it is not
    a completed download.
    """
    name = path.name
    lowered = name.lower()
    if lowered.endswith(".part") or lowered.endswith(".ytdl") or ".part" in lowered:
        return True
    if _FRAGMENT_RE.search(lowered):
        return True
    if Path(lowered).suffix in _AUDIO_ONLY_SUFFIXES and _ID_IN_BRACKETS_RE.search(name):
        return True
    return False


def _unlink_file(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def remove_stale_download_parts(output: str, *, force: bool = False) -> list[str]:
    """Delete leftover yt-dlp temps that make --continue resume a dead download.

    A partial set is kept together when any member is large enough to resume,
    including a small .ytdl sidecar. force=True drops the whole set.
    """
    partials: list[tuple[Path, int]] = []
    empties: list[Path] = []
    for path in iter_download_artifacts(output):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if is_partial_download(path):
            partials.append((path, size))
        elif size == 0 and path.suffix.lower() in _EMPTY_MEDIA_SUFFIXES:
            empties.append(path)
    removed: list[str] = []
    drop_partials = bool(partials) and (
        force or all(size < _TINY_PART_BYTES for _path, size in partials)
    )
    if drop_partials:
        for path, _size in partials:
            if _unlink_file(path):
                removed.append(str(path))
    for path in empties:
        if _unlink_file(path):
            removed.append(str(path))
    return removed


def partial_video_id(name: str) -> str | None:
    match = _ID_IN_BRACKETS_RE.search(name or "")
    if not match:
        return None
    return match.group(1)


def _partial_media_base(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".part"):
        base = name[: -len(".part")]
    elif lower.endswith(".ytdl"):
        base = name[: -len(".ytdl")]
    elif ".part" in lower:
        base = name[: lower.index(".part")]
    else:
        base = name
    match = _FRAGMENT_RE.search(base)
    if match:
        base = base[: match.start()]
    return base


def _has_finished_sibling(path: Path) -> bool:
    """True when a real media file sits beside this temp and shares its stem."""
    base = _partial_media_base(path.name)
    if not base or base == path.name:
        return False
    parent = path.parent
    candidates = [parent / base]
    if Path(base).suffix.lower() not in _MEDIA_SUFFIXES:
        candidates.extend(parent / f"{base}{ext}" for ext in _EMPTY_MEDIA_SUFFIXES)
    for candidate in candidates:
        if candidate == path or not is_saved_media(candidate):
            continue
        return True
    return False


def remove_orphan_partials(root: str, protected_ids: set[str]) -> list[str]:
    """Delete leftover temps that are not an active or retryable download.

    protected_ids are catalogue ids still queued, paused, failed, or downloading.
    A .part with no video id is removed only when a finished file with the same
    stem is already beside it.
    """
    root_path = Path(root or "")
    if not root_path.is_dir():
        return []
    protected = {
        safe_component(item, default="")
        for item in protected_ids
        if str(item or "").strip()
    }
    protected.discard("")
    try:
        root_resolved = root_path.resolve()
    except OSError:
        return []
    removed: list[str] = []
    for path in root_path.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        if not is_partial_download(path):
            continue
        try:
            path.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        vid = partial_video_id(path.name)
        if vid and safe_component(vid, default="") in protected:
            continue
        if vid or _has_finished_sibling(path):
            if _unlink_file(path):
                removed.append(str(path))
    return removed


def looks_like_no_data_blocks(log: str | None) -> bool:
    return _NO_DATA_BLOCKS in (log or "").lower()


_META_MARK = "vkget_meta:"
_PATH_MARK = "vkget_path:"


def parse_download_marks(text: str) -> tuple[str | None, dict[str, str]]:
    """Read title, date, id, and filepath from marked yt-dlp --print lines."""
    path = None
    meta: dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(_PATH_MARK):
            found = stripped[len(_PATH_MARK) :].strip()
            if found:
                path = found
            continue
        if not stripped.startswith(_META_MARK):
            continue
        payload = stripped[len(_META_MARK) :]
        if "\t" not in payload:
            continue
        upload_date, rest = payload.split("\t", 1)
        if "\t" not in rest:
            continue
        title, video_id = rest.rsplit("\t", 1)
        meta = {
            "upload_date": upload_date.strip(),
            "title": title.replace("\n", " ").strip(),
            "id": video_id.strip(),
        }
    return path, meta


def is_bare_id_file(path: Path, video_id: str) -> bool:
    """True for the in-progress template `[{id}].ext` and nothing else."""
    vid = safe_component(video_id or "", default="")
    if not vid:
        return False
    return bool(re.fullmatch(rf"\[{re.escape(vid)}\]\.[^.]+", path.name, re.I))


def is_saved_media(path: Path) -> bool:
    """A non-empty finished media file, not a temp, hidden file, or compat sidecar."""
    if not path.is_file() or path.is_symlink() or path.name.startswith("."):
        return False
    if is_partial_download(path) or path.name.lower().endswith(".compat.mp4"):
        return False
    if path.suffix.lower() not in _MEDIA_SUFFIXES:
        return False
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _video_id_token(video_id: str) -> str:
    vid = safe_component(video_id or "", default="")
    if not vid:
        return ""
    return f"[{vid}]"


def find_existing_video_files(root: str, video_id: str) -> tuple[Path | None, Path | None]:
    """Return (best named file, bare `[{id}].ext` file) for this video id."""
    token = _video_id_token(video_id)
    if not token:
        return None, None
    try:
        root_text = os.fspath(root)
    except TypeError:
        return None, None
    if not isinstance(root_text, str) or not root_text:
        return None, None
    root_path = Path(root_text)
    if not root_path.is_dir():
        return None, None
    try:
        root_resolved = root_path.resolve()
    except OSError:
        return None, None
    named: list[Path] = []
    bare: list[Path] = []
    for path in root_path.rglob("*"):
        if not is_saved_media(path) or token not in path.name:
            continue
        try:
            path.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        if is_bare_id_file(path, video_id):
            bare.append(path)
        else:
            named.append(path)

    def rank(path: Path) -> tuple[int, int]:
        dated = 1 if dated_filename(str(path), video_id) else 0
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return dated, size

    named.sort(key=rank, reverse=True)
    bare.sort(key=rank, reverse=True)
    return (named[0] if named else None, bare[0] if bare else None)


def _folder_directory(root: str, folder: str) -> Path:
    dir_name = safe_component(folder, default="_single")
    if not is_usable_folder(dir_name):
        dir_name = "_single"
    return Path(root) / dir_name


def adopt_existing_download(
    root: str,
    *,
    video_id: str,
    title: str,
    upload_date: str | None = None,
    folder: str = "",
) -> str | None:
    """Use a file already on disk. Rename a bare id file; never overwrite a real one."""
    named, bare = find_existing_video_files(root, video_id)
    if named:
        if (
            bare
            and bare.resolve() != named.resolve()
            and is_bare_id_file(bare, video_id)
        ):
            _unlink_file(bare)
        return str(named)
    if not bare or not is_bare_id_file(bare, video_id):
        return None
    placed = place_downloaded_file(
        str(bare),
        title=title,
        video_id=video_id,
        upload_date=upload_date,
        directory=_folder_directory(root, folder),
    )
    if placed and Path(placed).is_file() and not is_bare_id_file(Path(placed), video_id):
        return placed
    return None


def looks_like_already_downloaded(log: str | None) -> bool:
    text = (log or "").lower()
    if "already been downloaded" in text or "already downloaded" in text:
        return True
    return "file exists" in text and "errno 17" in text


def resolve_on_disk_download(output: str, reported: str | None) -> str:
    """The real media file for this output template, ignoring yt-dlp status lines."""
    candidate = Path(reported or "")
    if is_saved_media(candidate):
        return str(candidate)
    parent = Path(output).parent
    prefix = download_output_prefix(output)
    if not prefix or not parent.is_dir():
        return ""
    found: list[Path] = []
    for path in parent.iterdir():
        if path.is_file() and path.name.startswith(prefix) and is_saved_media(path):
            found.append(path)
    if not found:
        return ""
    found.sort(key=lambda path: path.stat().st_size, reverse=True)
    return str(found[0])


def place_downloaded_file(
    src: str,
    *,
    title: str,
    video_id: str,
    upload_date: str | None = None,
    folder: str | None = None,
    directory: Path | None = None,
) -> str:
    """Move a finished download to `{title} [{id}]-{YYYY-MM-DD}.ext`.

    A non-empty destination is kept. A 0-byte destination is replaced.
    Only a bare `[{id}].ext` source is removed when the destination stays.
    """
    source = Path(src or "")
    if not source.name:
        return src
    if is_partial_download(source):
        return src
    suffix = source.suffix or ".mp4"
    stem = download_stem(title, video_id, upload_date) + suffix
    if directory is not None:
        dest = Path(directory) / stem
    elif folder:
        dest = _download_dir(folder) / stem
    else:
        parent = source.parent if str(source.parent) not in {"", "."} else Path(".")
        dest = parent / stem
    if dest.exists() and dest.is_file():
        try:
            dest_size = dest.stat().st_size
        except OSError:
            dest_size = 1
        same = False
        try:
            same = source.exists() and dest.resolve() == source.resolve()
        except OSError:
            same = False
        if same:
            return str(source)
        if dest_size > 0:
            if source.is_file() and is_bare_id_file(source, video_id):
                _unlink_file(source)
            return str(dest)
        _unlink_file(dest)
    if not source.is_file():
        return str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(dest)
    except OSError:
        if dest.is_file():
            try:
                if dest.stat().st_size > 0:
                    return str(dest)
            except OSError:
                pass
        return str(source)
    return str(dest)


def path_inside_root(path: str, root: str) -> bool:
    candidate = Path(path or "")
    if not candidate.is_absolute() or candidate.is_symlink():
        return False
    try:
        root_resolved = Path(root).resolve()
        resolved = candidate.resolve()
        resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return False
    return resolved != root_resolved and candidate.is_file()


_ENTRY_TITLE_KEYS = (
    "title",
    "fulltitle",
    "alt_title",
    "track",
    "series",
    "episode_title",
    "chapter",
)
_PAGE_TITLE_SUFFIXES = (
    re.compile(r"\s*[|–—-]\s*VK Video\s*$", re.I),
    re.compile(r"\s*[|–—-]\s*VK Видео\s*$", re.I),
    re.compile(r"\s*[|–—-]\s*VK\s*$", re.I),
    re.compile(r"\s*[|–—-]\s*ВКонтакте\s*$", re.I),
)
_OG_TITLE_RE = re.compile(
    r"<meta[^>]+(?:property|name)=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
    re.I,
)
_OG_TITLE_RE_SWAP = re.compile(
    r"<meta[^>]+content=['\"]([^'\"]+)['\"][^>]+(?:property|name)=['\"]og:title['\"]",
    re.I,
)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _first_description_line(text: str, external_id: str = "") -> str:
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if is_usable_video_title(line, external_id):
            return line
    return ""


def clean_page_title(text: str | None, external_id: str = "") -> str:
    value = html.unescape((text or "").strip())
    value = re.sub(r"\s+", " ", value)
    for suffix in _PAGE_TITLE_SUFFIXES:
        value = suffix.sub("", value).strip()
    if is_usable_video_title(value, external_id):
        return value
    return ""


def title_from_html(markup: str | None, external_id: str = "") -> str:
    text = markup or ""
    for pattern in (_OG_TITLE_RE, _OG_TITLE_RE_SWAP):
        match = pattern.search(text)
        if match:
            cleaned = clean_page_title(match.group(1), external_id)
            if cleaned:
                return cleaned
    match = _HTML_TITLE_RE.search(text)
    if match:
        return clean_page_title(re.sub(r"<[^>]+>", "", match.group(1)), external_id)
    return ""


def title_from_entry(entry: dict | None, external_id: str = "") -> str:
    if not isinstance(entry, dict):
        return ""
    for key in _ENTRY_TITLE_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and is_usable_video_title(value, external_id):
            return value.strip()
    for key in ("description", "synopsis"):
        value = entry.get(key)
        if isinstance(value, str):
            line = _first_description_line(value, external_id)
            if line:
                return line
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
    proxy: str | None = None,
) -> tuple[list[str], str | None]:
    args = ["/usr/local/bin/yt-dlp"]
    cookie_path, cleanup = _write_cookie_file(extra_cookies)
    if cookie_path:
        args += ["--cookies", cookie_path]
    if user_agent:
        args += ["--user-agent", user_agent]
    if proxy:
        args += ["--proxy", proxy]
    return args, cleanup


_YTDLP_SPEED_RE = re.compile(
    r"\[download\].*?at\s+([0-9]*\.?[0-9]+)\s*([KMGT]i?B)/s",
    re.I,
)


def parse_ytdlp_speed_bps(line: str) -> int | None:
    match = _YTDLP_SPEED_RE.search(line or "")
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = 1
    if unit.startswith("K"):
        multiplier = 1024
    elif unit.startswith("M"):
        multiplier = 1024 * 1024
    elif unit.startswith("G"):
        multiplier = 1024 * 1024 * 1024
    elif unit.startswith("T"):
        multiplier = 1024 * 1024 * 1024 * 1024
    return int(number * multiplier)


def _vpn_speed_limits(proxy: str | None) -> tuple[int | None, int | None]:
    if not proxy:
        return None, None
    rate = getattr(settings, "vpn_min_download_rate", None)
    duration = getattr(settings, "vpn_slow_rate_duration", None)
    if not isinstance(rate, str):
        return None, None
    from .vpn.util import parse_rate_bps

    parsed = parse_rate_bps(rate)
    if not parsed:
        return None, None
    if not isinstance(duration, int):
        duration = 120
    return parsed, max(duration, 1)


async def _run(
    args: list[str],
    timeout: int | None = None,
    min_rate_bps: int | None = None,
    slow_rate_duration: int | None = None,
):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if min_rate_bps is None or slow_rate_duration is None:
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

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    below_since: float | None = None
    slow = False

    async def consume(stream, bucket: list[str]):
        nonlocal below_since, slow
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace")
            bucket.append(text)
            speed = parse_ytdlp_speed_bps(text)
            if speed is None:
                continue
            if speed < min_rate_bps:
                if below_since is None:
                    below_since = time.monotonic()
                elif time.monotonic() - below_since >= slow_rate_duration:
                    slow = True
                    proc.kill()
                    return
            else:
                below_since = None

    try:
        await asyncio.wait_for(
            asyncio.gather(
                consume(proc.stdout, stdout_chunks),
                consume(proc.stderr, stderr_chunks),
                proc.wait(),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    out = "".join(stdout_chunks)
    err = "".join(stderr_chunks)
    if slow:
        err = f"{err}\nVPN_SLOW_RATE: download stayed below minimum rate".strip()
        return 1, out, err
    return proc.returncode, out, err


async def _flaresolverr_solution(url: str) -> dict:
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
    if not cookies:
        raise RuntimeError("FlareSolverr returned no cookies")
    print(f"vkget: FlareSolverr cookies for {url}", flush=True)
    return solution


async def fetch_flaresolverr_cookies(url: str) -> tuple[list[dict], str | None]:
    solution = await _flaresolverr_solution(url)
    return solution.get("cookies") or [], solution.get("userAgent") or None


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


def _cookie_map(extra_cookies: list[dict] | None = None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if extra_cookies:
        for cookie in extra_cookies:
            name = str(cookie.get("name") or "").strip()
            if name:
                cookies[name] = str(cookie.get("value") or "")
        return cookies
    path = settings.cookie_file
    if not path or not os.path.exists(path):
        return cookies
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except OSError:
        return cookies
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if not stripped.startswith("#HttpOnly_"):
                continue
            stripped = stripped[len("#HttpOnly_") :]
        parts = stripped.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


async def _get_page_title(
    url: str,
    external_id: str = "",
    *,
    extra_cookies: list[dict] | None = None,
    user_agent: str | None = None,
) -> tuple[str, str]:
    headers = {"User-Agent": user_agent or "Mozilla/5.0 vkget"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = await client.get(
                url,
                headers=headers,
                cookies=_cookie_map(extra_cookies),
            )
    except Exception as exc:
        return "", str(exc)
    if response.status_code >= 400:
        return "", f"HTTP {response.status_code} {url}"
    title = title_from_html(response.text, external_id)
    if title:
        return title, ""
    if looks_like_bot_protection(response.text):
        return "", f"challenge: {url}"
    return "", f"no title: {url}"


async def fetch_page_title(url: str, external_id: str = "") -> str:
    """og:title / <title> when yt-dlp JSON has no usable name."""
    for candidate in download_url_candidates(url):
        title, err = await _get_page_title(candidate, external_id)
        if title:
            return title
        if not (flaresolverr_enabled() and looks_like_bot_protection(err)):
            continue
        try:
            solution = await _flaresolverr_solution(candidate)
        except Exception:
            continue
        html_title = title_from_html(str(solution.get("response") or ""), external_id)
        if html_title:
            return html_title
        title, _ = await _get_page_title(
            candidate,
            external_id,
            extra_cookies=solution.get("cookies") or [],
            user_agent=solution.get("userAgent") or None,
        )
        if title:
            return title
    return ""


async def inspect_url(url: str) -> dict:
    extra = [
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    return await _try_hosts_json(download_url_candidates(url), extra, 120)


async def resolve_video_metadata(
    url: str,
    *,
    title: str = "",
    channel: str = "",
    upload_date: str | None = None,
    external_id: str = "",
) -> dict[str, str]:
    """Fill title/channel/date from a full inspect when scan leftovers are URL bits."""
    result = {
        "title": (title or "").strip(),
        "channel": (channel or "").strip(),
        "upload_date": (upload_date or "").strip(),
    }
    if not format_upload_date(result["upload_date"]):
        result["upload_date"] = ""
    needs_inspect = (
        not is_usable_video_title(result["title"], external_id)
        or not is_usable_channel(result["channel"])
        or not result["upload_date"]
    )
    if not needs_inspect:
        return result
    info: dict = {}
    try:
        info = await inspect_url(url)
    except Exception:
        info = {}
    ext = str(info.get("id") or external_id)
    resolved = title_from_entry(info, ext)
    if resolved:
        result["title"] = resolved
    ch = (
        info.get("channel")
        or info.get("uploader")
        or info.get("playlist_uploader")
        or ""
    )
    if isinstance(ch, str) and is_usable_channel(ch):
        result["channel"] = ch.strip()
    raw_date = info.get("upload_date") or info.get("release_date")
    if raw_date:
        result["upload_date"] = str(raw_date).strip()
    elif not result["upload_date"]:
        ts = info.get("timestamp") or info.get("release_timestamp")
        try:
            stamp = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            result["upload_date"] = stamp.strftime("%Y%m%d")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    if not is_usable_video_title(result["title"], external_id or ext):
        page_title = await fetch_page_title(url, ext or external_id)
        if page_title:
            result["title"] = page_title
    return result


async def resolve_video_title(
    url: str,
    current: str,
    external_id: str = "",
) -> str:
    """Inspect a single video URL only when the stored/playlist title is unusable."""
    current = (current or "").strip()
    if is_usable_video_title(current, external_id):
        return current
    meta = await resolve_video_metadata(
        url,
        title=current,
        external_id=external_id,
    )
    return meta["title"] or current


async def inspect_playlist_flat(url: str) -> dict:
    extra = [
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    return await _try_hosts_json(scan_url_candidates(url), extra, 180)


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Cut on a character boundary so the UTF-8 encoding fits in max_bytes."""
    if max_bytes < 1:
        return ""
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def safe_component(value: str, *, default: str = "Unknown") -> str:
    value = (value or default).strip()
    value = re.sub(r'[\/\\:*?"<>|%$]', "_", value)
    value = re.sub(r"[\x00-\x1f]", "", value)
    value = re.sub(r"\s+", " ", value)
    # NAME_MAX is 255 bytes. Keep folder names shorter than that.
    value = truncate_utf8(value, 180).strip(" .")
    return value or default


def download_format(max_height: int) -> str:
    """Prefer H.264 + AAC MP4 so Samsung TVs can play the file."""
    h = int(max_height)
    return "/".join(
        [
            f"best[ext=mp4][vcodec^=avc][height<={h}]",
            f"best[ext=mp4][vcodec^=h264][height<={h}]",
            f"bestvideo[vcodec^=avc1][height<={h}]+bestaudio[acodec^=mp4a]",
            f"bestvideo[vcodec^=avc][height<={h}]+bestaudio[acodec^=mp4a]",
            f"bestvideo[vcodec^=avc1][height<={h}]+bestaudio",
            f"bestvideo[vcodec^=h264][height<={h}]+bestaudio",
            f"best[ext=mp4][height<={h}]",
            f"bestvideo[height<={h}]+bestaudio",
            f"best[height<={h}]",
        ]
    )


TV_VIDEO_CODECS = frozenset({"h264"})
TV_AUDIO_CODECS = frozenset({"aac"})
TV_PIXEL_FORMATS = frozenset({"yuv420p", "yuvj420p"})
TV_VIDEO_PROFILES = frozenset(
    {
        "baseline",
        "constrained baseline",
        "main",
        "high",
        "constrained high",
        "progressive high",
    }
)
TV_AUDIO_PROFILES = frozenset({"lc", "aac-lc", "aac_lc", "low complexity"})
TV_SAMPLE_RATES = frozenset({44100, 48000})
TV_MAX_LEVEL = 41
TV_MAX_WIDTH = 1280
TV_VIDEO_TAGS = frozenset({"avc1"})
TV_PROGRESSIVE = frozenset({"", "progressive", "unknown"})
def _is_attached_pic(stream: dict) -> bool:
    disposition = stream.get("disposition") or {}
    return bool(disposition.get("attached_pic"))


def _probe_streams(probe: dict) -> tuple[dict | None, dict | None]:
    streams = probe.get("streams") or []
    video = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video" and not _is_attached_pic(stream)
        ),
        None,
    )
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    return video, audio


def _norm_profile(value: object) -> str:
    return str(value or "").strip().casefold()


def _int_field(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def tv_max_size(max_height: int | None = None) -> tuple[int, int]:
    height = int(max_height if max_height is not None else settings.max_height)
    return TV_MAX_WIDTH, height


def tv_codec_plan(probe: dict, max_height: int | None = None) -> dict[str, bool]:
    video, audio = _probe_streams(probe)
    max_w, max_h = tv_max_size(max_height)
    vcodec = ((video or {}).get("codec_name") or "").lower()
    pix = ((video or {}).get("pix_fmt") or "").lower()
    profile = _norm_profile((video or {}).get("profile"))
    level = _int_field((video or {}).get("level"))
    width = _int_field((video or {}).get("width"))
    height = _int_field((video or {}).get("height"))
    tag = ((video or {}).get("codec_tag_string") or "").strip().lower()
    field = ((video or {}).get("field_order") or "").strip().lower()
    video_ok = bool(
        video
        and vcodec in TV_VIDEO_CODECS
        and pix in TV_PIXEL_FORMATS
        and profile in TV_VIDEO_PROFILES
        and level is not None
        and 0 < level <= TV_MAX_LEVEL
        and width is not None
        and height is not None
        and width % 2 == 0
        and height % 2 == 0
        and 0 < width <= max_w
        and 0 < height <= max_h
        and tag in TV_VIDEO_TAGS
        and field in TV_PROGRESSIVE
    )

    acodec = ((audio or {}).get("codec_name") or "").lower()
    aprofile = _norm_profile((audio or {}).get("profile"))
    channels = _int_field((audio or {}).get("channels"))
    sample_rate = _int_field((audio or {}).get("sample_rate"))
    has_audio = audio is not None
    audio_ok = (not has_audio) or (
        acodec in TV_AUDIO_CODECS
        and aprofile in TV_AUDIO_PROFILES
        and channels is not None
        and 0 < channels <= 2
        and sample_rate in TV_SAMPLE_RATES
    )
    return {
        "has_video": video is not None,
        "has_audio": has_audio,
        "copy_video": video_ok,
        "copy_audio": has_audio and audio_ok,
    }


def ffmpeg_tv_args(
    src: str,
    dest: str,
    probe: dict,
    max_height: int | None = None,
) -> list[str]:
    plan = tv_codec_plan(probe, max_height=max_height)
    args = [
        "ffmpeg",
        "-y",
        "-i",
        src,
        "-map",
        "0:V:0",
    ]
    if plan["has_audio"]:
        args += ["-map", "0:a:0"]
    args += ["-sn", "-dn"]
    if plan["copy_video"]:
        args += ["-c:v", "copy", "-tag:v", "avc1"]
    else:
        max_w, max_h = tv_max_size(max_height)
        vf = (
            f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        )
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-level",
            "4.0",
            "-tag:v",
            "avc1",
            "-vf",
            vf,
        ]
    if plan["has_audio"]:
        if plan["copy_audio"]:
            args += ["-c:a", "copy"]
        else:
            args += [
                "-c:a",
                "aac",
                "-profile:a",
                "aac_low",
                "-b:a",
                "160k",
                "-ac",
                "2",
                "-ar",
                "48000",
            ]
    args += ["-movflags", "+faststart", "-f", "mp4", dest]
    return args


def mp4_has_faststart(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            while True:
                header = handle.read(8)
                if len(header) < 8:
                    return False
                size = int.from_bytes(header[:4], "big")
                box = header[4:8]
                if box == b"moov":
                    return True
                if box == b"mdat":
                    return False
                if size == 1:
                    ext = handle.read(8)
                    if len(ext) < 8:
                        return False
                    size = int.from_bytes(ext, "big")
                    skip = size - 16
                elif size == 0:
                    return False
                else:
                    skip = size - 8
                if skip < 0:
                    return False
                handle.seek(skip, os.SEEK_CUR)
    except OSError:
        return False


def is_mp4_container(probe: dict) -> bool:
    name = ((probe.get("format") or {}).get("format_name") or "").lower()
    return "mp4" in {part.strip() for part in name.split(",") if part.strip()}


def is_tv_ready(probe: dict, path: str = "", max_height: int | None = None) -> bool:
    """True when the file already matches the Samsung-safe H.264/AAC-LC MP4 profile."""
    plan = tv_codec_plan(probe, max_height=max_height)
    if not plan["has_video"] or not plan["copy_video"]:
        return False
    if plan["has_audio"] and not plan["copy_audio"]:
        return False
    if path and Path(path).suffix.lower() != ".mp4":
        return False
    if not is_mp4_container(probe):
        return False
    if path and not mp4_has_faststart(path):
        return False
    return True


async def probe_media(path: str) -> dict:
    rc, out, err = await _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,codec_tag_string,"
            "profile,level,pix_fmt,width,height,field_order,channels,sample_rate:"
            "stream_disposition=attached_pic",
            "-of",
            "json",
            path,
        ],
        timeout=60,
    )
    if rc != 0:
        raise RuntimeError(err.strip() or out.strip() or "ffprobe failed")
    return json.loads(out or "{}")


async def ensure_tv_compatible(path: str, *, force_remux: bool = True) -> str:
    """Remux or transcode to H.264/AAC-LC MP4 with faststart for Smart TVs."""
    src = Path(path)
    if not src.is_file():
        return path
    probe = await probe_media(str(src))
    if not tv_codec_plan(probe)["has_video"]:
        raise RuntimeError("downloaded file has no video stream")
    if not force_remux and is_tv_ready(probe, str(src)):
        return str(src)
    dest = src.with_name(f"{src.stem}.compat.mp4")
    rc, out, err = await _run(
        ffmpeg_tv_args(str(src), str(dest), probe),
        timeout=2 * 60 * 60,
    )
    if rc != 0:
        try:
            dest.unlink()
        except OSError:
            pass
        raise RuntimeError(err.strip() or out.strip() or "ffmpeg failed")
    final = src if src.suffix.lower() == ".mp4" else src.with_suffix(".mp4")
    dest.replace(final)
    if final != src and src.exists():
        try:
            src.unlink()
        except OSError:
            pass
    return str(final)


async def _download_once(
    url: str,
    output: str,
    fmt: str,
    extra_cookies: list[dict] | None = None,
    user_agent: str | None = None,
    proxy: str | None = None,
):
    cleanup = None
    try:
        args, cleanup = common_args(extra_cookies, user_agent, proxy=proxy)
        args += [
            "--format", fmt,
            "--merge-output-format", "mp4",
            "--remux-video", "mp4",
            "--concurrent-fragments", "1",
            "--limit-rate", settings.download_rate,
            "--retries", "3",
            "--fragment-retries", "3",
            "--socket-timeout", "30",
            "--continue",
            "--part",
            "--no-overwrites",
            "--newline",
            "--print", "after_move:vkget_path:%(filepath)s",
            "--print", "after_move:vkget_meta:%(upload_date)s\t%(title)s\t%(id)s",
            "--output", output,
            url,
        ]
        min_rate, slow_for = _vpn_speed_limits(proxy)
        rc, out, err = await _run(
            args,
            timeout=4 * 60 * 60,
            min_rate_bps=min_rate,
            slow_rate_duration=slow_for,
        )
        printed, _meta = parse_download_marks(out)
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        final_path = printed or (lines[-1] if lines and rc == 0 else None)
        if final_path and final_path.startswith("vkget_"):
            final_path = None
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
    folder: str | None = None,
    proxy: str | None = None,
):
    folder_name = download_folder_name(
        subscription_title=folder or "",
        channel=channel,
    )
    output_path = build_partial_output(folder=folder_name, video_id=video_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = str(output_path)
    fmt = download_format(settings.max_height)
    stale = remove_stale_download_parts(output)
    if stale:
        print(
            "vkget: removed leftover partial download before retry: "
            + ", ".join(Path(item).name for item in stale),
            flush=True,
        )

    last_rc = 1
    last_path = None
    last_log = ""

    async def _finish(rc, final_path, log):
        recoverable = rc == 0 or looks_like_already_downloaded(log)
        if not recoverable:
            return rc, final_path, log
        printed, meta = parse_download_marks(log)
        reported = printed or final_path or ""
        used = resolve_on_disk_download(output, reported)
        if not used:
            if rc == 0:
                return 1, None, f"{log}\nDownload finished without a file.".strip()
            return rc, final_path, log
        named = place_downloaded_file(
            used,
            title=(meta.get("title") or title),
            video_id=(meta.get("id") or video_id),
            upload_date=(meta.get("upload_date") or upload_date),
            folder=folder_name,
        )
        if not named or not Path(named).is_file():
            return 1, None, f"{log}\nDownload finished without a file.".strip()
        try:
            tv_path = await ensure_tv_compatible(named, force_remux=False)
        except Exception as exc:
            return 1, named, f"{log}\nTV compatible encode failed: {exc}".strip()
        if not tv_path or not Path(tv_path).is_file():
            return 1, None, f"{log}\nDownload finished without a file.".strip()
        return 0, tv_path, log

    async def _attempt(candidate: str, extra_cookies=None, user_agent=None):
        once_kwargs = {"proxy": proxy} if proxy else {}
        rc, final_path, log = await _download_once(
            candidate,
            output,
            fmt,
            extra_cookies=extra_cookies,
            user_agent=user_agent,
            **once_kwargs,
        )
        if rc == 0 or not looks_like_no_data_blocks(log):
            return rc, final_path, log
        print(
            "vkget: yt-dlp wrote no data blocks, discarding partial and retrying once",
            flush=True,
        )
        remove_stale_download_parts(output, force=True)
        return await _download_once(
            candidate,
            output,
            fmt,
            extra_cookies=extra_cookies,
            user_agent=user_agent,
            **once_kwargs,
        )

    for candidate in download_url_candidates(url):
        try:
            rc, final_path, log = await _attempt(candidate)
        except asyncio.TimeoutError:
            last_rc = 1
            last_path = None
            last_log = f"{candidate}: timed out"
            continue

        if rc == 0 or looks_like_already_downloaded(log):
            finished = await _finish(rc, final_path, log)
            if finished[0] == 0 or rc == 0:
                return finished

        last_rc, last_path, last_log = rc, final_path, log
        if not (flaresolverr_enabled() and looks_like_bot_protection(log)):
            continue

        try:
            cookies, user_agent = await fetch_flaresolverr_cookies(candidate)
            rc, final_path, log = await _attempt(
                candidate,
                extra_cookies=cookies,
                user_agent=user_agent,
            )
        except asyncio.TimeoutError:
            last_log = f"{last_log}\n{candidate}: timed out after FlareSolverr".strip()
            continue
        except Exception as flare_exc:
            last_log = f"{last_log}\nFlareSolverr {candidate}: {flare_exc}".strip()
            continue

        if rc == 0 or looks_like_already_downloaded(log):
            finished = await _finish(rc, final_path, log)
            if finished[0] == 0 or rc == 0:
                return finished
        last_rc, last_path, last_log = rc, final_path, log

    return last_rc, last_path, last_log
