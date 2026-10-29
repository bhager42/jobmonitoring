"""SQLAlchemy ORM models. See docs/DEVELOPMENT_PLAN.md §5."""

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores timezone-aware datetimes as naive UTC; returns them as aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime(),
        dict[str, Any]: JSON(),
        list[str]: JSON(),
    }


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex of the token
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip: Mapped[str | None] = mapped_column(String(45))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index(None, "ip", "attempted_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(45))
    attempted_at: Mapped[datetime] = mapped_column(default=utcnow)
    success: Mapped[bool] = mapped_column(Boolean)


class Monitor(Base):
    __tablename__ = "monitors"
    __table_args__ = (
        CheckConstraint("render_mode IN ('static', 'browser', 'auto')", name="render_mode"),
        CheckConstraint("interval_hours BETWEEN 1 AND 720", name="interval_hours"),
        Index(None, "enabled", "next_check_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(2048))
    adapter: Mapped[str | None] = mapped_column(String(50))
    adapter_config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    render_mode: Mapped[str] = mapped_column(String(10), default="auto")
    criteria: Mapped[dict[str, Any]] = mapped_column(default=dict)
    interval_hours: Mapped[int] = mapped_column(Integer, default=12)
    enabled: Mapped[bool] = mapped_column(default=True)
    respect_robots: Mapped[bool] = mapped_column(default=True)
    notify: Mapped[bool] = mapped_column(default=True)
    last_check_at: Mapped[datetime | None]
    next_check_at: Mapped[datetime | None]
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    baseline_done: Mapped[bool] = mapped_column(default=False)
    # Raise alerts for matching postings found by the first (baseline) check too.
    alert_existing: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Posting(Base):
    __tablename__ = "postings"
    __table_args__ = (
        UniqueConstraint("monitor_id", "fingerprint"),
        Index(None, "monitor_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(2048))
    location: Mapped[str | None] = mapped_column(String(500))
    department: Mapped[str | None] = mapped_column(String(200))
    employment_type: Mapped[str | None] = mapped_column(String(100))
    remote: Mapped[bool | None]
    posted_at: Mapped[datetime | None]
    description_text: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    missing_count: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (Index(None, "seen", "matched_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    posting_id: Mapped[int] = mapped_column(
        ForeignKey("postings.id", ondelete="CASCADE"), unique=True
    )
    reasons: Mapped[list[str]] = mapped_column(default=list)
    matched_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen: Mapped[bool] = mapped_column(default=False)
    dismissed: Mapped[bool] = mapped_column(default=False)
    notified_at: Mapped[datetime | None]


class CheckRun(Base):
    __tablename__ = "check_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'error', 'blocked', 'skipped')", name="status"
        ),
        Index(None, "monitor_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(10), default="running")
    adapter_used: Mapped[str | None] = mapped_column(String(50))
    postings_found: Mapped[int] = mapped_column(default=0)
    new_postings: Mapped[int] = mapped_column(default=0)
    new_matches: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None]


class NotificationTarget(Base):
    __tablename__ = "notification_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    apprise_url_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
