import os
from dataclasses import dataclass
from urllib.parse import quote_plus


def build_database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url

    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "3306")
    name = os.getenv("DB_NAME", "vkget")
    user = os.getenv("DB_USER", "vkget")
    password = quote_plus(os.getenv("DB_PASSWORD", "vkget"))

    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{name}"


@dataclass(frozen=True)
class Settings:
    database_url: str = build_database_url()
    download_root: str = os.getenv("DOWNLOAD_ROOT", "/downloads")
    cookie_file: str = os.getenv("COOKIE_FILE", "/config/cookies.txt")
    max_height: int = min(int(os.getenv("MAX_HEIGHT", "720")), 720)
    download_rate: str = os.getenv("DOWNLOAD_RATE", "500K")
    min_gap_minutes: int = int(os.getenv("MIN_GAP_MINUTES", "15"))
    max_gap_minutes: int = int(os.getenv("MIN_GAP_MAX_MINUTES", "30"))
    discovery_min_hours: int = int(os.getenv("DISCOVERY_INTERVAL_MIN_HOURS", "3"))
    discovery_max_hours: int = int(os.getenv("DISCOVERY_INTERVAL_MAX_HOURS", "6"))
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    flaresolverr_url: str = os.getenv("FLARESOLVERR_URL", "").strip()
    flaresolverr_url: str = os.getenv(
        "FLARESOLVERR_URL",
        "http://flaresolverr.flaresolverr.svc.cluster.local:8191",
    )


settings = Settings()
