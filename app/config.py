import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://vkget:vkget@127.0.0.1:3306/vkget",
    )
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

settings = Settings()
