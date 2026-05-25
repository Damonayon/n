"""bot.fact_check — защита от галлюцинаций (T2.6).

Two-stage защита:
  1. **Python-проверка** (быстрая, бесплатная): цифры и имена в посте
     должны быть подкреплены в source (summary статьи).
  2. **AI fact-check** (опциональная, через ModelTier.CRITIC): LLM ищет
     утверждения в посте, которых нет в source.

Severity:
  - CRITICAL: цифра в посте, которой нет в source → возможна выдумка
  - WARNING:  имя/название в посте, не упомянутое в source
  - INFO:     косметические расхождения (порядок слов и т.п.)

Никогда не блокирует pipeline: при сбое AI fact-check возвращается «нет проблем».
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bot.ai import ModelTier, call_llm
from bot.logging_setup import get_logger

log = get_logger("bot.fact_check")


# ─── Severity ────────────────────────────────────────────────────────────────


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class FactIssue:
    """Одно расхождение между постом и источником."""

    severity: Severity
    kind: str  # "number_not_in_source" | "name_not_in_source" | "ai_unsourced_claim"
    fragment: str  # цитата из поста
    note: str = ""


@dataclass
class FactCheckResult:
    issues: list[FactIssue] = field(default_factory=list)
    ai_checked: bool = False
    ai_summary: str = ""

    @property
    def has_critical(self) -> bool:
        return any(i.severity == Severity.CRITICAL for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == Severity.WARNING for i in self.issues)

    @property
    def passed(self) -> bool:
        """Pipeline пропускает пост, если нет CRITICAL проблем."""
        return not self.has_critical

    def to_json(self) -> str:
        return json.dumps(
            {
                "issues": [
                    {
                        "severity": i.severity.value,
                        "kind": i.kind,
                        "fragment": i.fragment,
                        "note": i.note,
                    }
                    for i in self.issues
                ],
                "ai_checked": self.ai_checked,
                "ai_summary": self.ai_summary,
            },
            ensure_ascii=False,
        )

    def summary(self) -> str:
        if not self.issues:
            return "✅ no issues"
        by_sev: dict[str, int] = {}
        for i in self.issues:
            by_sev[i.severity.value] = by_sev.get(i.severity.value, 0) + 1
        parts = " ".join(f"{k}={v}" for k, v in by_sev.items())
        mark = "❌" if self.has_critical else "⚠"
        return f"{mark} {parts}"


# ─── Whitelist общеизвестных сущностей (не считаются галлюцинацией) ─────────
# Эти бренды/имена часто появляются в посте из контекста канала, даже если их
# нет в summary конкретной статьи. Добавляются вручную по результатам логов.

WHITELISTED_ENTITIES: frozenset[str] = frozenset(
    {
        # AI-компании (наш профиль)
        "OpenAI",
        "Anthropic",
        "Google",
        "DeepMind",
        "Meta",
        "Microsoft",
        "NVIDIA",
        "Mistral",
        "Stability",
        "Hugging Face",
        "GPT",
        "Claude",
        "Gemini",
        "Llama",
        "ChatGPT",
        "Copilot",
        # Telegram-каналы, медиа
        "Bloomberg",
        "Reuters",
        "TechCrunch",
        "Verge",
        "FT",
        "WSJ",
        "Forbes",
        # Общие термины
        "AI",
        "ML",
        "ИИ",
        "GPU",
        "API",
        "LLM",
    }
)


# ─── Регулярки ───────────────────────────────────────────────────────────────


# Цифры: целые, дробные, проценты, валюта.
# Сознательно не ловим единичные "1" в "1 минута" — слишком много шума.
# Хорошо ловит: 50000, 73%, $12 млн, 14%, 4500.
_NUMBER_RE = re.compile(r"\$?\d[\d\s.,]*(?:%|млн|млрд|million|billion|k|тыс|тысяч)?")

# Простой NER: Capitalized word(s) длиннее 2 букв, до 3 слов подряд.
# Для русского — кириллические заглавные.
_NAME_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z]{2,}|[А-ЯЁ][а-яё]{2,})(?:\s+(?:[A-Z][a-zA-Z]{2,}|[А-ЯЁ][а-яё]{2,})){0,2}\b"
)

# HTML-разметка для очистки
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text)


def _normalize(text: str) -> str:
    """Нормализация для устойчивого сравнения."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _extract_numeric_atoms(s: str) -> set[str]:
    """Извлекает «атомы» из чисел: для "50 000" → {"50000", "50 000"}.
    Для "73%" → {"73", "73%"}. Это даёт устойчивое сравнение «пост → source»."""
    atoms: set[str] = set()
    for m in _NUMBER_RE.finditer(s):
        raw = m.group(0).strip()
        atoms.add(raw.lower())
        # Очистка от пробелов и валют — добавляем как ещё один атом
        digits_only = re.sub(r"[^\d.,]", "", raw)
        if digits_only:
            atoms.add(digits_only)
            # И без разделителей
            atoms.add(digits_only.replace(",", "").replace(".", ""))
    return atoms


# ─── Python-уровень проверки ─────────────────────────────────────────────────


