"""bot.ai — единая точка вызова LLM с каскадом моделей по стоимости.

Концепция ModelTier:
- CHEAP   — для классификации, фильтров, простых задач (gpt-4o-mini)
- SMART   — для творческой генерации (gpt-4o)
- CRITIC  — для оценочных задач (gpt-4o-mini)

Каскад: для каждого tier есть primary и fallback модели. Если primary недоступна
(404, исчерпаны ретраи) — переключаемся на fallback. Если все упали — RuntimeError.

Цель — оптимизировать использование квот GPT-4o (≈150 запросов/день free),
переложив дешёвые задачи на gpt-4o-mini (≈300+ запросов/день).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import requests

from bot.config import get_settings
from bot.http import (
    CircuitOpenError,
    DeadlineExceededError,
    RetryableHttpStatus,
    http_post,
)
from bot.logging_setup import get_logger

log = get_logger("bot.ai")

GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"


# ─── ModelTier — что для какой задачи ────────────────────────────────────────


class ModelTier(StrEnum):
    """Уровни моделей по стоимости квоты GPT-4o.

    Используется как параметр в call_llm() — выбирает подходящий каскад.
    """

    CHEAP = "cheap"  # классификация, фильтры
    SMART = "smart"  # творческая генерация
    CRITIC = "critic"  # оценка качества


# Каскады: первая модель — primary, остальные — fallback в порядке предпочтения.
# Меняя только эту карту, можно перенастроить распределение нагрузки между моделями.
MODEL_CASCADES: dict[ModelTier, list[str]] = {
    ModelTier.CHEAP: ["gpt-4o-mini", "gpt-4o"],
    ModelTier.SMART: ["gpt-4o", "gpt-4o-mini"],
    ModelTier.CRITIC: ["gpt-4o-mini", "gpt-4o"],
}


@dataclass
class LLMResponse:
    """Результат вызова LLM."""

    content: str
    model_used: str
    tier: ModelTier


# ─── Главная функция ─────────────────────────────────────────────────────────


def call_llm(
    messages: list[dict[str, Any]],
    *,
    tier: ModelTier = ModelTier.SMART,
    temperature: float = 0.7,
    max_tokens: int = 1500,
    json_mode: bool = False,
) -> LLMResponse:
    """Вызывает LLM согласно tier-каскаду.

    http_post сам ретраит 429/5xx с exponential backoff. Здесь мы лишь
    переключаемся на следующую модель из cascade, если первая «закончилась».

    Возвращает LLMResponse(content, model_used, tier).
    Бросает RuntimeError, если ни одна модель не отвечает.
    """
    settings = get_settings()
    cascade = MODEL_CASCADES[tier]

    last_err: Exception | None = None
    for model in cascade:
        try:
            resp = _call_one(
                model, messages, temperature, max_tokens, json_mode, settings.gh_models_token
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                return LLMResponse(content=content, model_used=model, tier=tier)
            # 400/404: модель недоступна → пробуем следующую (без retry)
            log.warning(
                "[%s] %s недоступна (HTTP %d): %s",
                tier.value,
                model,
                resp.status_code,
                resp.text[:150],
            )
        except RetryableHttpStatus as exc:
            log.warning("[%s] %s: исчерпаны ретраи (%s)", tier.value, model, exc)
            last_err = exc
        except (CircuitOpenError, DeadlineExceededError):
            # Это уже не наше дело — пробрасываем наверх
            raise
        except requests.RequestException as exc:
            log.warning("[%s] %s: сетевая ошибка после ретраев: %s", tier.value, model, exc)
            last_err = exc
    raise RuntimeError(f"[{tier.value}] все модели каскада недоступны: {last_err}")


def _call_one(
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    token: str,
) -> requests.Response:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return http_post(
        GITHUB_MODELS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
