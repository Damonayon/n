"""bot.prompts — runtime-загрузка версионированных промптов из БД.

T2.4 — Promptops. Каждый промпт хранится с (kind, version), активна одна
версия на kind. Few-shot примеры выбираются случайно из активного пула,
с приоритетом по quality_score и совпадению рубрики.

Использование:
    from bot.prompts import get_active_prompt, sample_few_shot

    prompt = get_active_prompt("generator")
    # prompt.system, prompt.user_template, prompt.id, prompt.version

    examples = sample_few_shot(rubric="🚀 Запуск/Релиз", count=3, language="русский")
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from bot.config import PROJECT_ROOT
from bot.db import session_scope
from bot.logging_setup import get_logger
from bot.models import FewShotExample, Prompt

log = get_logger("bot.prompts")

PROMPTS_DIR = PROJECT_ROOT / "prompts"
FEW_SHOT_DIR = PROMPTS_DIR / "few_shot"


# ─── DTO ─────────────────────────────────────────────────────────────────────


@dataclass
class PromptVersion:
    """Снимок версии промпта, который безопасно использовать вне session."""

    id: int
    kind: str
    version: str
    system: str
    user_template: str
    notes: str | None


@dataclass
class FewShotItem:
    """Эталонный пост для few-shot контекста."""

    id: int
    slug: str
    rubric: str
    body: str
    quality_score: int


# ─── Загрузка активного промпта ──────────────────────────────────────────────


def get_active_prompt(kind: str) -> PromptVersion:
    """Возвращает активную версию промпта данного kind.

    Если в БД нет активной — пытается прочитать .md-файл из prompts/ и засеять
    автоматически (zero-config bootstrap). Это позволяет генератору работать
    сразу после первого деплоя без ручного `prompts_admin.py seed`.
    """
    with session_scope() as session:
        row = session.execute(
            select(Prompt)
            .where(Prompt.kind == kind, Prompt.is_active.is_(True))
            .order_by(Prompt.created_at.desc())
        ).scalar_one_or_none()
        if row is not None:
            return PromptVersion(
                id=row.id,
                kind=row.kind,
                version=row.version,
                system=row.system_prompt,
                user_template=row.user_template,
                notes=row.notes,
            )

    # Auto-bootstrap: ищем prompts/{kind}_v*.md, грузим в БД, возвращаем.
    log.info("Промпт %s не найден в БД — пробую auto-bootstrap из prompts/", kind)
    bootstrapped = _bootstrap_from_files(kind)
    if bootstrapped is not None:
        return bootstrapped

    raise RuntimeError(
        f"Активный промпт kind={kind!r} не найден в БД и не удалось найти "
        f"prompts/{kind}_v*.md. Запусти `python scripts/prompts_admin.py seed`."
    )


def _bootstrap_from_files(kind: str) -> PromptVersion | None:
    """Ищет prompts/{kind}_v*.md и грузит в БД как активную версию."""
    candidates = sorted(PROMPTS_DIR.glob(f"{kind}_v*.md"))
    if not candidates:
        return None
    # Берём последнюю по имени = последнюю версию
    f = candidates[-1]
    parsed = parse_prompt_file(f)
    upsert_prompt(parsed)
    return get_active_prompt(kind)


# ─── Парсинг .md-файлов ──────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_prompt_file(path: Path) -> dict[str, Any]:
    """Читает .md-файл с YAML-frontmatter и разбивает body на '# system' / '# user'.

    Возвращает dict: {kind, version, notes, system, user_template}.
    """
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"Frontmatter не найден в {path.name}")

    meta_block, body = match.group(1), match.group(2)
    meta = _parse_simple_yaml(meta_block)

    # Разбиваем body на секции "# system" и "# user"
    system_text = ""
    user_text = ""
    current: list[str] = []
    target = None
    for line in body.splitlines():
        if line.strip().startswith("# system"):
            target = "system"
            current = []
            continue
        if line.strip().startswith("# user"):
            if target == "system":
                system_text = "\n".join(current).strip()
            target = "user"
            current = []
            continue
        if target is not None:
            current.append(line)
    if target == "user":
        user_text = "\n".join(current).strip()
    elif target == "system":
        system_text = "\n".join(current).strip()

    return {
        "kind": meta["kind"],
        "version": meta["version"],
        "notes": meta.get("notes", ""),
        "system": system_text,
        "user_template": user_text,
    }


def _parse_simple_yaml(text: str) -> dict[str, str]:
    """Минимальный YAML-парсер для frontmatter (key: value)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ─── Upsert в БД ─────────────────────────────────────────────────────────────


