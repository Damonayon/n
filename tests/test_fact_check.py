"""Тесты bot.fact_check — защита от галлюцинаций (T2.6)."""

from __future__ import annotations

import json
from unittest.mock import patch

from bot.ai import LLMResponse, ModelTier
from bot.fact_check import (
    FactCheckResult,
    FactIssue,
    Severity,
    ai_fact_check,
    fact_check_post,
    find_unsourced_names,
    find_unsourced_numbers,
    python_fact_check,
)


def _llm(payload: dict) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False),
        model_used="gpt-4o-mini",
        tier=ModelTier.CRITIC,
    )


# ─── Python: цифры ───────────────────────────────────────────────────────────


class TestFindUnsourcedNumbers:
    def test_matching_numbers_no_issues(self) -> None:
        source = "В исследовании участвовало 4500 человек, 73% используют ИИ ежедневно."
        post = "📊 73% сотрудников. Исследование охватило 4500 респондентов."
        assert find_unsourced_numbers(post, source) == []

    def test_fabricated_number_critical(self) -> None:
        source = "В исследовании участвовало 4500 человек."
        post = "📊 9999% сотрудников. Этого числа в источнике нет."
        issues = find_unsourced_numbers(post, source)
        assert len(issues) >= 1
        assert all(i.severity == Severity.CRITICAL for i in issues)
        assert any("9999" in i.fragment for i in issues)

    def test_ignores_single_digit(self) -> None:
        # «1 минута», «2 человека» — слишком общие, не флагаем
        source = "Текст без цифр."
        post = "Через 1 минуту приходит ответ. 2 шага и готово."
        assert find_unsourced_numbers(post, source) == []

    def test_currency_in_source(self) -> None:
        source = "OpenAI сэкономит $12 млн в год."
        post = "На этом OpenAI сэкономит $12 млн."
        assert find_unsourced_numbers(post, source) == []

    def test_whitespace_in_number(self) -> None:
        # «50 000» в посте vs «50000» в источнике — должно матчиться
        source = "Собрал 50000 пользователей."
        post = "Собрал <b>50 000 пользователей</b>."
        assert find_unsourced_numbers(post, source) == []


# ─── Python: имена ───────────────────────────────────────────────────────────


class TestFindUnsourcedNames:
    def test_whitelisted_names_not_flagged(self) -> None:
        source = "Big news today."
        post = "OpenAI и Google объявили о партнёрстве."
        # Оба в whitelist
        assert find_unsourced_names(post, source) == []

    def test_unknown_name_warning(self) -> None:
        source = "Большая новость."
        post = "Компания Krymsoft анонсировала бесплатную модель."
        issues = find_unsourced_names(post, source)
        assert len(issues) >= 1
        assert all(i.severity == Severity.WARNING for i in issues)

    def test_name_in_source_not_flagged(self) -> None:
        source = "Stripe раунд на $640 млн."
        post = "Stripe объявил о привлечении средств."
        assert find_unsourced_names(post, source) == []

    def test_limits_to_5(self) -> None:
        source = ""
        post = " ".join(f"Компанияизмышлено{i}кейс" for i in range(20))
        issues = find_unsourced_names(post, source)
        assert len(issues) <= 5


# ─── Объединённый ────────────────────────────────────────────────────────────


class TestPythonFactCheck:
    def test_clean_post_no_issues(self) -> None:
        source = "OpenAI запустил GPT-5. Привлекли $1 млрд."
        post = "🚀 OpenAI запустил GPT-5 за $1 млрд."
        assert python_fact_check(post, source) == []

    def test_combines_numbers_and_names(self) -> None:
        source = "Простой текст."
        post = "Krymsoft объявил о цифре 9999."
        issues = python_fact_check(post, source)
        kinds = {i.kind for i in issues}
        assert "number_not_in_source" in kinds
        assert "name_not_in_source" in kinds


# ─── AI fact-check ───────────────────────────────────────────────────────────


class TestAiFactCheck:
    def test_no_source_returns_empty(self) -> None:
        issues, summary = ai_fact_check("post", "")
        assert issues == []
        assert summary == "no source"

    def test_clean_response(self) -> None:
        resp = _llm({"ok": True, "issues": [], "summary": "всё чисто"})
        with patch("bot.fact_check.call_llm", return_value=resp):
            issues, summary = ai_fact_check("пост", "источник")
            assert issues == []
            assert "чисто" in summary

    def test_finds_unsourced(self) -> None:
        resp = _llm(
            {
                "ok": False,
                "issues": [
                    {"severity": "critical", "fragment": "9999", "note": "выдуман"},
                    {"severity": "warning", "fragment": "Krymsoft", "note": "нет в источнике"},
                ],
                "summary": "2 проблемы",
            }
        )
        with patch("bot.fact_check.call_llm", return_value=resp):
            issues, _ = ai_fact_check("пост с 9999", "источник")
            assert len(issues) == 2
            assert any(i.severity == Severity.CRITICAL for i in issues)
            assert any(i.severity == Severity.WARNING for i in issues)
            for i in issues:
                assert i.kind == "ai_unsourced_claim"

    def test_failsafe_on_error(self) -> None:
        with patch("bot.fact_check.call_llm", side_effect=RuntimeError("LLM down")):
            issues, summary = ai_fact_check("пост", "источник")
            assert issues == []
            assert "ai error" in summary


# ─── Результат ───────────────────────────────────────────────────────────────


class TestFactCheckResult:
    def test_passed_when_no_critical(self) -> None:
        r = FactCheckResult(issues=[FactIssue(severity=Severity.WARNING, kind="x", fragment="y")])
        assert r.passed
        assert r.has_warnings
        assert not r.has_critical

    def test_not_passed_when_critical(self) -> None:
        r = FactCheckResult(issues=[FactIssue(severity=Severity.CRITICAL, kind="x", fragment="y")])
        assert not r.passed

    def test_json_roundtrip(self) -> None:
        r = FactCheckResult(
            issues=[
                FactIssue(severity=Severity.CRITICAL, kind="number_not_in_source", fragment="9999"),
                FactIssue(severity=Severity.WARNING, kind="name_not_in_source", fragment="Foo"),
            ],
            ai_checked=True,
            ai_summary="test",
        )
        restored = json.loads(r.to_json())
        assert restored["ai_checked"] is True
        assert restored["ai_summary"] == "test"
        assert len(restored["issues"]) == 2

    def test_summary_format(self) -> None:
        r = FactCheckResult(
            issues=[
                FactIssue(severity=Severity.CRITICAL, kind="x", fragment="a"),
                FactIssue(severity=Severity.WARNING, kind="y", fragment="b"),
            ]
        )
        s = r.summary()
        assert "critical=1" in s
        assert "warning=1" in s
        assert "❌" in s


# ─── End-to-end ──────────────────────────────────────────────────────────────


class TestFactCheckPost:
    def test_python_only(self) -> None:
        source = "Stripe раунд на $640 млн."
        post = "Stripe привлёк $640 млн."
        result = fact_check_post(post, source, use_ai=False)
        assert result.passed
        assert not result.ai_checked

    def test_python_finds_critical(self) -> None:
        source = "Тест без цифр."
        post = "🚀 За 999999 секунд решено."
        result = fact_check_post(post, source, use_ai=False)
        assert not result.passed
        assert result.has_critical
