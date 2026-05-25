"""Тесты bot.prompts — Promptops: версионирование + ротация few-shot."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Изолированная БД для тестов (общий conftest должен переопределять DB_URL)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_MODERATOR_ID", "1")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "test")
os.environ.setdefault("GH_MODELS_TOKEN", "test")


from bot.db import init_db, session_scope
from bot.models import FewShotExample
from bot.prompts import (
    _parse_simple_yaml,
    format_few_shot_for_prompt,
    get_active_prompt,
    list_versions,
    parse_few_shot_file,
    parse_prompt_file,
    rollback,
    sample_few_shot,
    set_active_version,
    upsert_few_shot,
    upsert_prompt,
)

# ─── YAML parser ─────────────────────────────────────────────────────────────


class TestParseSimpleYaml:
    def test_basic(self) -> None:
        text = "kind: generator\nversion: v1\nnotes: hello"
        result = _parse_simple_yaml(text)
        assert result == {"kind": "generator", "version": "v1", "notes": "hello"}

    def test_strips_quotes(self) -> None:
        result = _parse_simple_yaml("kind: \"generator\"\nnotes: 'hi'")
        assert result["kind"] == "generator"
        assert result["notes"] == "hi"

    def test_skips_empty_and_comments(self) -> None:
        result = _parse_simple_yaml("# comment\n\nkey: value\n")
        assert result == {"key": "value"}


# ─── Parse .md files ─────────────────────────────────────────────────────────


class TestParsePromptFile:
    def test_existing_filter_v1(self) -> None:
        path = Path(__file__).resolve().parent.parent / "prompts" / "filter_v1.md"
        data = parse_prompt_file(path)
        assert data["kind"] == "filter"
        assert data["version"] == "v1"
        assert "главный редактор" in data["system"]
        assert "{title}" in data["user_template"]

    def test_existing_generator_v1_has_few_shot_marker(self) -> None:
        path = Path(__file__).resolve().parent.parent / "prompts" / "generator_v1.md"
        data = parse_prompt_file(path)
        assert "{few_shot_examples}" in data["user_template"], (
            "Шаблон должен содержать маркер для подстановки эталонов"
        )


class TestParseFewShotFile:
    def test_existing_launch_001(self) -> None:
        path = Path(__file__).resolve().parent.parent / "prompts" / "few_shot" / "launch_001.md"
        data = parse_few_shot_file(path)
        assert data["slug"] == "launch_001"
        assert "Запуск" in data["rubric"]
        assert data["language"] == "русский"
        assert "v0" in data["body"]


# ─── Upsert + загрузка + ротация ────────────────────────────────────────────


@pytest.fixture
def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Изолированная БД для каждого теста."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")

    # Сбрасываем синглтоны bot.config и bot.db, чтобы они пересоздались с новым DB_URL
    import bot.config
    import bot.db

    bot.config._settings = None
    bot.db._engine = None
    bot.db._SessionLocal = None

    init_db()


class TestUpsertPrompt:
    def test_first_insert_becomes_active(self, clean_db: None) -> None:
        upsert_prompt(
            {
                "kind": "generator",
                "version": "v1",
                "system": "sys",
                "user_template": "tpl",
                "notes": "",
            }
        )
        loaded = get_active_prompt("generator")
        assert loaded.version == "v1"
        assert loaded.system == "sys"

    def test_second_version_deactivates_first(self, clean_db: None) -> None:
        upsert_prompt(
            {
                "kind": "generator",
                "version": "v1",
                "system": "s1",
                "user_template": "u1",
                "notes": "",
            }
        )
        upsert_prompt(
            {
                "kind": "generator",
                "version": "v2",
                "system": "s2",
                "user_template": "u2",
                "notes": "",
            }
        )
        loaded = get_active_prompt("generator")
        assert loaded.version == "v2"
        # Проверяем, что v1 теперь неактивна
        all_versions = list_versions("generator")
        v1 = next(p for p in all_versions if p.version == "v1")
        assert v1.is_active is False


class TestRotation:
    def test_set_active_version(self, clean_db: None) -> None:
        for v in ("v1", "v2"):
            upsert_prompt(
                {
                    "kind": "filter",
                    "version": v,
                    "system": f"sys-{v}",
                    "user_template": "x",
                    "notes": "",
                }
            )
        # После двух upsert активна v2 — переключаем на v1
        set_active_version("filter", "v1")
        assert get_active_prompt("filter").version == "v1"

    def test_rollback(self, clean_db: None) -> None:
        for v in ("v1", "v2"):
            upsert_prompt(
                {"kind": "filter", "version": v, "system": "x", "user_template": "x", "notes": ""}
            )
        # Активна v2 → откат должен вернуть v1
        new_v = rollback("filter")
        assert new_v == "v1"
        assert get_active_prompt("filter").version == "v1"

    def test_rollback_fails_with_single_version(self, clean_db: None) -> None:
        upsert_prompt(
            {"kind": "filter", "version": "v1", "system": "x", "user_template": "x", "notes": ""}
        )
        with pytest.raises(ValueError, match="Откат невозможен"):
            rollback("filter")


# ─── Few-shot ────────────────────────────────────────────────────────────────


@pytest.fixture
def populated_few_shot(clean_db: None) -> None:
    """6 few-shot примеров: 3 разных рубрики + 2 'any'."""
    examples = [
        {"slug": "a_launch", "rubric": "🚀 Запуск", "body": "хук про запуск", "quality_score": 9},
        {"slug": "b_launch", "rubric": "🚀 Запуск", "body": "ещё запуск", "quality_score": 7},
        {
            "slug": "c_scandal",
            "rubric": "🔻 Скандал",
            "body": "скандальный пост",
            "quality_score": 9,
        },
        {
            "slug": "d_number",
            "rubric": "📊 Цифры",
            "body": "73% сотрудников...",
            "quality_score": 8,
        },
        {"slug": "e_any1", "rubric": "any", "body": "универсальный 1", "quality_score": 9},
        {"slug": "f_any2", "rubric": "any", "body": "универсальный 2", "quality_score": 8},
    ]
    for ex in examples:
        upsert_few_shot({**ex, "language": "русский", "notes": ""})


class TestSampleFewShot:
    def test_returns_at_most_count(self, populated_few_shot: None) -> None:
        items = sample_few_shot(rubric="🚀 Запуск", count=2)
        assert len(items) == 2

    def test_prefers_matching_rubric(self, populated_few_shot: None) -> None:
        # Просим 2 для рубрики "🚀 Запуск" — должны быть оба запусковых
        items = sample_few_shot(rubric="🚀 Запуск", count=2)
        slugs = {x.slug for x in items}
        assert slugs == {"a_launch", "b_launch"}

    def test_fills_with_any_when_not_enough_rubric(self, populated_few_shot: None) -> None:
        # В рубрике "🚀 Запуск" только 2 примера. Просим 3 — третий должен прийти из fallback.
        items = sample_few_shot(rubric="🚀 Запуск", count=3)
        assert len(items) == 3
        slugs = {x.slug for x in items}
        assert "a_launch" in slugs
        assert "b_launch" in slugs

    def test_language_filter(self, populated_few_shot: None) -> None:
        # Все примеры на русском, английский запрос должен вернуть []
        items = sample_few_shot(rubric="any", count=3, language="english")
        assert items == []

    def test_no_duplicates(self, populated_few_shot: None) -> None:
        items = sample_few_shot(rubric="🚀 Запуск", count=5)
        slugs = [x.slug for x in items]
        assert len(slugs) == len(set(slugs)), "ротация не должна давать дубликатов"


class TestFormatBlock:
    def test_empty_pool_returns_marker(self) -> None:
        text = format_few_shot_for_prompt([])
        assert "нет эталонных" in text

    def test_includes_all_bodies(self, populated_few_shot: None) -> None:
        items = sample_few_shot(rubric="🔻 Скандал", count=2)
        text = format_few_shot_for_prompt(items)
        for item in items:
            assert item.body in text


# ─── Idempotency ─────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_upsert_twice_doesnt_duplicate(self, clean_db: None) -> None:
        for _ in range(2):
            upsert_prompt(
                {
                    "kind": "filter",
                    "version": "v1",
                    "system": "s",
                    "user_template": "u",
                    "notes": "",
                }
            )
        rows = list_versions("filter")
        assert len(rows) == 1

    def test_few_shot_upsert_idempotent(self, clean_db: None) -> None:
        for _ in range(3):
            upsert_few_shot(
                {
                    "slug": "x",
                    "rubric": "any",
                    "language": "русский",
                    "body": "b",
                    "quality_score": 9,
                    "notes": "",
                }
            )
        with session_scope() as session:
            count = session.query(FewShotExample).filter_by(slug="x").count()
        assert count == 1
