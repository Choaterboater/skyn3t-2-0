"""Unified LLM client with cost-aware routing and an offline stub backend.

One entry point — :meth:`LLMClient.complete` — resolves a tier to a model via
the :class:`ModelRouter`, then dispatches to a backend:

* ``openrouter`` — real HTTP (primary) when ``OPENROUTER_API_KEY`` is set.
* ``stub`` — deterministic offline responses so the full pipeline (and the
  test suite) runs with **no keys and no network**. This is what makes
  "brief -> runnable app" demonstrable out of the box.

Every call is metered (tokens + estimated USD) and checked against budget
caps — design rules #5 (cheap by default) and #6 (degrade, don't crash).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import structlog

from skyn3t.config.settings import Settings, get_settings
from skyn3t.core.model_router import ModelRouter, Tier

log = structlog.get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class LLMResult:
    text: str
    model: str
    backend: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class BudgetTracker:
    """Hard backstop on spend. Raising stops a runaway loop."""
    per_build_cap: float
    daily_cap: float
    token_cap: int
    spent_build: float = 0.0
    spent_day: float = 0.0
    tokens_day: int = 0
    calls: list[LLMResult] = field(default_factory=list)

    def record(self, r: LLMResult) -> None:
        self.spent_build += r.cost_usd
        self.spent_day += r.cost_usd
        self.tokens_day += r.prompt_tokens + r.completion_tokens
        self.calls.append(r)

    def check(self) -> None:
        if self.spent_build > self.per_build_cap:
            raise BudgetExceeded(f"per-build cap ${self.per_build_cap} exceeded (${self.spent_build:.4f})")
        if self.spent_day > self.daily_cap:
            raise BudgetExceeded(f"daily cap ${self.daily_cap} exceeded (${self.spent_day:.4f})")
        if self.tokens_day > self.token_cap:
            raise BudgetExceeded(f"daily token cap {self.token_cap} exceeded ({self.tokens_day})")

    def reset_build(self) -> None:
        self.spent_build = 0.0


class BudgetExceeded(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings | None = None, router: ModelRouter | None = None) -> None:
        self.settings = settings or get_settings()
        self.router = router or ModelRouter(self.settings)
        self.budget = BudgetTracker(
            per_build_cap=self.settings.per_build_usd_cap,
            daily_cap=self.settings.daily_usd_cap,
            token_cap=self.settings.daily_token_cap,
        )

    @property
    def backend(self) -> str:
        return "openrouter" if self.settings.openrouter_api_key else "stub"

    async def complete(
        self,
        prompt: str,
        tier: Tier = Tier.CHEAP,
        *,
        system: str | None = None,
        file_hint: str | None = None,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResult:
        model = self.router.resolve(tier, file_hint)
        if self.backend == "openrouter":
            result = await self._openrouter(model, prompt, system, max_tokens, json_mode)
        else:
            result = self._stub(model, prompt, system, json_mode)
        self.budget.record(result)
        self.budget.check()
        return result

    # ---- backends --------------------------------------------------------
    async def _openrouter(self, model, prompt, system, max_tokens, json_mode) -> LLMResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "HTTP-Referer": "https://github.com/skyn3t",
            "X-Title": "SkyN3t",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        # :free models cost $0; otherwise rough estimate.
        cost = 0.0 if model.endswith(":free") else (pt + ct) / 1_000_000 * 0.5
        return LLMResult(text=text, model=model, backend="openrouter",
                         prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)

    def _stub(self, model, prompt, system, json_mode) -> LLMResult:
        """Deterministic offline response. Good enough to exercise the pipeline."""
        if json_mode:
            text = json.dumps({"stub": True, "echo": prompt[:200]})
        else:
            text = (
                f"[stub:{model}] Offline response. "
                f"Set SKYN3T_OPENROUTER_API_KEY for real generation.\n"
                f"Prompt summary: {prompt[:160]}"
            )
        approx = max(1, len(prompt) // 4)
        return LLMResult(text=text, model=model, backend="stub",
                         prompt_tokens=approx, completion_tokens=len(text) // 4, cost_usd=0.0)
