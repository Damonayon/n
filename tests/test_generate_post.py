"""Тесты scripts.generate_post — парсинг ответов модели, ensure_correct_link, рубрики."""

from __future__ import annotations

import pytest

from bot.rubrics import RUBRICS, by_slug, heuristic_detect
from scripts.generate_post import (
    _extract_json,
    ensure_correct_link,
    parse_post,
)


class TestExtractJson:
    def test_clean_json(self) -> None:
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_json_in_markdown(self) -> None:
        raw = '```json\n{"a": 1, "b": "x"}\n```'
        assert _extract_json(raw) == {"a": 1, "b": "x"}

    def test_json_no_lang_marker(self) -> None:
        raw = '```\n{"a": 1}\n```'
        assert _extract_json(raw) == {"a": 1}

    def test_extra_text_around_block(self) -> None:
        raw = 'Here you go: ```json\n{"a": 1}\n``` Enjoy!'
        assert _extract_json(raw) == {"a": 1}


class TestParsePost:
    def test_valid_russian(self) -> None:
        raw = '{"post": "Это пост на русском с большим количеством кириллицы для проверки", "image_prompt": "test"}'
        text, prompt = parse_post(raw)
        assert "русском" in text
        assert prompt == "test"

    def test_rejects_too_few_cyrillic(self) -> None:
        raw = '{"post": "This is English post about AI", "image_prompt": "test"}'
        with pytest.raises(ValueError, match="не на русском"):
            parse_post(raw)

    def test_rejects_empty_post(self) -> None:
        with pytest.raises(ValueError, match="Пустой"):
            parse_post('{"post": "", "image_prompt": "x"}')

    def test_default_image_prompt(self) -> None:
        raw = '{"post": "Это пост на русском с большим количеством кириллицы для проверки", "image_prompt": ""}'
        _, prompt = parse_post(raw)
        assert prompt  # дефолтный непустой


class TestEnsureCorrectLink:
    def test_adds_link_when_missing(self) -> None:
        post = "Текст поста"
        result = ensure_correct_link(post, "https://x.com/a")
        assert '<a href="https://x.com/a">📖 Читать полностью</a>' in result

    def test_keeps_correct_link(self) -> None:
        post = 'Текст\n\n<a href="https://x.com/a">📖 Читать полностью</a>'
        result = ensure_correct_link(post, "https://x.com/a")
        # Не должно задвоиться
        assert result.count('<a href="https://x.com/a">') == 1

    def test_replaces_wrong_link(self) -> None:
        post = 'Текст\n\n<a href="https://wrong.com/y">📖 Читать полностью</a>'
        result = ensure_correct_link(post, "https://x.com/a")
        assert 'href="https://x.com/a"' in result
        assert 'href="https://wrong.com/y"' not in result


class TestRubricsCatalog:
    """T2.5 — каталог 16+ рубрик."""

    def test_catalog_has_15plus(self) -> None:
        assert len(RUBRICS) >= 15

    def test_all_rubrics_have_required_metadata(self) -> None:
        for r in RUBRICS.values():
            assert r.name and r.description and r.tone and r.cta_style
            assert r.triggers, f"{r.slug}: пустые triggers"


class TestHeuristicDetect:
    """Backup-эвристика по ключевым словам — для случая если AI лежит."""

    @pytest.mark.parametrize(
        ("text", "expected_slug"),
        [
            ("OpenAI launches new model", "launch"),
            ("Компания представила новый продукт", "launch"),
            ("Google уволил 12000 сотрудников", "scandal"),
            ("Microsoft fired 10000 employees", "scandal"),
            ("Study shows 73% use ChatGPT", "number_of_day"),
            # «исследование» — trigger у number_of_day (раньше идёт в каталоге)
            ("Новое исследование: ИИ работает", "number_of_day"),
            # research отличается специфичными триггерами (Стэнфорд / MIT / paper)
            ("MIT paper proves AI mimics humans", "research"),
            ("Stripe raised $1 billion in funding", "investment"),
        ],
    )
    def test_keyword_routing(self, text: str, expected_slug: str) -> None:
        result = heuristic_detect(text)
        assert result == expected_slug, f"Expected {expected_slug} for {text!r}, got {result}"

    def test_unknown_returns_none(self) -> None:
        # Случайная фраза без trigger-слов — эвристика молчит
        assert heuristic_detect("xqz random text 42") is None

    def test_by_slug_matches_catalog(self) -> None:
        for slug in RUBRICS:
            r = by_slug(slug)
            assert r.slug == slug
