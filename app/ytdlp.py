from __future__ import annotations

import asyncio
import json
import os
import re

import shutil
import tempfile

from pathlib import Path

from .config import settings

def normalize_vk_url(url: str) -> str:
    url = url.strip()
    if url.startswith("https://vkvideo.ru/"):
        return "https://vk.com/" + url.removeprefix("https://vkvideo.ru/")
    if url.startswith("http://vkvideo.ru/"):
        return "https://vk.com/" + url.removeprefix("http://vkvideo.ru/")
    return url

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

def common_args() -> list[str]:
    args = ["/usr/local/bin/yt-dlp"]
    if os.path.exists(settings.cookie_file):

        runtime_cookie = os.path.join(
            tempfile.gettempdir(),
            "vkget-cookies.txt",
        )
        shutil.copy2(settings.cookie_file, runtime_cookie)
        args += ["--cookies", runtime_cookie]
    return args

async def inspect_url(url: str) -> dict:
    url = normalize_vk_url(url)
    args = common_args() + [
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        url,
    ]
    rc, out, err = await _run(args, timeout=120)
    if rc != 0:
        raise RuntimeError(err.strip() or out.strip() or f"yt-dlp exited {rc}")
    return json.loads(out)

async def inspect_playlist_flat(url: str) -> dict:
    url = normalize_vk_url(url)
    args = common_args() + [
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        url,
    ]
    rc, out, err = await _run(args, timeout=180)
    if rc != 0:
        raise RuntimeError(err.strip() or out.strip() or f"yt-dlp exited {rc}")
    return json.loads(out)

def safe_component(value: str) -> str:
    value = (value or "Unknown").strip()
    value = re.sub(r'[\/\\:*?"<>|]', "_", value)
    value = re.sub(r"\s+", " ", value)
    return value[:180].strip(" .") or "Unknown"

async def download_video(url: str, channel: str):
    url = normalize_vk_url(url)
    folder = Path(settings.download_root) / safe_component(channel)
    folder.mkdir(parents=True, exist_ok=True)

    fmt = (
        f"bestvideo[height<={settings.max_height}]+bestaudio/"
        f"best[height<={settings.max_height}]/best"
    )

    output = str(
        folder / "%(upload_date>%Y-%m-%d,Unknown)s - %(title)s [%(id)s].%(ext)s"
    )

    args = common_args() + [
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
