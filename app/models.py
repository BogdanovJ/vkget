from __future__ import annotations
from datetime import datetime
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

def now():
    return datetime.now()

class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500), default="Subscription")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    initial_last_n: Mapped[int] = mapped_column(Integer, default=3)
    watch_future: Mapped[bool] = mapped_column(Boolean, default=True)
    min_duration_seconds: Mapped[int] = mapped_column(Integer, default=600)
    extra_stop_words: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_is_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scan_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    videos: Mapped[list["Video"]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
    )

    def display_title(self) -> str:
        from .ytdlp import PLACEHOLDER_TITLES, label_from_url

        title = (self.title or "").strip()
        if title not in PLACEHOLDER_TITLES:
            return title
        return label_from_url(self.source_url)

    def has_custom_title(self) -> bool:
        from .ytdlp import PLACEHOLDER_TITLES, label_from_url

        if self.title_is_custom:
            return True
        title = (self.title or "").strip()
        if title in PLACEHOLDER_TITLES:
            return False
        return title != label_from_url(self.source_url)

class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_video_source_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(50), default="vk")
    external_id: Mapped[str] = mapped_column(String(300))
    webpage_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(1000), default="Untitled")
    channel: Mapped[str] = mapped_column(String(500), default="Unknown")
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="NEW")
    ignore_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    subscription: Mapped[Subscription | None] = relationship(back_populates="videos")

    def display_title(self) -> str:
        from .ytdlp import is_usable_video_title

        title = (self.title or "").strip()
        if is_usable_video_title(title, self.external_id):
            return title
        return "Untitled"

class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class VpnEndpoint(Base):
    __tablename__ = "vpn_endpoints"
    __table_args__ = (
        UniqueConstraint("ip_address", name="uq_vpn_endpoint_ip"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), default="")
    ip_address: Mapped[str] = mapped_column(String(45))
    country: Mapped[str] = mapped_column(String(8), default="RU")
    provider: Mapped[str] = mapped_column(String(100), default="vpngate")
    source: Mapped[str] = mapped_column(String(50), default="vpngate")
    sources: Mapped[str] = mapped_column(String(200), default="vpngate")
    openvpn_udp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    openvpn_tcp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    openvpn_udp_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    openvpn_tcp_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    udp_config_is_ip: Mapped[bool] = mapped_column(Boolean, default=False)
    tcp_config_is_ip: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    successful_connections: Mapped[int] = mapped_column(Integer, default=0)
    failed_connections: Mapped[int] = mapped_column(Integer, default=0)
    reported_speed_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_ping_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_uptime_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    measured_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    measured_download_speed_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    def display_source(self) -> str:
        labels = {
            "vpngate": "VPN Gate",
            "vpnobratno": "VPN Obratno",
            "manual": "Manual",
        }
        return labels.get(self.source, self.source or "UNKNOWN")

    def display_protocol(self) -> str:
        parts: list[str] = []
        if self.openvpn_udp_config:
            parts.append("IP UDP" if self.udp_config_is_ip else "UDP")
        if self.openvpn_tcp_config:
            parts.append("IP TCP" if self.tcp_config_is_ip else "TCP")
        return " / ".join(parts) or "NONE"

    def display_status(self, current: datetime | None = None) -> str:
        when = current or now()
        if not self.is_active:
            return "DISABLED"
        if self.cooldown_until and self.cooldown_until > when:
            return "COOLDOWN"
        if self.is_stale:
            return "STALE"
        if not self.is_available:
            return "DOWN"
        return "READY"

    def has_usable_config(self) -> bool:
        return bool(self.openvpn_udp_config or self.openvpn_tcp_config)
