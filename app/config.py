import os
from dataclasses import dataclass
from urllib.parse import quote_plus


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


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
    flaresolverr_url: str = os.getenv(
        "FLARESOLVERR_URL",
        "http://flaresolverr.flaresolverr.svc.cluster.local:8191",
    ).strip()
    vpn_enabled: bool = env_bool("VPN_ENABLED", False)
    vpn_fallback_to_direct: bool = env_bool("VPN_FALLBACK_TO_DIRECT", True)
    vpn_gateway_url: str = os.getenv(
        "VPN_GATEWAY_URL",
        "http://vkget-vpn-gateway.vkget.svc.cluster.local:8081",
    ).strip()
    vpn_proxy_url: str = os.getenv(
        "VPN_PROXY_URL",
        "socks5://vkget-vpn-gateway.vkget.svc.cluster.local:1080",
    ).strip()
    vpn_connect_timeout: int = env_int("VPN_CONNECT_TIMEOUT", 15)
    vpn_verify_timeout: int = env_int("VPN_VERIFY_TIMEOUT", 10)
    vpn_min_download_rate: str = os.getenv("VPN_MIN_DOWNLOAD_RATE", "300K")
    vpn_slow_rate_duration: int = env_int("VPN_SLOW_RATE_DURATION", 120)


settings = Settings()