def upsert_prompt(data: dict[str, Any]) -> int:
    """Вставляет/обновляет промпт. Делает is_active=True для этой версии,
    is_active=False — для остальных версий того же kind.

    Возвращает id строки.
    """
    with session_scope() as session:
        existing = session.execute(
            select(Prompt).where(Prompt.kind == data["kind"], Prompt.version == data["version"])
        ).scalar_one_or_none()

        if existing is not None:
            existing.system_prompt = data["system"]
            existing.user_template = data["user_template"]
            existing.notes = data.get("notes") or None
            prompt_id = existing.id
        else:
            row = Prompt(
                kind=data["kind"],
                version=data["version"],
                system_prompt=data["system"],
                user_template=data["user_template"],
                notes=data.get("notes") or None,
                is_active=True,
            )
            session.add(row)
            session.flush()
            prompt_id = row.id

        # Деактивируем все остальные версии этого kind
        others = session.execute(
            select(Prompt).where(Prompt.kind == data["kind"], Prompt.id != prompt_id)
        ).scalars()
        for p in others:
            p.is_active = False

        # Активируем нашу
        session.execute(select(Prompt).where(Prompt.id == prompt_id)).scalar_one().is_active = True

        return prompt_id


def upsert_few_shot(data: dict[str, Any]) -> int:
    """Вставляет/обновляет few-shot пример."""
    with session_scope() as session:
        existing = session.execute(
            select(FewShotExample).where(FewShotExample.slug == data["slug"])
        ).scalar_one_or_none()
        if existing is not None:
            existing.rubric = data.get("rubric", "any")
            existing.language = data.get("language", "русский")
            existing.body = data["body"]
            existing.quality_score = int(data.get("quality_score", 9))
            existing.notes = data.get("notes") or None
            existing.is_active = True
            return existing.id
        row = FewShotExample(
            slug=data["slug"],
            rubric=data.get("rubric", "any"),
            language=data.get("language", "русский"),
            body=data["body"],
            quality_score=int(data.get("quality_score", 9)),
            notes=data.get("notes") or None,
            is_active=True,
        )
        session.add(row)
        session.flush()
        return row.id


