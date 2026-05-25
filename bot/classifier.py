"""bot.classifier — AI-классификация рубрики статьи (T2.5).

Заменяет наивный detect_rubric() по ключевым словам (W4 из аудита).
Используется ModelTier.CHEAP (gpt-4o-mini) — это классификация, не творчество.

Стратегия:
  1. AI-классификатор → (slug, confidence)
  2. Если confidence < LOW_CONFIDENCE_THRESHOLD → fallback на heuristic_detect
  3. Если и эвристика молчит → DEFAULT_RUBRIC
  4. apply_anti_repeat — балансировщик «не повторять подряд»

Никогда не бросает: при любом сбое возвращает DEFAULT_RUBRIC.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bot.ai import ModelTier, call_llm
from bot.logging_setup import get_logger
from bot.rubrics import (
    DEFAULT_RUBRIC,
    LOW_CONFIDENCE_THRESHOLD,
    RUBRICS,
    Rubric,
    apply_anti_repeat,
    by_slug,
    heuristic_detect,
)

log = get_logger("bot.classifier")


@dataclass
class ClassificationResult:
    rubric: Rubric
    confidence: float
    source: str  # "ai" | "heuristic" | "default" | "anti_repeat"
    reason: str = ""


def _build_classifier_prompt() -> tuple[str, str]:
    """Собирает (system, user_template) для классификатора из каталога рубрик."""
    rubrics_block = "\n".join(f"  {r.slug}: {r.name} — {r.description}" for r in RUBRICS.values())
    system = (
        "Ты — классификатор контент-рубрик для SMM-канала. Определяешь, "
        "к какому типу контента относится новость. Отвечаешь ТОЛЬКО валидным JSON."
    )
    user = (
        "Определи рубрику для статьи. Доступные рубрики:\n"
        f"{rubrics_block}\n\n"
        "СТАТЬЯ:\n"
        "Заголовок: {title}\n"
        "Содержание: {summary}\n\n"
        "Верни JSON:\n"
        '{{"rubric": "slug_из_списка_выше", "confidence": 0.0-1.0, '
        '"reason": "1 предложение"}}\n\n'
        "Confidence 0.9-1.0 — точное попадание; 0.7-0.9 — хорошо подходит; "
        "0.5-0.7 — спорно; <0.5 — не уверен."
    )
    return system, user


_SYSTEM_PROMPT, _USER_TEMPLATE = _build_classifier_prompt()


def _extract_json(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    return json.loads(cleaned)  # type: ignore[no-any-return]


def classify_with_ai(title: str, summary: str) -> ClassificationResult:
    """Запрос к LLM. Возвращает результат, никогда не бросает."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(title=title, summary=summary[:600]),
        },
    ]
    try:
        llm = call_llm(
            messages,
            tier=ModelTier.CHEAP,
            temperature=0.2,  # детерминированно
            max_tokens=120,
            json_mode=True,
        )
        data = _extract_json(llm.content)
        raw_slug = str(data.get("rubric", "")).strip()
        confidence = float(data.get("confidence", 0.0))
        reason = str(data.get("reason", ""))[:200]

        if raw_slug not in RUBRICS:
            log.warning("AI вернул неизвестный slug %r — fallback", raw_slug)
            return ClassificationResult(
                rubric=DEFAULT_RUBRIC,
                confidence=0.0,
                source="default",
                reason=f"unknown slug: {raw_slug}",
            )

        return ClassificationResult(
            rubric=by_slug(raw_slug),
            confidence=max(0.0, min(1.0, confidence)),
            source="ai",
            reason=reason,
        )
    except Exception as exc:
        log.warning("AI-классификатор сбойнул: %s", exc)
        return ClassificationResult(
            rubric=DEFAULT_RUBRIC,
            confidence=0.0,
            source="default",
            reason=f"ai error: {type(exc).__name__}",
        )


def classify_article(
    title: str,
    summary: str,
    *,
    recent_slugs: list[str] | None = None,
) -> ClassificationResult:
    """Высокоуровневая функция: AI → heuristic → default + anti-repeat.

    recent_slugs — список slug'ов последних N постов канала (для балансировки).
    """
    ai_result = classify_with_ai(title, summary)

    # Если AI уверен — используем его
    if ai_result.confidence >= LOW_CONFIDENCE_THRESHOLD:
        chosen_slug = ai_result.rubric.slug
        source = "ai"
        reason = ai_result.reason
    else:
        # Низкая уверенность — пробуем эвристику
        heuristic_slug = heuristic_detect(f"{title} {summary}")
        if heuristic_slug is not None:
            chosen_slug = heuristic_slug
            source = "heuristic"
            reason = (
                f"AI confidence {ai_result.confidence:.2f} < {LOW_CONFIDENCE_THRESHOLD}, "
                f"эвристика → {heuristic_slug}"
            )
        else:
            chosen_slug = DEFAULT_RUBRIC.slug
            source = "default"
            reason = (
                f"AI confidence {ai_result.confidence:.2f} < {LOW_CONFIDENCE_THRESHOLD}, "
                f"эвристика молчит → DEFAULT"
            )

    # Балансировка: anti-repeat
    if recent_slugs:
        # fallback на default, чтобы избежать монотонности
        original = chosen_slug
        chosen_slug = apply_anti_repeat(
            primary_slug=chosen_slug,
            fallback_slug=DEFAULT_RUBRIC.slug,
            recent_slugs=recent_slugs,
        )
        if chosen_slug != original:
            source = "anti_repeat"
            reason = f"{original} повторялась 2+ раз подряд → переключил на {chosen_slug}"

    return ClassificationResult(
        rubric=by_slug(chosen_slug),
        confidence=ai_result.confidence,
        source=source,
        reason=reason,
    )
