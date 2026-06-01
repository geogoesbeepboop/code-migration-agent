"""Tests for the budget circuit breaker and model tier pricing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_core.budget import Budget, BudgetExceededError, get_budget
from agent_core.models import Tier, tokens_to_usd, get_run_budget, clear_run_budget


# ── Budget dataclass ──────────────────────────────────────────────────────────

class TestBudget:
    def test_charge_within_limit(self):
        b = Budget(max_usd=10.0)
        b.charge(3.0)
        assert b.spent == pytest.approx(3.0)
        assert b.remaining == pytest.approx(7.0)

    def test_charge_exactly_at_limit_ok(self):
        b = Budget(max_usd=5.0)
        b.charge(5.0)  # should not raise

    def test_charge_over_limit_raises(self):
        b = Budget(max_usd=5.0)
        with pytest.raises(BudgetExceededError):
            b.charge(5.01)

    def test_multiple_charges_accumulate(self):
        b = Budget(max_usd=10.0)
        b.charge(3.0)
        b.charge(4.0)
        assert b.spent == pytest.approx(7.0)

    def test_accumulate_over_limit(self):
        b = Budget(max_usd=5.0)
        b.charge(3.0)
        with pytest.raises(BudgetExceededError):
            b.charge(3.0)  # 6.0 total > 5.0

    def test_remaining_never_negative(self):
        b = Budget(max_usd=5.0)
        try:
            b.charge(10.0)
        except BudgetExceededError:
            pass
        assert b.remaining == 0.0

    def test_thread_safety(self):
        """Multiple threads charging concurrently should not corrupt state."""
        import threading

        b = Budget(max_usd=1_000_000.0)
        errors: list[Exception] = []

        def charge():
            try:
                b.charge(0.001)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=charge) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert b.spent == pytest.approx(0.1, rel=1e-6)


class TestGetBudget:
    def test_default_max_usd(self, monkeypatch):
        monkeypatch.delenv("AGENT_CORE_MAX_USD_PER_TASK", raising=False)
        b = get_budget()
        assert b.max_usd == 5.0

    def test_custom_max_usd(self, monkeypatch):
        monkeypatch.setenv("AGENT_CORE_MAX_USD_PER_TASK", "25.0")
        b = get_budget()
        assert b.max_usd == 25.0


# ── token pricing ─────────────────────────────────────────────────────────────

class TestTokensToUsd:
    def test_haiku_pricing(self):
        cost = tokens_to_usd(Tier.FAST, input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(0.80)

    def test_sonnet_pricing(self):
        cost = tokens_to_usd(Tier.MID, input_tokens=0, output_tokens=1_000_000)
        assert cost == pytest.approx(15.0)

    def test_opus_pricing(self):
        cost = tokens_to_usd(Tier.HARD, input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(90.0)

    def test_small_call(self):
        # 1000 input tokens + 500 output at Sonnet
        cost = tokens_to_usd(Tier.MID, input_tokens=1000, output_tokens=500)
        expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_zero_tokens(self):
        assert tokens_to_usd(Tier.MID, 0, 0) == 0.0


# ── run-level budget registry ─────────────────────────────────────────────────

class TestGetRunBudget:
    def test_same_run_id_returns_same_budget(self):
        clear_run_budget("test_run_42")
        b1 = get_run_budget("test_run_42")
        b2 = get_run_budget("test_run_42")
        assert b1 is b2

    def test_different_run_ids_different_budgets(self):
        clear_run_budget("run_a")
        clear_run_budget("run_b")
        assert get_run_budget("run_a") is not get_run_budget("run_b")

    def test_cleared_budget_is_fresh(self):
        get_run_budget("clearable")
        get_run_budget("clearable").charge(1.0)
        clear_run_budget("clearable")
        fresh = get_run_budget("clearable")
        assert fresh.spent == 0.0


# ── complete() with mocked Anthropic ─────────────────────────────────────────

class TestComplete:
    def _mock_response(self, text: str, input_tokens=100, output_tokens=50):
        response = MagicMock()
        response.content = [MagicMock(text=text)]
        response.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
        return response

    def _call(self, *args, env_key="test_api_key", **kwargs):
        """Convenience: call complete() with mocked Anthropic + API key set."""
        from agent_core.models import complete
        import os
        mock_client = MagicMock()
        mock_client.messages.create.return_value = kwargs.pop("response", self._mock_response("out"))
        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": env_key}):
            result = complete(*args, **kwargs)
        return result, mock_client

    def test_returns_text(self):
        from agent_core.models import complete
        import os

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_response("diff output")

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            result = complete("test prompt", tier=Tier.MID)

        assert result == "diff output"

    def test_charges_budget(self):
        from agent_core.models import complete
        import os

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_response(
            "text", input_tokens=1_000_000, output_tokens=0
        )
        budget = Budget(max_usd=10.0)

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            complete("prompt", tier=Tier.MID, budget=budget)

        # Sonnet: 1M input tokens = $3.00
        assert budget.spent == pytest.approx(3.0)

    def test_raises_budget_exceeded(self):
        from agent_core.models import complete
        from agent_core.budget import BudgetExceededError
        import os

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_response(
            "text", input_tokens=1_000_000, output_tokens=1_000_000
        )
        budget = Budget(max_usd=1.0)  # tiny budget

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            with pytest.raises(BudgetExceededError):
                complete("prompt", tier=Tier.HARD, budget=budget)

    def test_uses_correct_model_for_tier(self):
        from agent_core.models import complete, _TIER_MODEL
        import os

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_response("out")

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            complete("p", tier=Tier.FAST)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == _TIER_MODEL[Tier.FAST]

    def test_passes_system_prompt(self):
        from agent_core.models import complete
        import os

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_response("out")

        with patch("anthropic.Anthropic", return_value=mock_client), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}):
            complete("p", system="You are an expert.", tier=Tier.MID)

        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs.get("system") == "You are an expert."
