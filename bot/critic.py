"""bot.critic — AI-критик постов (Quality Gate с экспертной калибровкой).

После генерации и Python-валидации (bot/post_validator.py) пост попадает сюда:
другая модель (gpt-4o-mini через ModelTier.CRITIC) оценивает по 6 критериям с
взвешенным итогом. Если overall < QUALITY_THRESHOLD — пост перегенерируется,
причём feedback критика идёт обратно в промпт генератора (in-context learning).

Экспертные решения, заложенные в этот модуль:

1. **Веса критериев** — SMM-исследования (NN/Group 2024) показали, что первые
   3 секунды решают 80% удержания. Поэтому hook+specificity вместе весят 0.45,
   а grammar и originality по 0.10.

2. **Калибровка через few-shot** — без примеров "плохой 4/10" модель ставит
   7 везде ("GPT-anchoring bias"). В промпте есть конкретные эталоны.

3. **Перевешенный overall** — мы НЕ доверяем overall, который модель сама
   подсчитала: считаем weighted-sum из per-criterion scores. Защищает от
   ситуации "общая оценка 9 при hook=4".

4. **Anti-AI-fingerprint** — критик специально ищет шаблонные обороты ИИ.

5. **Fail-safe** — сбой критика НЕ блокирует пайплайн (возвращаем neutral approve).

6. **Best-effort fallback** — если N регенераций не помогли, генератор отправляет
   лучшую из попыток с пометкой score.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bot.ai import ModelTier, call_llm
from bot.config import get_settings
from bot.logging_setup import get_logger

log = get_logger("bot.critic")

# Дефолтные значения. Реальные значения берутся из Settings (см. bot.config).
# Эти константы используются только если бот.config недоступен (тесты юнитов).
DEFAULT_QUALITY_THRESHOLD = 7
DEFAULT_HARD_FLOOR = 4

# Сколько раз повторно генерировать пост, если критик отклонил.
MAX_REGENERATIONS = 2

# Обратная совместимость: импорт `from bot.critic import QUALITY_THRESHOLD`
# (читается из настроек лениво, через property — см. _threshold ниже).
QUALITY_THRESHOLD = DEFAULT_QUALITY_THRESHOLD

# Веса критериев. См. блок-комментарий выше — основано на SMM-исследованиях.
CRITERION_WEIGHTS: dict[str, float] = {
    "hook": 0.25,
    "specificity": 0.20,
    "value": 0.20,
    "emotion": 0.15,
    "originality": 0.10,
    "grammar": 0.10,
}
assert abs(sum(CRITERION_WEIGHTS.values()) - 1.0) < 1e-9


CRITIC_SYSTEM = """Ты — главный редактор топового SMM-агентства уровня BBDO / Red.
Ты беспощадный, но справедливый. Твоя задача — отделять реально цепляющие посты
от формально валидных, но скучных.

Калибровка:
- 1-3: мусор, шаблон, ноль эмоции
- 4-6: средне, можно опубликовать, но без отклика
- 7-8: хороший пост, нормальный охват, репосты вероятны
- 9-10: вирусный потенциал, такие посты обсуждают сутками

Большинство постов = 5-7. Ставь 9-10 только за реальные шедевры.

Особое внимание — ANTI-AI-FINGERPRINT. Снижай оценку, если видишь:
- «открывает новые возможности», «в современном мире», «позволяет вам»
- «давайте разберёмся», «как мы видим», «таким образом»
- идеально-сбалансированные абзацы по 3 предложения каждый
- эпитеты-клише: «впечатляющий», «значительный», «существенный»

Отвечай ТОЛЬКО валидным JSON, без markdown."""


CRITIC_PROMPT = """Оцени пост по 6 критериям (шкала 1-10):

