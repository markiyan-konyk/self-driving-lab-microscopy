"""Token-usage and cost accounting for the LLM calls.

Every LLM response from LangChain carries a standardised ``usage_metadata``
({input_tokens, output_tokens, ...}) across both Anthropic and OpenAI. The meter
below accumulates those, breaks them down by graph node, and multiplies by a
price table to estimate the run's USD cost -- surfaced live on the dashboard and
in the final report.

Prices are USD per 1,000,000 tokens (input, output). The Anthropic values come
from the model table shipped with the claude-api skill; OpenAI values are
best-effort and, like all of them, can drift -- override per run with
PRICE_IN_PER_MTOK / PRICE_OUT_PER_MTOK (env or config) if they're stale.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional

# $/1M tokens: (input, output)
PRICES = {
    # --- Anthropic (from the claude-api skill's model table) ---
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    "claude-sonnet-5":   (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
    "claude-fable-5":    (10.0, 50.0),
    # --- OpenAI GPT-5.6 family (best-effort; override if stale) ---
    "gpt-5.6-sol":       (5.0, 30.0),
    "gpt-5.6-terra":     (3.0, 15.0),
    "gpt-5.6-luna":      (1.0, 5.0),
}


def price_for(model: str):
    """Return ((in_$/Mtok, out_$/Mtok), priced?) for a model id."""
    if model in PRICES:
        return PRICES[model], True
    # try a prefix match (handles date-suffixed variants defensively)
    for key, val in PRICES.items():
        if model and model.startswith(key):
            return val, True
    return (0.0, 0.0), False


def extract_tokens(msg):
    """Pull (input_tokens, output_tokens) out of a LangChain response message."""
    um = getattr(msg, "usage_metadata", None)
    if um:
        return int(um.get("input_tokens", 0) or 0), int(um.get("output_tokens", 0) or 0)
    rm = getattr(msg, "response_metadata", {}) or {}
    u = rm.get("usage") or rm.get("token_usage") or {}
    it = u.get("input_tokens") or u.get("prompt_tokens") or 0
    ot = u.get("output_tokens") or u.get("completion_tokens") or 0
    return int(it or 0), int(ot or 0)


@dataclass
class UsageMeter:
    model: str = "offline"
    price_in: Optional[float] = None    # override $/Mtok in
    price_out: Optional[float] = None   # override $/Mtok out
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    by_node: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _unit_prices(self):
        (pin, pout), priced = price_for(self.model)
        if self.price_in is not None:
            pin, priced = self.price_in, True
        if self.price_out is not None:
            pout, priced = self.price_out, True
        return pin, pout, priced

    def record(self, msg, node: str = "?"):
        """Add one LLM response's tokens; returns (in, out) for that call."""
        it, ot = extract_tokens(msg)
        with self._lock:
            self.input_tokens += it
            self.output_tokens += ot
            self.calls += 1
            b = self.by_node.setdefault(node, {"in": 0, "out": 0, "calls": 0})
            b["in"] += it
            b["out"] += ot
            b["calls"] += 1
        return it, ot

    def cost_usd(self):
        pin, pout, priced = self._unit_prices()
        if not priced:
            return 0.0, False
        c = self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout
        return c, True

    def snapshot(self):
        pin, pout, priced = self._unit_prices()
        cost, _ = self.cost_usd()
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "calls": self.calls,
            "cost_usd": round(cost, 6),
            "priced": priced,
            "price_in_per_mtok": pin,
            "price_out_per_mtok": pout,
            "by_node": dict(self.by_node),
        }
