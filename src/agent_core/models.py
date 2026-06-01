"""Model tier abstraction over Anthropic API with budget circuit breaker.

Usage:
    from agent_core.models import Tier, complete, get_run_budget

    # Budget is process-global, keyed by run_id (thread_id from LangGraph).
    budget = get_run_budget(run_id="abc123")
    text = complete(prompt, tier=Tier.MID, budget=budget)
"""

from __future__ import annotations

import logging
import os
import threading
from enum import Enum
from typing import Any

from .budget import Budget, BudgetExceededError, get_budget

log = logging.getLogger(__name__)


# ── tier definitions ──────────────────────────────────────────────────────────

class Tier(str, Enum):
    FAST = "fast"    # haiku  — cheap, quick
    MID  = "mid"     # sonnet — default worker
    HARD = "hard"    # opus   — fix loop, critic


_TIER_MODEL: dict[Tier, str] = {
    Tier.FAST: "claude-haiku-4-5-20251001",
    Tier.MID:  "claude-sonnet-4-6",
    Tier.HARD: "claude-opus-4-8",
}

# USD per 1M tokens (input, output) — Anthropic pricing as of 2025
_TIER_PRICING: dict[Tier, tuple[float, float]] = {
    Tier.FAST: (0.80,  4.00),    # haiku
    Tier.MID:  (3.00,  15.00),   # sonnet
    Tier.HARD: (15.00, 75.00),   # opus
}


# ── run-level budget registry (process-global, keyed by run_id) ───────────────

_budgets: dict[str, Budget] = {}
_budgets_lock = threading.Lock()


def get_run_budget(run_id: str) -> Budget:
    """Return the Budget for this run, creating it on first call."""
    with _budgets_lock:
        if run_id not in _budgets:
            _budgets[run_id] = get_budget()
            log.info("Budget created for run %s: max $%.2f", run_id, _budgets[run_id].max_usd)
        return _budgets[run_id]


def clear_run_budget(run_id: str) -> None:
    with _budgets_lock:
        _budgets.pop(run_id, None)


# ── token cost calculator ─────────────────────────────────────────────────────

def tokens_to_usd(tier: Tier, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = _TIER_PRICING[tier]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


# ── complete() ────────────────────────────────────────────────────────────────

def complete(
    prompt: str,
    *,
    tier: Tier = Tier.MID,
    system: str | None = None,
    max_tokens: int = 4096,
    budget: Budget | None = None,
) -> str:
    """Call the Anthropic API at the given tier.

    Returns the assistant text.
    Raises BudgetExceededError if the accumulated cost exceeds the budget.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = _TIER_MODEL[tier]

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    text = response.content[0].text

    # Charge budget if provided
    usage = response.usage
    cost = tokens_to_usd(tier, usage.input_tokens, usage.output_tokens)
    log.debug(
        "LLM call: model=%s  in=%d  out=%d  cost=$%.5f",
        model, usage.input_tokens, usage.output_tokens, cost,
    )

    if budget is not None:
        budget.charge(cost)  # raises BudgetExceededError if over limit

    return text


def complete_with_cost(
    prompt: str,
    *,
    tier: Tier = Tier.MID,
    system: str | None = None,
    max_tokens: int = 4096,
    budget: Budget | None = None,
) -> tuple[str, float]:
    """Like complete(), but also returns the USD cost of this call."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = _TIER_MODEL[tier]

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    text = response.content[0].text

    usage = response.usage
    cost = tokens_to_usd(tier, usage.input_tokens, usage.output_tokens)

    if budget is not None:
        budget.charge(cost)

    return text, cost
