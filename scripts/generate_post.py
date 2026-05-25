"""generate_post.py — генератор постов (версия с БД).

Особенности:
- Универсальная архитектура: одна кодовая база для любого канала
- Конфигурация через pydantic-settings (валидация на старте, см. bot/config.py)
- Хранилище: SQLite через SQLAlchemy (см. bot/storage.py)
- Умный фильтр контента (GPT-4o оценивает HIGH/MEDIUM/LOW)
- Эталонные примеры вирусных постов в промпте (few-shot learning)
- Жёсткие правила SMM
- Гарантированные рабочие гиперссылки

Запуск: python scripts/generate_post.py
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import feedparser
import requests

# Добавляем корень проекта в sys.path, чтобы `from bot...` работало
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.ai import ModelTier, call_llm  # noqa: E402
from bot.config import get_settings  # noqa: E402
from bot.db import init_db, session_scope  # noqa: E402
from bot.http import (  # noqa: E402
    CircuitOpenError,
    DeadlineExceededError,
    http_get,
    http_post,
    set_deadline,
)
from bot.logging_setup import get_logger, setup_logging  # noqa: E402
from bot.storage import (  # noqa: E402
    article_hash,
    create_pending_post,
    ensure_channel,
    known_article_hashes,
    save_article,
)
from bot.utils import best_telegram_file_id  # noqa: E402

# ─── Константы ───────────────────────────────────────────────────────────────
# Сколько последних статей брать из каждого RSS-фида
ENTRIES_PER_FEED = 5
# Сколько кандидатов прогонять через фильтр качества за один запуск
MAX_CANDIDATES_TO_FILTER = 10
# Общий таймбюджет на весь процесс генерации
PROCESS_DEADLINE_SEC = 300  # 5 минут


# ─── Конфигурация ────────────────────────────────────────────────────────────
settings = get_settings()
log = get_logger("generate_post")


# ─── Утилиты HTTP ────────────────────────────────────────────────────────────


def notify_moderator(text: str) -> None:
    """Отправка алерта модератору. Сетевые ошибки не пробрасываем."""
    try:
        http_post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_moderator_id, "text": text},
            timeout=10,
        )
    except (requests.RequestException, CircuitOpenError, DeadlineExceededError) as exc:
        log.warning("notify_moderator failed: %s", exc)


# ─── Загрузка статей из RSS ──────────────────────────────────────────────────


def fetch_articles() -> list[dict[str, Any]]:
    """Скачиваем каждый RSS-фид через http_get (retry + UA + timeout),
    парсим через feedparser из байтов. Ошибки одного фида не валят остальные."""
    articles: list[dict[str, Any]] = []
    for feed_url in settings.rss_feeds:
        try:
            resp = http_get(feed_url, timeout=15)
            if resp.status_code != 200:
                log.warning("RSS %s → HTTP %d", feed_url, resp.status_code)
                continue
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:ENTRIES_PER_FEED]:
                url = entry.get("link", "")
                if not url:
                    continue
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))
                articles.append(
                    {
                        "id": article_hash(url),
                        "title": entry.get("title", "").strip(),
                        "url": url,
                        "summary": summary[:800].strip(),
                        "source_feed": feed_url,
                    }
                )
        except (requests.RequestException, CircuitOpenError) as exc:
            log.warning("RSS недоступен %s: %s", feed_url, exc)
        except DeadlineExceededError:
            log.warning("Deadline во время загрузки RSS — прерываю фетч")
            break
        except Exception as exc:  # парсер feedparser может бросить что угодно
            log.warning("RSS-ошибка %s: %s", feed_url, exc)
    log.info("Всего статей из RSS: %d", len(articles))
    return articles


# ─── Промпты — теперь через bot.prompts (T2.4 — Promptops) ─────────────────
#
# Все промпты хранятся в БД (таблица prompts) с версиями. Источник правды —
# .md-файлы в каталоге prompts/, синхронизируются через
# `python scripts/prompts_admin.py seed` или auto-bootstrap при первом запуске.
#
# Few-shot эталоны (таблица few_shot_examples, источник prompts/few_shot/*.md)
# выбираются случайно с приоритетом по рубрике и quality_score, и подмешиваются
# в шаблон GENERATOR через токен {few_shot_examples}.
#
# Для отката версии: scripts/prompts_admin.py rollback <kind>.


# ─── JSON-extract из ответа модели ───────────────────────────────────────────


def _extract_json(raw: str) -> dict[str, Any]:
    """Достаёт JSON из ответа модели — даже если он в markdown-блоке."""
    cleaned = raw.strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    data: dict[str, Any] = json.loads(cleaned)
    return data


# ─── Фильтр качества ─────────────────────────────────────────────────────────


def filter_article(article: dict[str, Any]) -> tuple[str, str]:
    """Оценивает статью HIGH/MEDIUM/LOW через дешёвую модель.

    Промпт берётся из активной версии filter (T2.4 — Promptops). Переменные
    {channel_topic}, {channel_niche}, {channel_audience} подставляются здесь —
    они не входят в `article` dict, поэтому format(**article) не сработал бы.
    """
    from bot.prompts import get_active_prompt

    prompt = get_active_prompt("filter")
    system_text = prompt.system.format(
        channel_topic=settings.channel_topic,
        channel_niche=settings.channel_niche,
        channel_audience=settings.channel_audience,
    )
    user_text = prompt.user_template.format(**article)
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    try:
        result = call_llm(
            messages,
            tier=ModelTier.CHEAP,  # фильтр — дешёвая классификация → gpt-4o-mini
            temperature=0.3,
            max_tokens=200,
            json_mode=True,
        )
        raw = result.content
        data = _extract_json(raw)
        return data.get("quality", "LOW"), data.get("reason", "")
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        log.warning("Ошибка фильтра, считаем MEDIUM: %s", exc)
        return "MEDIUM", "ошибка парсинга"


# ─── Определение рубрики (AI-классификатор + балансировка) ──────────────────
#
# T2.5 — заменили эвристику по ключевым словам на AI-классификатор (W4 из аудита).
# Используется bot.classifier.classify_article, который:
#   1. Спрашивает gpt-4o-mini (через ModelTier.CHEAP) — какая рубрика?
#   2. При confidence < 0.5 — fallback на heuristic_detect → DEFAULT_RUBRIC
#   3. apply_anti_repeat — если 2+ раз подряд была эта рубрика, переключает.


def detect_rubric_for_article(
    article: dict[str, Any], recent_slugs: list[str]
) -> tuple[str, str, str]:
    """Возвращает (rubric_name, structure_hint, cta_style).

    rubric_name — человекочитаемое имя с emoji (для записи в Article.rubric и логов).
    structure_hint и cta_style — подмешиваются в промпт генератора.
    """
    from bot.classifier import classify_article

    result = classify_article(
        title=article["title"],
        summary=article["summary"],
        recent_slugs=recent_slugs,
    )
    log.info(
        "Рубрика: %s (источник=%s, confidence=%.2f) — %s",
        result.rubric.name,
        result.source,
        result.confidence,
        result.reason,
    )
    return result.rubric.name, result.rubric.structure_hint, result.rubric.cta_style


# ─── Генерация поста ─────────────────────────────────────────────────────────


def parse_post(raw: str) -> tuple[str, str]:
    data = _extract_json(raw)
    post_text = data.get("post", "").strip()
    image_prompt = data.get("image_prompt", "").strip()

    if not post_text:
        raise ValueError("Пустой пост")

    if settings.channel_lang.lower() == "русский":
        ru = sum(1 for c in post_text if "Ѐ" <= c <= "ӿ")
        if ru < 30:
            raise ValueError(f"Пост не на русском (ru символов: {ru})")

    if not image_prompt:
        image_prompt = "AI neural network dark space neon glow cinematic 8k"

    return post_text, image_prompt


def ensure_correct_link(post_text: str, article_url: str) -> str:
    """Гарантирует наличие правильной гиперссылки в посте."""
    correct = f'<a href="{article_url}">📖 Читать полностью</a>'
    if correct in post_text:
        return post_text
    post_text = re.sub(r"<a\s+href=[^>]*>.*?</a>", "", post_text, flags=re.IGNORECASE | re.DOTALL)
    post_text = re.sub(r"📖\s*Читать\s*полностью", "", post_text, flags=re.IGNORECASE)
    return post_text.rstrip() + f"\n\n{correct}"


# Прогрессия температур: первая попытка — стандартный творческий регистр (0.85),
# при регенерациях критика повышаем для большего разнообразия (близко к потолку 4o).
TEMPERATURE_LADDER = (0.85, 0.95, 1.05)

# Максимум попыток для каждой стадии (раздельные счётчики — у них разная природа сбоя).
MAX_VALIDATION_ATTEMPTS = 3


@dataclass
class GeneratedPost:
    """Результат генерации поста — всё, что должно попасть в БД и в Telegram."""

    post_text: str
    image_prompt: str
    model_used: str
    quality_score: int | None
    critic_scores_json: str | None
    critic_feedback: str | None
    critic_preview: str | None
    prompt_version_id: int | None  # T2.4: ссылка на использованную версию промпта
    few_shot_slugs_json: str | None  # T2.4: JSON-список slug'ов эталонов
    fact_check_json: str | None  # T2.6: результат fact-check (Python + AI)


def generate_post_content(
    article: dict[str, Any],
    rubric: str,
    *,
    structure_hint: str = "",
    cta_style: str = "",
) -> GeneratedPost:
    """Полный pipeline генерации с двухступенчатым quality gate.

    Шаги:
      1. LLM-генерация (gpt-4o, temperature из TEMPERATURE_LADDER) → JSON.
      2. Python-валидатор: длина/цифры/ссылка/banned words.
         Сбой = модель сломала формат → повтор с тем же prompt.
      3. AI-критик: 6 критериев + hard floor.
         Сбой = пост скучный → повтор с feedback в промпте + ↑ temperature.

    Раздельные счётчики validation_attempts / critic_attempts — у них разная природа
    сбоев и разные стратегии восстановления.

    Best-effort fallback: если критик ни разу не одобрил, но есть валидный пост —
    отдаём лучшую попытку. Никогда не теряем статью.
    """
    from bot.critic import MAX_REGENERATIONS, CriticResult, critique_post
    from bot.post_validator import validate_post
    from bot.prompts import (
        format_few_shot_for_prompt,
        get_active_prompt,
        sample_few_shot,
    )

    # T2.4: грузим активный промпт + рандомные few-shot эталоны
    gen_prompt = get_active_prompt("generator")
    # rubric пришёл как name с emoji ("🚀 Запуск/Релиз") — для матчинга в БД
    # нужен slug. Маппим обратно через bot.rubrics.by_name.
    from bot.rubrics import by_name

    rubric_obj = by_name(rubric)
    rubric_slug_for_sampling = rubric_obj.slug if rubric_obj else "any"
    examples = sample_few_shot(
        rubric=rubric_slug_for_sampling, count=3, language=settings.channel_lang
    )
    few_shot_block = format_few_shot_for_prompt(examples)
    few_shot_slugs = [e.slug for e in examples]
    few_shot_slugs_json = json.dumps(few_shot_slugs, ensure_ascii=False)
    log.info(
        "Промпт generator=%s, few-shot=%s",
        gen_prompt.version,
        few_shot_slugs or "пусто",
    )

    system_text = gen_prompt.system.format(
        channel_topic=settings.channel_topic,
        channel_niche=settings.channel_niche,
        channel_audience=settings.channel_audience,
        channel_lang=settings.channel_lang,
    )
    base_prompt = gen_prompt.user_template.format(
        title=article["title"],
        summary=article["summary"],
        url=article["url"],
        rubric=rubric,
        lang=settings.channel_lang,
        few_shot_examples=few_shot_block,
    )

    # T2.5: рубрика-специфичные подсказки подмешиваются в конец user-промпта,
    # после общих требований. Это даёт модели тонкую настройку стиля под тип контента.
    if structure_hint or cta_style:
        base_prompt += (
            f"\n\n═══════════════════════════════════════════════\n"
            f"СТИЛЬ ПОД РУБРИКУ «{rubric}»\n"
            f"═══════════════════════════════════════════════\n"
        )
        if structure_hint:
            base_prompt += f"Структура: {structure_hint}\n"
        if cta_style:
            base_prompt += f"CTA: {cta_style}\n"

    last_err: Exception | None = None
    last_validation: str | None = None
    critic_feedback_for_prompt: str = ""
    best_candidate: GeneratedPost | None = None

    validation_attempts = 0
    critic_attempts = 0

    while validation_attempts < MAX_VALIDATION_ATTEMPTS and critic_attempts <= MAX_REGENERATIONS:
        # Прогрессивная температура: больше регенераций → выше temperature
        temperature = TEMPERATURE_LADDER[min(critic_attempts, len(TEMPERATURE_LADDER) - 1)]
        attempt_label = f"v{validation_attempts + 1}/c{critic_attempts + 1}@T={temperature:.2f}"

        try:
            user_msg = base_prompt
            if critic_feedback_for_prompt:
                user_msg += (
                    f"\n\n⚠ Предыдущая попытка получила низкую оценку критика. "
                    f"Учти feedback и сделай радикально иначе:\n"
                    f"{critic_feedback_for_prompt}"
                )
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_msg},
            ]
            llm = call_llm(
                messages,
                tier=ModelTier.SMART,
                temperature=temperature,
                max_tokens=1500,
                json_mode=True,
            )
            raw, model_used = llm.content, llm.model_used
            post_text, image_prompt = parse_post(raw)
            post_text = ensure_correct_link(post_text, article["url"])

            # 1) Python-валидация
            v_result = validate_post(
                post_text,
                article_url=article["url"],
                language=settings.channel_lang,
            )
            log.info("Validation [%s]: %s", attempt_label, v_result.summary())
            if not v_result.ok:
                validation_attempts += 1
                last_validation = "; ".join(v_result.errors)
                log.warning("  ❌ валидация: %s", last_validation)
                time.sleep(2)
                continue
            if v_result.warnings:
                log.info("  ⚠ warnings: %s", "; ".join(v_result.warnings))

            # 2) AI-критик (Quality Gate с hard floor)
            c_result: CriticResult = critique_post(post_text)
            log.info("Critic [%s]: %s", attempt_label, c_result.summary())

            # 3) Fact-check (T2.6) — защита от галлюцинаций
            # Делаем ПОСЛЕ критика, но ДО approve. Если есть CRITICAL (выдуманная
            # цифра/дата) — отклоняем независимо от критика.
            from bot.fact_check import fact_check_post

            fc_result = fact_check_post(
                post_text, article.get("summary", ""), use_ai=c_result.approved
            )
            log.info("FactCheck [%s]: %s", attempt_label, fc_result.summary())

            candidate = GeneratedPost(
                post_text=post_text,
                image_prompt=image_prompt,
                model_used=model_used,
                quality_score=c_result.overall,
                critic_scores_json=c_result.scores_json(),
                critic_feedback=c_result.feedback or c_result.rejection_reason or None,
                critic_preview=c_result.short_preview(),
                prompt_version_id=gen_prompt.id,
                few_shot_slugs_json=few_shot_slugs_json,
                fact_check_json=fc_result.to_json(),
            )

            # Запоминаем лучший вариант для fallback
            if best_candidate is None or (c_result.overall or 0) > (
                best_candidate.quality_score or 0
            ):
                best_candidate = candidate

            # Одобрение: и критик довольный, и fact-check без CRITICAL проблем
            if c_result.approved and fc_result.passed:
                return candidate

            critic_attempts += 1
            # Feedback для следующей попытки: либо от критика, либо от fact-check
            if not fc_result.passed:
                critic_issues = [i for i in fc_result.issues if i.severity.value == "critical"]
                critic_feedback_for_prompt = (
                    "fact-check нашёл выдуманные данные: "
                    + "; ".join(f"«{i.fragment}»" for i in critic_issues[:3])
                    + ". Используй ТОЛЬКО факты из исходной статьи."
                )
                log.warning(
                    "  ❌ fact-check отклонил (CRITICAL): %d issues — попытка %d/%d",
                    len(critic_issues),
                    critic_attempts,
                    MAX_REGENERATIONS + 1,
                )
            else:
                critic_feedback_for_prompt = (
                    c_result.feedback or "повысь хук, добавь конкретики и эмоцию"
                )
                log.warning(
                    "  ❌ критик отклонил: %s — критик-попытка %d/%d",
                    c_result.rejection_reason or critic_feedback_for_prompt,
                    critic_attempts,
                    MAX_REGENERATIONS + 1,
                )
            time.sleep(2)
        except (json.JSONDecodeError, ValueError) as exc:
            validation_attempts += 1
            log.warning("Parse-error [%s]: %s", attempt_label, exc)
            last_err = exc
            time.sleep(3)

    # Best-effort fallback: критик ни разу не одобрил, но валидный пост есть.
    if best_candidate is not None:
        log.warning(
            "Все попытки не прошли критика. Отдаём лучшую (score=%s) с пометкой модератору.",
            best_candidate.quality_score,
        )
        return best_candidate

    err_msg = last_validation or str(last_err) or "unknown"
    raise RuntimeError(f"Не удалось сгенерировать корректный пост: {err_msg}")


# ─── Картинки ────────────────────────────────────────────────────────────────


def build_image_url(prompt: str) -> str:
    seed = random.randint(10000, 99999)
    full = f"{prompt}, NO humans, NO faces, NO text, NO letters, abstract only, professional"
    encoded = urllib.parse.quote(full)
    return (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1080&height=1080&model=flux&nologo=true&enhance=true&seed={seed}"
    )


# ─── Telegram: отправка на одобрение ─────────────────────────────────────────


def send_for_approval(
    post_text: str,
    image_url: str,
    art_hash_str: str,
    *,
    critic_preview: str | None = None,
    below_threshold: bool = False,
) -> tuple[int, str | None]:
    """Отправляет пост модератору. Возвращает (message_id, file_id|None).

    file_id важен: при публикации мы используем его, а не image_url,
    чтобы публикация не зависела от доступности Pollinations (см. T1.5/C5).

    critic_preview — короткая строка от AI-критика для отображения модератору,
    чтобы он принимал решение с учётом машинной оценки.
    below_threshold — если True, помечаем пост ⚠ (best-effort fallback).
    """
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Опубликовать", "callback_data": f"approve_{art_hash_str}"},
                {"text": "❌ Отклонить", "callback_data": f"reject_{art_hash_str}"},
            ]
        ]
    }

    preview_body = re.sub(r"<[^>]+>", "", post_text)
    header_mark = "⚠" if below_threshold else "📬"
    header = f"{header_mark} Новый пост [{settings.channel_topic}]"
    if critic_preview:
        header += f"\n{critic_preview}"
    caption = f"{header}:\n\n{preview_body}"

    result = http_post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto",
        json={
            "chat_id": settings.telegram_moderator_id,
            "photo": image_url,
            "caption": caption[:1024],
            "reply_markup": keyboard,
        },
        timeout=25,
    ).json()

    file_id: str | None = None
    if result.get("ok"):
        file_id = best_telegram_file_id(result)
    else:
        log.warning("Фото не загрузилось (%s), отправляю текстом", result.get("description"))
        result = http_post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_moderator_id,
                "text": caption[:4096],
                "reply_markup": keyboard,
            },
            timeout=10,
        ).json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram ошибка: {result}")

    return result["result"]["message_id"], file_id


# ─── ГЛАВНАЯ ФУНКЦИЯ ─────────────────────────────────────────────────────────


def main() -> None:
    setup_logging()
    set_deadline(PROCESS_DEADLINE_SEC)
    log.info(
        "=== Канал «%s» — %s ===", settings.channel_topic, datetime.now().strftime("%Y-%m-%d %H:%M")
    )

    init_db()

    try:
        articles = fetch_articles()

        # Выясняем что уже видели — одна транзакция чисто на чтение
        with session_scope() as session:
            channel = ensure_channel(session)
            channel_id = channel.id
            known = known_article_hashes(session, channel_id)

        new_articles = [a for a in articles if a["id"] not in known]
        log.info("Новых статей в RSS: %d", len(new_articles))

        if not new_articles:
            log.info("Нет новых статей.")
            return

        log.info(
            "Фильтрация качества top-%d кандидатов:",
            min(MAX_CANDIDATES_TO_FILTER, len(new_articles)),
        )
        best_article: dict[str, Any] | None = None
        first_medium: dict[str, Any] | None = None

        for i, article in enumerate(new_articles[:MAX_CANDIDATES_TO_FILTER]):
            log.info("[%d] %s", i + 1, article["title"][:70])
            quality, reason = filter_article(article)
            log.info("  → %s: %s", quality, reason)

            # Сохраняем статью в БД (даже LOW — чтобы не оценивать повторно)
            with session_scope() as session:
                save_article(
                    session,
                    channel_id=channel_id,
                    url=article["url"],
                    title=article["title"],
                    summary=article["summary"],
                    source_feed=article.get("source_feed"),
                    quality=quality,
                    quality_reason=reason,
                )

            if quality == "HIGH":
                best_article = article
                log.info("✅ ВЫБРАНА КАК HIGH")
                break
            if quality == "MEDIUM" and first_medium is None:
                first_medium = article

        if best_article is None:
            best_article = first_medium

        if best_article is None:
            log.info("Не нашли подходящих статей в этом цикле.")
            return

        log.info("📝 Генерируем пост для: %s", best_article["title"])

        # T2.5: получаем недавнюю историю рубрик канала для anti-repeat балансировщика
        from bot.storage import recent_post_rubrics

        with session_scope() as session:
            recent_slugs = recent_post_rubrics(session, channel_id, limit=5)
        log.info("Недавняя история рубрик: %s", recent_slugs or "пусто")

        rubric, structure_hint, cta_style = detect_rubric_for_article(best_article, recent_slugs)

        result = generate_post_content(
            best_article, rubric, structure_hint=structure_hint, cta_style=cta_style
        )
        log.info(
            "Пост готов: %d символов, модель=%s, quality=%s",
            len(result.post_text),
            result.model_used,
            result.quality_score if result.quality_score is not None else "n/a",
        )
        log.info("Image: %s", result.image_prompt[:80])

        image_url = build_image_url(result.image_prompt)

        # Если quality_score ниже порога — отправляем пост с пометкой для модератора
        below_threshold = (
            result.quality_score is not None
            and result.quality_score < settings.critic_quality_threshold
        )
        if below_threshold:
            log.warning(
                "⚠ low quality_score=%d (best-effort fallback) — модератору с пометкой",
                result.quality_score,
            )

        msg_id, image_file_id = send_for_approval(
            result.post_text,
            image_url,
            best_article["id"],
            critic_preview=result.critic_preview,
            below_threshold=below_threshold,
        )
        log.info(
            "✅ Отправлено модератору (msg_id=%d, file_id=%s)",
            msg_id,
            (image_file_id[:16] + "…") if image_file_id else "none",
        )

        # Финальная транзакция: сохраняем рубрику и создаём pending-Post
        with session_scope() as session:
            article_obj = save_article(
                session,
                channel_id=channel_id,
                url=best_article["url"],
                title=best_article["title"],
                summary=best_article["summary"],
                source_feed=best_article.get("source_feed"),
                rubric=rubric,
            )
            create_pending_post(
                session,
                article=article_obj,
                channel_id=channel_id,
                post_text=result.post_text,
                image_url=image_url,
                image_prompt=result.image_prompt,
                image_file_id=image_file_id,
                moderator_msg_id=msg_id,
                model_used=result.model_used,
                quality_score=result.quality_score,
                critic_scores_json=result.critic_scores_json,
                critic_feedback=result.critic_feedback,
                prompt_version_id=result.prompt_version_id,
                few_shot_slugs_json=result.few_shot_slugs_json,
                fact_check_json=result.fact_check_json,
            )

        log.info("✅ ГОТОВО")

    except Exception as exc:
        # log.exception сам прицепит traceback и сработает Telegram-алерт + Sentry
        log.exception("Сбой пайплайна [%s]: %s", settings.channel_topic, type(exc).__name__)
        notify_moderator(f"❌ Сбой [{settings.channel_topic}]: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