1. **hook** — насколько цепляет первая строка? (1=пресная, 10=невозможно листнуть)
2. **specificity** — конкретика, цифры, факты? (1=общие фразы, 10=всё точно)
3. **value** — реальный инсайт для читателя? (1=ноль пользы, 10=меняет картину мира)
4. **emotion** — вызывает реакцию? (1=плоский, 10=удивление/тревога/восторг)
5. **grammar** — опрятность? (1=косноязычие, 10=безупречно)
6. **originality** — свежесть vs клише? (1=AI-шаблон, 10=уникально)

ЭТАЛОНЫ ДЛЯ КАЛИБРОВКИ:

Пример СЛАБОГО поста (overall=4):
"В современном мире технологии открывают новые возможности. ИИ позволяет автоматизировать многие задачи. Это впечатляющий прогресс. Что вы думаете об ИИ?"
→ hook=3, specificity=2, value=3, emotion=2, grammar=8, originality=2

Пример СИЛЬНОГО поста (overall=9):
"🚨 Stripe выгнал 14% сотрудников и заменил их ChatGPT. CEO написал письмо: «Мы платим $300k разработчику, который три часа в день копирует тикеты в Jira. Это унижение для всех». Если ты разраб — закрой этот пост и подумай. Если HR — тебе тоже скоро объяснят."
→ hook=10, specificity=9, value=9, emotion=10, grammar=9, originality=9

ПОСТ ДЛЯ ОЦЕНКИ:
{post_text}

