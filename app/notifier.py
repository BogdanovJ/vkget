import re

import httpx
from .config import settings
from .ytdlp import composed_title, is_usable_channel, to_vkvideo

_FALLBACK_FAILURE = "The download failed and will be tried again."
_EXCEPTION_PREFIX = re.compile(
    r"^(?:OSError|PermissionError|FileExistsError|FileNotFoundError|"
    r"TimeoutError|RuntimeError|BlockingIOError|ConnectionError):\s*",
    re.I,
)


def friendly_failure(text: BaseException | str | None) -> str:
    """One plain sentence for Telegram. Never a class name or errno."""
    if isinstance(text, BaseException):
        raw = f"{type(text).__name__}: {text}".strip()
    else:
        raw = str(text or "").strip()
    lowered = raw.lower()
    if not raw:
        return _FALLBACK_FAILURE
    if (
        "file exists" in lowered
        or "already been downloaded" in lowered
        or "already downloaded" in lowered
        or "errno 17" in lowered
    ):
        return "This video is already in the download folder."
    if any(
        hint in lowered
        for hint in (
            "no space left",
            "errno 28",
            "disk quota",
            "errno 122",
            "edquot",
        )
    ):
        return "The download disk is full."
    if any(
        hint in lowered
        for hint in (
            "permission denied",
            "errno 13",
            "errno 1]",
            "read-only file system",
            "read only file system",
            "errno 30",
        )
    ):
        return "The download folder is not writable."
    if any(
        hint in lowered
        for hint in (
            "device or resource busy",
            "errno 16",
            "text file busy",
            "errno 26",
        )
    ):
        return "The file is open elsewhere and could not be replaced."
    if "did not get any data blocks" in lowered or "no data blocks" in lowered:
        return "VK sent no video data."
    if "vpn_slow_rate" in lowered or "below minimum rate" in lowered:
        return "The VPN download was too slow."
    if any(hint in lowered for hint in ("ffmpeg", "ffprobe", "tv compatible")):
        return "The file could not be prepared for the TV."
    if any(
        hint in lowered
        for hint in (
            "video unavailable",
            "private video",
            "video is private",
            "has been removed",
            "has been deleted",
            "no longer available",
        )
    ):
        return "This video is unavailable."
    if "timed out" in lowered or "timeout" in lowered:
        return "The download timed out."

    line = raw.splitlines()[0].strip()
    line = re.sub(r"^(?:error:\s*)+", "", line, flags=re.I).strip()
    previous = None
    while previous != line:
        previous = line
        line = _EXCEPTION_PREFIX.sub("", line).strip()
    line = re.sub(r"\[Errno\s+-?\d+\]\s*", "", line).strip()
    line = line.strip(" .")
    if not line or _technical_failure_line(line):
        return _FALLBACK_FAILURE
    if len(line) > 160:
        line = line[:157].rstrip(" .") + "..."
    if not line.endswith("."):
        line += "."
    return line


def _technical_failure_line(line: str) -> bool:
    lowered = line.casefold()
    if "oserror" in lowered or "errno" in lowered or "traceback" in lowered:
        return True
    return bool(re.fullmatch(r"[A-Za-z]+Error", line))


def format_video_notice(
    *,
    ok: bool,
    title: str,
    channel: str = "",
    height: int | None = None,
    path: str = "",
    page_url: str = "",
    detail: str = "",
    retry_at=None,
    external_id: str = "",
) -> str:
    shown_title = composed_title(title, channel=channel, external_id=external_id)
    shown_channel = channel if is_usable_channel(channel) else ""
    page = to_vkvideo(page_url) if page_url else ""
    lines = ["✅ VKGET" if ok else "⚠ VKGET", shown_title]
    if ok:
        if shown_channel and height is not None:
            lines.append(f"{shown_channel} · ≤{height}p")
        elif height is not None:
            lines.append(f"Downloaded ≤{height}p")
        elif shown_channel:
            lines.append(shown_channel)
        if page:
            lines.append(page)
        if path:
            lines.append(path)
    else:
        if shown_channel:
            lines.append(shown_channel)
        if detail:
            lines.append(detail)
        if page:
            lines.append(page)
        if retry_at is not None:
            lines.append(f"Next attempt: {retry_at:%Y-%m-%d %H:%M}")
    return "\n".join(line for line in lines if line)


async def notify(text: str):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": text,
                },
            )
    except Exception:
        # Notifications must never break the scheduler.
        pass
