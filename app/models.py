from __future__ import annotations
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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

class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
