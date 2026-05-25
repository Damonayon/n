"""bot.logging_setup — единая настройка логирования.

Логи идут параллельно в три места:
1. stdout (читаемо, для GitHub Actions logs)
2. Таблица `logs` в БД (для офлайн-диагностики)
3. Sentry (если задан SENTRY_DSN) — для агрегации и алертов
4. Telegram-алерт модератору на ERROR и CRITICAL (с антиспам-троттлингом)

Использование:
    from bot.logging_setup import setup_logging, get_logger

    setup_logging()                # один раз в начале скрипта
    log = get_logger(__name__)
    log.info("Старт")
    log.error("Что-то сломалось", exc_info=True)

Поведение полностью контролируется конфигом (см. bot/config.py):
- SENTRY_DSN пустой → Sentry просто не включается
- БД недоступна → лог в БД пропускается, остальные продолжают работать
- Telegram упал → тот же принцип
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

from bot.config import get_settings

_initialized: bool = False

# ─── Антиспам для Telegram-алертов ───────────────────────────────────────────
# Один и тот же event-ключ слать не чаще, чем раз в TELEGRAM_ALERT_COOLDOWN_SEC.
TELEGRAM_ALERT_COOLDOWN_SEC = 600  # 10 минут
_last_alert_at: dict[str, float] = {}


# ─── Форматтер ───────────────────────────────────────────────────────────────


class _CleanFormatter(logging.Formatter):
    """Краткий формат для stdout — без лишнего шума."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        prefix = f"{ts} {record.levelname:7s} {record.name}"
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{prefix} | {msg}"


# ─── Handler: запись в таблицу logs ──────────────────────────────────────────


class _DBLogHandler(logging.Handler):
    """Сохраняет лог в таблицу `logs`. Молча игнорирует ошибки БД,
    чтобы сбой логирования не сломал основной поток."""

    def emit(self, record: logging.LogRecord) -> None:
        # Логи ниже WARNING в БД не пишем — иначе таблица распухнет
        if record.levelno < logging.WARNING:
            return
        try:
            from bot.db import session_scope  # отложенный импорт — избегаем цикла
            from bot.models import LogEntry

            payload: dict[str, Any] = {
                k: v
                for k, v in record.__dict__.items()
                if k
                in (
                    "module",
                    "funcName",
                    "lineno",
                    "process",
                    "thread",
                )
            }
            if record.exc_info:
                payload["exception"] = logging.Formatter().formatException(record.exc_info)[:4000]

            entry = LogEntry(
                level=record.levelname,
                event=record.name,
                message=record.getMessage()[:4000],
                payload_json=json.dumps(payload, ensure_ascii=False, default=str),
            )
            with session_scope() as session:
                session.add(entry)
        except Exception:
            # Логирование не должно падать. Никаких re-raise.
            pass


# ─── Handler: Telegram-алерт на ERROR/CRITICAL ───────────────────────────────


class _TelegramAlertHandler(logging.Handler):
    """Шлёт алерт модератору на ERROR/CRITICAL с троттлингом 10 минут на ключ."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        try:
            settings = get_settings()
            # Ключ троттлинга: имя логгера + сообщение (грубо нормализованное)
            key = f"{record.name}:{record.getMessage()[:80]}"
            now = time.time()
            last = _last_alert_at.get(key, 0.0)
            if now - last < TELEGRAM_ALERT_COOLDOWN_SEC:
                return
            _last_alert_at[key] = now

            import requests  # отложенный импорт

            text_lines = [
                f"❌ [{settings.channel_topic}] {record.levelname}",
                f"{record.name}: {record.getMessage()[:500]}",
            ]
            if record.exc_info:
                exc_txt = logging.Formatter().formatException(record.exc_info)
                text_lines.append(f"\n<pre>{exc_txt[:1500]}</pre>")
            text = "\n".join(text_lines)[:4000]

            # Используем "сырой" requests, а не http-обёртку: если упадёт
            # bot.http, логирование не должно ввязываться в его retry-цикл
            # (иначе ERROR от http_post снова дёрнет TelegramAlertHandler).
            requests.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={
                    "chat_id": settings.telegram_moderator_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=5,
            )
        except Exception:
            pass


# ─── Sentry (опционально) ────────────────────────────────────────────────────


def _init_sentry() -> bool:
    """Включает Sentry, если задан SENTRY_DSN. Возвращает True, если включено."""
    settings = get_settings()
    if not settings.sentry_dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_logging = LoggingIntegration(
            level=logging.INFO,  # фиксируем как breadcrumbs
            event_level=logging.ERROR,  # шлём как events
        )
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            release=_detect_release(),
            send_default_pii=False,
            integrations=[sentry_logging],
            traces_sample_rate=0.0,  # пока без performance-tracing
            attach_stacktrace=True,
        )
        sentry_sdk.set_tag("channel.slug", settings.channel_slug)
        sentry_sdk.set_tag("channel.topic", settings.channel_topic)
        return True
    except ImportError:
        return False
    except Exception:
        return False


def _detect_release() -> str | None:
    """Определяет версию релиза: пробуем переменные GitHub Actions, иначе None."""
    import os

    return os.environ.get("GITHUB_SHA") or os.environ.get("GIT_COMMIT") or None


# ─── Главная точка входа ─────────────────────────────────────────────────────


def setup_logging() -> None:
    """Настраивает логирование. Идемпотентна — повторные вызовы безопасны."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # Чистим возможные дефолтные хэндлеры
    for h in list(root.handlers):
        root.removeHandler(h)

    # 1) stdout
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_CleanFormatter())
    root.addHandler(stream)

    # 2) БД
    root.addHandler(_DBLogHandler())

    # 3) Telegram-алерт
    root.addHandler(_TelegramAlertHandler())

    # 4) Sentry (если есть DSN)
    sentry_on = _init_sentry()

    # Понижаем шум сторонних библиотек
    for noisy in ("urllib3", "requests", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger("bot.logging_setup")
    log.info(
        "logging initialized: stdout + db + telegram-alert + sentry=%s, level=%s",
        sentry_on,
        settings.log_level,
    )


def get_logger(name: str) -> logging.Logger:
    """Удобная обёртка вокруг logging.getLogger."""
    return logging.getLogger(name)
