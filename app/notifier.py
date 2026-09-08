import httpx
from .config import settings
from .ytdlp import is_usable_channel, is_usable_video_title, to_vkvideo


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
    shown_title = (title or "").strip()
    if not is_usable_video_title(shown_title, external_id):
        shown_title = "Untitled"
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
