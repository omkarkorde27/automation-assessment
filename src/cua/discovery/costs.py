"""What a run cost, and what it would have cost.

Token counts here are **measured** -- they come straight off `usage` in each API
response. Prices are **stated assumptions**: they are published per-model rates
that change, and nothing in the system depends on them being right. They live in
one table so a stale number is a one-line fix rather than a hunt, and every
report that quotes a dollar figure says which rates produced it.

The reason this exists at all is that the two model calls in this system have
completely different shapes. The agent loop re-sends a growing conversation
every turn -- ten turns of a frameset's accessibility dump plus screenshots is
most of a hundred thousand input tokens. The authoring reviewer sends one small
structured prompt, once. Reporting a single total hides that the expensive part
is the loop, and that the cheap part does not need an expensive model.
"""

from __future__ import annotations

from dataclasses import dataclass

# USD per million tokens, (input, output). Published list prices; update here.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

PRICES_NOTE = (
    "USD per million tokens, list price, no caching or batch discount applied. "
    "Token counts are measured; prices are assumptions."
)


def price_for(model: str) -> tuple[float, float] | None:
    if model in PRICES:
        return PRICES[model]
    # Model ids carry dated suffixes; fall back to the longest matching prefix.
    matches = [k for k in PRICES if model.startswith(k.split("-2")[0])]
    return PRICES[max(matches, key=len)] if matches else None


@dataclass
class Spend:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def usd(self) -> float | None:
        rates = price_for(self.model)
        if rates is None:
            return None
        return (self.input_tokens / 1e6) * rates[0] + (self.output_tokens / 1e6) * rates[1]

    def line(self, label: str) -> str:
        cost = self.usd
        money = f"${cost:.4f}" if cost is not None else "  (no price on file)"
        return (f"  {label:<22} {self.model:<28} "
                f"{self.input_tokens:>8,} in / {self.output_tokens:>6,} out   {money}")


@dataclass
class RunCost:
    agent: Spend
    reviewer: Spend | None = None

    @property
    def total_usd(self) -> float | None:
        parts = [s.usd for s in (self.agent, self.reviewer) if s is not None]
        return sum(p for p in parts if p is not None) if parts else None

    def report(self) -> str:
        lines = [self.agent.line("discovery loop")]
        if self.reviewer is not None and (self.reviewer.input_tokens or self.reviewer.output_tokens):
            lines.append(self.reviewer.line("authoring review"))
        total = self.total_usd
        if total is not None:
            lines.append(f"  {'TOTAL':<22} {'':<28} {'':>8}      {'':>6}       ${total:.4f}")
        lines.append(f"  ({PRICES_NOTE})")
        return "\n".join(lines)

    def counterfactual(self, *, reviewer_model: str) -> str:
        """What the same run would have cost with a different reviewer model."""
        if self.reviewer is None or not self.reviewer.input_tokens:
            return ""
        alt = Spend(reviewer_model, self.reviewer.input_tokens, self.reviewer.output_tokens)
        here, there = self.reviewer.usd, alt.usd
        if here is None or there is None:
            return ""
        delta = there - here
        return (f"  same review on {reviewer_model}: ${there:.4f} "
                f"({'+' if delta >= 0 else '-'}${abs(delta):.4f}, "
                f"{there / here:.1f}x)" if here else "")
