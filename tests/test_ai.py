"""Тесты bot.ai — каскад моделей по ModelTier."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot.ai import MODEL_CASCADES, ModelTier, call_llm


def _mock_response(status: int = 200, content: str = "result", model: str = "gpt-4o") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "model": model,
    }
    return resp


class TestModelTier:
    def test_str_values(self) -> None:
        assert ModelTier.CHEAP == "cheap"
        assert ModelTier.SMART == "smart"
        assert ModelTier.CRITIC == "critic"

    def test_cascades_have_at_least_two_models(self) -> None:
        for tier, cascade in MODEL_CASCADES.items():
            assert len(cascade) >= 2, f"{tier} cascade слишком короткий"

    def test_cheap_starts_with_mini(self) -> None:
        assert MODEL_CASCADES[ModelTier.CHEAP][0] == "gpt-4o-mini"

    def test_smart_starts_with_4o(self) -> None:
        assert MODEL_CASCADES[ModelTier.SMART][0] == "gpt-4o"


class TestCallLlm:
    def test_returns_content_on_success(self) -> None:
        with patch("bot.ai._call_one", return_value=_mock_response(200, "hello")):
            result = call_llm([{"role": "user", "content": "x"}], tier=ModelTier.CHEAP)
            assert result.content == "hello"
            assert result.tier == ModelTier.CHEAP
            assert result.model_used == "gpt-4o-mini"

    def test_falls_back_to_next_model_on_404(self) -> None:
        # Первая — 404, вторая — 200
        responses = [_mock_response(404), _mock_response(200, "ok")]
        with patch("bot.ai._call_one", side_effect=responses):
            result = call_llm([{"role": "user", "content": "x"}], tier=ModelTier.CHEAP)
            assert result.content == "ok"
            assert result.model_used == "gpt-4o"  # второй в cascade CHEAP

    def test_smart_tier_uses_4o_first(self) -> None:
        with patch("bot.ai._call_one", return_value=_mock_response(200, "creative")) as mock:
            result = call_llm([{"role": "user", "content": "x"}], tier=ModelTier.SMART)
            assert result.model_used == "gpt-4o"
            # Проверяем, что вызвали именно gpt-4o
            assert mock.call_args.args[0] == "gpt-4o"

    def test_all_models_fail(self) -> None:
        with (
            patch("bot.ai._call_one", return_value=_mock_response(404)),
            pytest.raises(RuntimeError, match="все модели каскада недоступны"),
        ):
            call_llm([{"role": "user", "content": "x"}], tier=ModelTier.CHEAP)

    def test_json_mode_passed_through(self) -> None:
        with patch("bot.ai._call_one", return_value=_mock_response(200)) as mock:
            call_llm(
                [{"role": "user", "content": "x"}],
                tier=ModelTier.CHEAP,
                json_mode=True,
            )
            # 5-й позиционный аргумент = json_mode
            assert mock.call_args.args[4] is True
