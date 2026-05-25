"""Тесты bot.classifier — AI-классификация рубрики + anti-repeat балансировка."""

from __future__ import annotations

from unittest.mock import patch

from bot.ai import LLMResponse, ModelTier
from bot.classifier import classify_article, classify_with_ai
from bot.rubrics import DEFAULT_RUBRIC, apply_anti_repeat


def _llm(payload: dict) -> LLMResponse:
    import json

    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used="gpt-4o-mini",
        tier=ModelTier.CHEAP,
    )


class TestClassifyWithAi:
    def test_high_confidence_pass_through(self) -> None:
        resp = _llm({"rubric": "launch", "confidence": 0.95, "reason": "релиз продукта"})
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_with_ai("OpenAI launches GPT-5", "release info")
            assert r.rubric.slug == "launch"
            assert r.confidence == 0.95
            assert r.source == "ai"

    def test_unknown_slug_falls_back_to_default(self) -> None:
        resp = _llm({"rubric": "nonexistent", "confidence": 0.9})
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_with_ai("x", "y")
            assert r.rubric.slug == DEFAULT_RUBRIC.slug
            assert r.source == "default"

    def test_clamps_confidence(self) -> None:
        resp = _llm({"rubric": "launch", "confidence": 5.0})  # out-of-range
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_with_ai("x", "y")
            assert 0.0 <= r.confidence <= 1.0

    def test_failsafe_on_exception(self) -> None:
        with patch("bot.classifier.call_llm", side_effect=RuntimeError("LLM down")):
            r = classify_with_ai("x", "y")
            assert r.rubric.slug == DEFAULT_RUBRIC.slug
            assert r.source == "default"
            assert "ai error" in r.reason


class TestClassifyArticle:
    def test_high_confidence_uses_ai(self) -> None:
        resp = _llm({"rubric": "scandal", "confidence": 0.9, "reason": "увольнение"})
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_article("Microsoft уволил 10000", "")
            assert r.rubric.slug == "scandal"
            assert r.source == "ai"

    def test_low_confidence_falls_back_to_heuristic(self) -> None:
        # AI не уверен → эвристика по слову "launch"
        resp = _llm({"rubric": "launch", "confidence": 0.3})
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_article("Apple launches iPhone", "")
            assert r.source == "heuristic"
            assert r.rubric.slug == "launch"

    def test_low_confidence_no_heuristic_uses_default(self) -> None:
        # Низкая уверенность + эвристика не находит trigger
        resp = _llm({"rubric": "launch", "confidence": 0.2})
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_article("xyz qqq", "abc def")
            assert r.source == "default"
            assert r.rubric.slug == DEFAULT_RUBRIC.slug

    def test_anti_repeat_switches_rubric(self) -> None:
        # AI выдаёт launch, но последние 2 поста уже launch — переключаемся
        resp = _llm({"rubric": "launch", "confidence": 0.9})
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_article(
                "Apple launches iPhone",
                "",
                recent_slugs=["scandal", "launch", "launch"],
            )
            assert r.source == "anti_repeat"
            assert r.rubric.slug == DEFAULT_RUBRIC.slug

    def test_anti_repeat_doesnt_switch_after_one_in_row(self) -> None:
        # Только один launch недавно — переключать не надо
        resp = _llm({"rubric": "launch", "confidence": 0.9})
        with patch("bot.classifier.call_llm", return_value=resp):
            r = classify_article("X", "", recent_slugs=["scandal", "launch"])
            assert r.source == "ai"
            assert r.rubric.slug == "launch"


class TestApplyAntiRepeat:
    def test_empty_history(self) -> None:
        assert apply_anti_repeat("launch", "case", []) == "launch"

    def test_two_in_row_triggers_switch(self) -> None:
        assert apply_anti_repeat("launch", "case", ["scandal", "launch", "launch"]) == "case"

    def test_one_in_row_no_switch(self) -> None:
        assert apply_anti_repeat("launch", "case", ["launch"]) == "launch"

    def test_three_in_row_still_switches(self) -> None:
        assert apply_anti_repeat("launch", "case", ["launch", "launch", "launch"]) == "case"