def find_unsourced_numbers(post_text: str, source_text: str) -> list[FactIssue]:
    """Возвращает цифры/проценты/валютные суммы, которые есть в посте,
    но не находятся в source. CRITICAL."""
    post_clean = _strip_html(post_text)
    src_norm = _normalize(source_text)
    src_atoms = _extract_numeric_atoms(src_norm)

    issues: list[FactIssue] = []
    seen: set[str] = set()

    for m in _NUMBER_RE.finditer(post_clean):
        raw = m.group(0).strip()
        norm = raw.lower()
        if norm in seen:
            continue
        seen.add(norm)
        # Пробуем все варианты атомов
        candidate_atoms = _extract_numeric_atoms(norm)
        if any(a in src_atoms for a in candidate_atoms):
            continue
        # Также: достаточно, что просто строка содержится в источнике
        if any(a in src_norm for a in candidate_atoms):
            continue
        # Игнорируем «1», «2» и пр. — слишком короткие, дают много шума
        digits = re.sub(r"[^\d]", "", raw)
        if len(digits) <= 1:
            continue
        issues.append(
            FactIssue(
                severity=Severity.CRITICAL,
                kind="number_not_in_source",
                fragment=raw,
                note="число в посте не найдено в исходной статье",
            )
        )
    return issues


def find_unsourced_names(post_text: str, source_text: str) -> list[FactIssue]:
    """Возвращает имена/названия в посте, отсутствующие в source.
    WARNING (не CRITICAL — это менее опасно)."""
    post_clean = _strip_html(post_text)
    src_norm = _normalize(source_text)

    issues: list[FactIssue] = []
    seen: set[str] = set()

    for m in _NAME_RE.finditer(post_clean):
        name = m.group(0).strip()
        if name in seen:
            continue
        seen.add(name)
        # Whitelist
        if name in WHITELISTED_ENTITIES or any(w in name for w in WHITELISTED_ENTITIES):
            continue
        # Короткие имена пропускаем (слишком много false-positive)
        if len(name) < 4:
            continue
        # Если в source встречается — ок
        if name.lower() in src_norm:
            continue
        issues.append(
            FactIssue(
                severity=Severity.WARNING,
                kind="name_not_in_source",
                fragment=name,
                note="имя/название в посте не найдено в исходной статье",
            )
        )
    # Ограничиваем количество шума: первые 5
    return issues[:5]


def python_fact_check(post_text: str, source_text: str) -> list[FactIssue]:
    """Полная Python-проверка (быстро, бесплатно)."""
    issues: list[FactIssue] = []
    issues.extend(find_unsourced_numbers(post_text, source_text))
    issues.extend(find_unsourced_names(post_text, source_text))
    return issues


# ─── AI-уровень проверки ─────────────────────────────────────────────────────


AI_FACT_CHECK_SYSTEM = (
    "Ты — fact-checker SMM-агентства. Тебе дают сгенерированный Telegram-пост "
    "и оригинальную статью. Твоя задача — найти утверждения в посте, "
    "которых нет в статье. Отвечаешь ТОЛЬКО валидным JSON."
)

AI_FACT_CHECK_USER = """Проверь, есть ли в посте выдуманные факты, цифры или имена,
которых нет в источнике. Игнорируй общие фразы и эмоции — только конкретные
проверяемые утверждения.

ОРИГИНАЛЬНАЯ СТАТЬЯ:
{source}

ПОСТ ДЛЯ ПРОВЕРКИ:
{post}

Верни JSON:
{{
  "ok": true|false,
  "issues": [
    {{"severity": "critical|warning", "fragment": "цитата", "note": "что не так"}}
  ],
  "summary": "1 предложение"
}}

Если ok=true — issues должен быть пустой массив."""


def _extract_json(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if "```" in cleaned:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1)
    return json.loads(cleaned)  # type: ignore[no-any-return]


def ai_fact_check(post_text: str, source_text: str) -> tuple[list[FactIssue], str]:
    """AI fact-check (опционально). Возвращает (issues, summary).
    Никогда не бросает: при сбое — пустой список."""
    if not source_text or not source_text.strip():
        return [], "no source"

    messages = [
        {"role": "system", "content": AI_FACT_CHECK_SYSTEM},
        {
            "role": "user",
            "content": AI_FACT_CHECK_USER.format(
                source=source_text[:2000], post=_strip_html(post_text)
            ),
        },
    ]
    try:
        llm = call_llm(
            messages,
            tier=ModelTier.CRITIC,
            temperature=0.1,  # детерминированно
            max_tokens=400,
            json_mode=True,
        )
        data = _extract_json(llm.content)
        ai_summary = str(data.get("summary", ""))[:300]
        raw_issues = data.get("issues", []) or []

        result: list[FactIssue] = []
        for item in raw_issues:
            sev_str = str(item.get("severity", "warning")).lower()
            try:
                sev = Severity(sev_str)
            except ValueError:
                sev = Severity.WARNING
            result.append(
                FactIssue(
                    severity=sev,
                    kind="ai_unsourced_claim",
                    fragment=str(item.get("fragment", ""))[:200],
                    note=str(item.get("note", ""))[:300],
                )
            )
        return result, ai_summary
    except Exception as exc:
        log.warning("AI fact-check сбойнул: %s", exc)
        return [], f"ai error: {type(exc).__name__}"


# ─── Главная функция ─────────────────────────────────────────────────────────


def fact_check_post(
    post_text: str,
    source_text: str,
    *,
    use_ai: bool = True,
) -> FactCheckResult:
    """Полный fact-check: Python + опционально AI.

    Returns FactCheckResult, который НИКОГДА не блокирует pipeline сам по себе.
    Решение о перегенерации принимает caller через .passed / .has_critical.
    """
    py_issues = python_fact_check(post_text, source_text)

    ai_issues: list[FactIssue] = []
    ai_summary = ""
    if use_ai:
        ai_issues, ai_summary = ai_fact_check(post_text, source_text)

    return FactCheckResult(
        issues=py_issues + ai_issues,
        ai_checked=use_ai,
        ai_summary=ai_summary,
    )