def parse_few_shot_file(path: Path) -> dict[str, Any]:
    """Читает few_shot/*.md с frontmatter — slug/rubric/language/quality_score/notes + body."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(f"Frontmatter не найден в {path.name}")
    meta = _parse_simple_yaml(match.group(1))
    body = match.group(2).strip()
    return {
        "slug": meta["slug"],
        "rubric": meta.get("rubric", "any"),
        "language": meta.get("language", "русский"),
        "quality_score": meta.get("quality_score", "9"),
        "notes": meta.get("notes", ""),
        "body": body,
    }


# ─── Ротация few-shot ────────────────────────────────────────────────────────


def sample_few_shot(
    *, rubric: str = "any", count: int = 3, language: str = "русский"
) -> list[FewShotItem]:
    """Возвращает до `count` few-shot примеров.

    Стратегия:
      1. Сначала — примеры с точным совпадением рубрики
      2. Дополняем универсальными (rubric='any')
      3. Внутри каждой группы — взвешенный выбор по quality_score

    Это даёт стилистическое разнообразие (ротация на каждый запуск)
    без потери релевантности рубрике.
    """
    with session_scope() as session:
        all_items = list(
            session.execute(
                select(FewShotExample).where(
                    FewShotExample.is_active.is_(True),
                    FewShotExample.language == language,
                )
            ).scalars()
        )

    if not all_items:
        return []

    rubric_match = [x for x in all_items if x.rubric == rubric and rubric != "any"]
    fallback = [x for x in all_items if x.rubric == "any" or x.rubric != rubric]
    # Внутри каждой группы — взвешенный shuffle по quality_score
    rubric_match = _weighted_shuffle(rubric_match)
    fallback = _weighted_shuffle(fallback)

    chosen: list[FewShotExample] = []
    # Сначала добираем из rubric_match (до count)
    for x in rubric_match:
        if len(chosen) >= count:
            break
        chosen.append(x)
    # Затем — из fallback, избегая дубликатов
    for x in fallback:
        if len(chosen) >= count:
            break
        if x.id in {c.id for c in chosen}:
            continue
        chosen.append(x)

    return [
        FewShotItem(
            id=x.id,
            slug=x.slug,
            rubric=x.rubric,
            body=x.body,
            quality_score=x.quality_score,
        )
        for x in chosen
    ]


def _weighted_shuffle(items: list[FewShotExample]) -> list[FewShotExample]:
    """Псевдо-shuffle с весами quality_score (выше score = чаще в начале)."""
    if not items:
        return items
    weighted = [(random.random() * x.quality_score, x) for x in items]
    weighted.sort(key=lambda kv: kv[0], reverse=True)
    return [x for _, x in weighted]


def format_few_shot_for_prompt(examples: list[FewShotItem]) -> str:
    """Форматирует список примеров в блок для подстановки в {few_shot_examples}."""
    if not examples:
        return "(нет эталонных примеров — пиши на общих основаниях)"
    blocks: list[str] = []
    for i, ex in enumerate(examples, 1):
        blocks.append(
            f"ПРИМЕР {i} ({ex.rubric}):\n─────────────────────────────\n"
            f"{ex.body}\n─────────────────────────────"
        )
    return "\n\n".join(blocks)


# ─── Управление активной версией ─────────────────────────────────────────────


def set_active_version(kind: str, version: str) -> None:
    """Переключает активную версию данного kind."""
    with session_scope() as session:
        target = session.execute(
            select(Prompt).where(Prompt.kind == kind, Prompt.version == version)
        ).scalar_one_or_none()
        if target is None:
            raise ValueError(f"Версия {version!r} промпта {kind!r} не найдена.")
        # Деактивируем все, активируем нужную
        others = session.execute(
            select(Prompt).where(Prompt.kind == kind, Prompt.id != target.id)
        ).scalars()
        for p in others:
            p.is_active = False
        target.is_active = True


def list_versions(kind: str | None = None) -> list[Prompt]:
    """Возвращает все версии промптов (опционально фильтр по kind)."""
    with session_scope() as session:
        stmt = select(Prompt)
        if kind:
            stmt = stmt.where(Prompt.kind == kind)
        return list(session.execute(stmt.order_by(Prompt.kind, Prompt.created_at)).scalars())


def rollback(kind: str) -> str:
    """Откат на предыдущую (по created_at) версию данного kind.
    Возвращает имя версии, которая стала активной."""
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Prompt).where(Prompt.kind == kind).order_by(Prompt.created_at.desc())
            ).scalars()
        )
        if len(rows) < 2:
            raise ValueError(f"Откат невозможен: для {kind!r} в БД только {len(rows)} версия(и).")
        # rows[0] — текущая активная, rows[1] — предыдущая
        prev = rows[1]
        for p in rows:
            p.is_active = p.id == prev.id
        return prev.version


# ─── Bulk seed ───────────────────────────────────────────────────────────────


def seed_all_from_files() -> tuple[int, int]:
    """Сидит все .md-файлы из prompts/ и prompts/few_shot/ в БД.

    Возвращает (n_prompts, n_few_shot).
    """
    n_prompts = 0
    for f in sorted(PROMPTS_DIR.glob("*_v*.md")):
        data = parse_prompt_file(f)
        upsert_prompt(data)
        n_prompts += 1
        log.info("Seeded prompt: %s %s", data["kind"], data["version"])

    n_few_shot = 0
    if FEW_SHOT_DIR.exists():
        for f in sorted(FEW_SHOT_DIR.glob("*.md")):
            data = parse_few_shot_file(f)
            upsert_few_shot(data)
            n_few_shot += 1
            log.info("Seeded few_shot: %s (%s)", data["slug"], data["rubric"])

    return n_prompts, n_few_shot