Верни JSON:
{{
  "hook": 1-10,
  "specificity": 1-10,
  "value": 1-10,
  "emotion": 1-10,
  "grammar": 1-10,
  "originality": 1-10,
  "feedback": "1-2 конкретные правки для перегенерации"
}}"""


@dataclass
class CriticResult:
    overall: int  # weighted score (после нашего пересчёта)
    scores: dict[str, int] = field(default_factory=dict)
    verdict: str = "regenerate"  # approve | regenerate (выводится из overall + hard floor)
    feedback: str = ""
    rejection_reason: str = ""  # причина отклонения (для логов/БД)

    @property
    def approved(self) -> bool:
        return self.verdict == "approve"

    @property
    def min_score(self) -> int:
        """Минимальный балл по любому критерию — для hard floor проверки."""
        return min(self.scores.values()) if self.scores else 0

    def scores_json(self) -> str:
        """Сериализация per-criterion scores для сохранения в БД."""
        return json.dumps(self.scores, ensure_ascii=False, sort_keys=True)

    def summary(self) -> str:
        if self.approved:
            return f"✅ overall={self.overall} ({self._fmt_scores()})"
        reason = self.rejection_reason or (self.feedback or "")[:120]
        return f"❌ overall={self.overall} ({self._fmt_scores()}): {reason}"

    def short_preview(self) -> str:
        """Однострочная сводка для caption-превью модератора."""
        return f"критик: {self.overall}/10 · " + " ".join(
            f"{k[:3]}={v}" for k, v in self.scores.items()
        )

    def _fmt_scores(self) -> str:
        return " ".join(f"{k[:3]}={v}" for k, v in self.scores.items())


def _extract_json(raw: str) -> dict[str, Any]:
    """JSON-extract: парсим даже если модель вернула markdown-обёртку."""
    cleaned = raw.strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    data: dict[str, Any] = json.loads(cleaned)
    return data


def _calculate_overall(scores: dict[str, int]) -> int:
    """Взвешенная оценка. Защищает от 'модель завысила overall'."""
    weighted = sum(scores.get(k, 0) * w for k, w in CRITERION_WEIGHTS.items())
    return max(1, min(10, round(weighted)))


def _resolve_thresholds() -> tuple[int, int]:
    """Возвращает (quality_threshold, hard_floor) из Settings, с дефолтами на случай ошибки."""
    try:
        s = get_settings()
        return s.critic_quality_threshold, s.critic_hard_floor
    except Exception:
        return DEFAULT_QUALITY_THRESHOLD, DEFAULT_HARD_FLOOR


def _get_critic_system() -> str:
    """Активный critic system-промпт из БД, с fallback на встроенный CRITIC_SYSTEM."""
    try:
        from bot.prompts import get_active_prompt

        return get_active_prompt("critic").system
    except Exception as exc:
        log.warning("Не удалось загрузить critic-промпт из БД, fallback: %s", exc)
        return CRITIC_SYSTEM


def _get_critic_template() -> str:
    """Активный critic user-template из БД, с fallback."""
    try:
        from bot.prompts import get_active_prompt

        return get_active_prompt("critic").user_template
    except Exception as exc:
        log.warning("Не удалось загрузить critic-промпт из БД, fallback: %s", exc)
        return CRITIC_PROMPT


def _decide_verdict(
    overall: int, scores: dict[str, int], threshold: int, hard_floor: int
) -> tuple[str, str]:
    """Выносит verdict с учётом hard floor.

    Hard floor: даже если weighted overall достаточный, любой критерий
    ниже hard_floor → reject. Защита от ситуации «hook=3, остальное=10 → overall=8».
    Это критично для SMM: пост с плохим hook не цепляет, никакая средняя оценка
    не компенсирует.

    Возвращает (verdict, rejection_reason).
    """
    if scores:
        worst_criterion, worst_score = min(scores.items(), key=lambda kv: kv[1])
        if worst_score < hard_floor:
            return (
                "regenerate",
                f"hard floor: {worst_criterion}={worst_score} (< {hard_floor})",
            )
    if overall < threshold:
        return "regenerate", f"overall {overall} < threshold {threshold}"
    return "approve", ""


def critique_post(post_text: str) -> CriticResult:
    """Оценивает пост через AI-критика.

    Двухуровневый контроль:
      1. Weighted overall ≥ quality_threshold (из Settings, по умолчанию 7).
      2. Hard floor: каждый из 6 критериев ≥ hard_floor (из Settings, по умолчанию 4).
         Защита от «hook=3, overall=8» — формально проходит, реально не работает.

    Fail-safe: при любом сбое возвращает нейтральный verdict=approve с overall=threshold,
    чтобы не блокировать публикацию из-за проблемы в самом критике.
    """
    threshold, hard_floor = _resolve_thresholds()

    messages = [
        # T2.4: критик-промпт загружается из активной версии в БД.
        # Если БД пуста — fallback на встроенные CRITIC_SYSTEM/CRITIC_PROMPT.
        {"role": "system", "content": _get_critic_system()},
        {"role": "user", "content": _get_critic_template().format(post_text=post_text)},
    ]
    try:
        llm = call_llm(
            messages,
            tier=ModelTier.CRITIC,
            temperature=0.2,  # стабильная, воспроизводимая оценка
            max_tokens=400,
            json_mode=True,
        )
        data = _extract_json(llm.content)
        scores: dict[str, int] = {}
        for k in ("hook", "specificity", "value", "emotion", "grammar", "originality"):
            if k not in data:
                scores[k] = 5  # модель пропустила поле — нейтральное значение
                continue
            try:
                scores[k] = max(1, min(10, int(data[k])))
            except (ValueError, TypeError):
                scores[k] = 5  # значение нечисловое — нейтральное

        overall = _calculate_overall(scores)
        verdict, rejection_reason = _decide_verdict(overall, scores, threshold, hard_floor)
        feedback = str(data.get("feedback", "") or "")
        return CriticResult(
            overall=overall,
            scores=scores,
            verdict=verdict,
            feedback=feedback,
            rejection_reason=rejection_reason,
        )
    except Exception as exc:
        # Сбой критика не должен блокировать публикацию.
        log.warning("AI-критик сбойнул (пропускаю пост): %s", exc)
        return CriticResult(
            overall=threshold,
            scores=dict.fromkeys(CRITERION_WEIGHTS, threshold),
            verdict="approve",
            feedback="critic unavailable",
        )
