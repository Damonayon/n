"""bot.models — ORM-схема БД (SQLAlchemy 2.0, типизированный стиль).

Назначение таблиц:
- channels       : справочник каналов сети (для масштабирования)
- articles       : все увиденные RSS-статьи + результат фильтрации качества
- posts          : посты в очереди модерации / опубликованные / отклонённые
- metrics        : аналитика опубликованных постов (заполняется в Стадии 3)
- prompts        : версионирование промптов (заполняется в Стадии 2)
- logs           : структурированные события (опционально, локальные логи)
- system_state   : ключ-значение хранилище (Telegram offset и т.п.)
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Текущее время в UTC, с timezone-меткой."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""


# ─── channels ────────────────────────────────────────────────────────────────


class Channel(Base):
    """Канал сети. Сейчас обычно один; в будущем — N."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    topic: Mapped[str] = mapped_column(String(255))
    niche: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(32), default="русский")
    telegram_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    articles: Mapped[list[Article]] = relationship(back_populates="channel")
    posts: Mapped[list[Post]] = relationship(back_populates="channel")


# ─── articles ────────────────────────────────────────────────────────────────


class Article(Base):
    """Статья, увиденная ботом из RSS. Хранится независимо от того, опубликована или нет."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    article_hash: Mapped[str] = mapped_column(
        String(32),
        index=True,
        comment="MD5(url)[:16] — стабильный идентификатор статьи",
    )
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_feed: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="HIGH / MEDIUM / LOW / null=не оценено"
    )
    quality_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric: Mapped[str | None] = mapped_column(String(128), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    channel: Mapped[Channel] = relationship(back_populates="articles")
    posts: Mapped[list[Post]] = relationship(back_populates="article")

    __table_args__ = (
        UniqueConstraint("channel_id", "article_hash", name="uq_articles_channel_hash"),
        Index("ix_articles_channel_discovered", "channel_id", "discovered_at"),
    )


# ─── posts ───────────────────────────────────────────────────────────────────


# Допустимые статусы поста (используем строки, не Enum — проще миграции)
POST_STATUS_PENDING = "pending"
POST_STATUS_PUBLISHED = "published"
POST_STATUS_REJECTED = "rejected"
POST_STATUS_FAILED = "failed"


class Post(Base):
    """Сгенерированный пост. Живёт в БД от момента создания до публикации/отклонения."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    post_text: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Telegram file_id картинки. Сохраняется при отправке модератору. При публикации
    # используется он вместо image_url — гарантирует, что картинка не "испарится"
    # если внешний хостинг (Pollinations) лежит. См. T1.5.
    image_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    moderator_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=POST_STATUS_PENDING, index=True)
    prompt_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompts.id", ondelete="SET NULL"), nullable=True
    )
    model_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_score: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Weighted overall AI-критика [1-10]. См. bot.critic.",
    )
    # Полная разбивка по 6 критериям — JSON {hook, specificity, value, emotion, grammar, originality}.
    # Нужно для аналитики A/B-тестов промптов (T2.11) и retro-анализа стиля канала.
    critic_scores_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Feedback критика. Сохраняется даже при approve — помогает находить системные
    # слабости («регулярно низкий hook» → нужна правка GENERATOR_PROMPT).
    critic_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    article: Mapped[Article] = relationship(back_populates="posts")
    channel: Mapped[Channel] = relationship(back_populates="posts")
    prompt_version: Mapped[Prompt | None] = relationship()

    __table_args__ = (
        Index("ix_posts_channel_status", "channel_id", "status"),
        Index("ix_posts_status_created", "status", "created_at"),
    )


# ─── metrics ─────────────────────────────────────────────────────────────────


class Metric(Base):
    """Снимок метрик опубликованного поста. Заполняется задачей аналитики (T3.5)."""

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reactions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ─── prompts ─────────────────────────────────────────────────────────────────


class Prompt(Base):
    """Версионированный промпт. Заполняется в T2.5."""

    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(
        String(32), index=True, comment="filter / generator / critic / rubric"
    )
    version: Mapped[str] = mapped_column(String(32))
    system_prompt: Mapped[str] = mapped_column(Text)
    user_template: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("kind", "version", name="uq_prompts_kind_version"),)


# ─── logs ────────────────────────────────────────────────────────────────────


class LogEntry(Base):
    """Локальный структурированный лог. Sentry будет основным каналом (T1.3),
    но локальный лог полезен для офлайн-диагностики."""

    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# ─── system_state ────────────────────────────────────────────────────────────


class SystemState(Base):
    """Ключ-значение для технического состояния (Telegram offset и т.п.)."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
