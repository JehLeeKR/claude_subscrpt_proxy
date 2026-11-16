"""Token accounting: read `usage` out of responses as they stream past, and price it.

Subscription seats have no per-token cost, but their usage is still counted
(and priced at API rates as "API-equivalent value") so seats and paid backends
can be compared. Paid backends' cost feeds the router's daily budget.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

# USD per million tokens: (input, output, cache_read). Cache writes are priced at
# 1.25x input (5-minute TTL) or 2x input (1-hour TTL).
# First-party API list prices; Bedrock and LiteLLM-routed providers may differ.
# Override with `--prices prices.json` ({"model-prefix": [in, out, cache_read]}).
DEFAULT_PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 1.00),
    "claude-mythos-5-1": (10.0, 50.0, 1.00),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4": (1.0, 5.0, 0.10),
}
FALLBACK_PRICE = DEFAULT_PRICES["claude-opus-5-5"]


def normalize_model(model: str) -> str:
    m = model.lower()
    # Bedrock ids: "anthropic.claude-…", "us.anthropic.claude-…", "…-v1:0"
    if "anthropic." in m:
        m = m.split("anthropic.", 1)[1]
    m = m.split("[", 1)[0]
    for suffix in ("-v1:0", "-v1", ":0"):
        if m.endswith(suffix):
            m = m[: -len(suffix)]
    return m


def price_for(model: str, prices: dict[str, tuple[float, float, float]]) -> tuple[tuple[float, float, float], bool]:
    m = normalize_model(model)
    best = max((p for p in prices if m.startswith(p)), key=len, default=None)
    return (prices[best], True) if best else (FALLBACK_PRICE, False)


@dataclass
class Tokens:
    requests: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cost_usd: float = 0.0
    unpriced: bool = False

    def add(self, o: "Tokens") -> None:
        for f in ("requests", "input", "output", "cache_read", "cache_write_5m", "cache_write_1h"):
            setattr(self, f, getattr(self, f) + getattr(o, f))
        self.cost_usd += o.cost_usd
        self.unpriced = self.unpriced or o.unpriced

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["cost_usd"] = round(self.cost_usd, 6)
        return d


class UsageMeter:
    """Fed the raw response bytes; extracts model and usage from SSE or JSON."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.usage: dict = {}
        self._buf = b""
        self._json = b""
        self._sse: bool | None = None

    def feed(self, chunk: bytes) -> None:
        if self._sse is None:
            head = chunk.lstrip()[:6]
            self._sse = head.startswith(b"event:") or head.startswith(b"data:")
        if not self._sse:
            if len(self._json) < 4 * 1024 * 1024:
                self._json += chunk
            return
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.startswith(b"data:"):
                self._event(line[5:].strip())

    def _event(self, raw: bytes) -> None:
        try:
            ev = json.loads(raw)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        t = ev.get("type")
        if t == "message_start":
            msg = ev.get("message") or {}
            self.model = msg.get("model") or self.model
            self.usage.update(msg.get("usage") or {})
        elif t == "message_delta":
            # message_delta usage is cumulative for the fields it carries.
            self.usage.update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})

    def finish(self) -> None:
        if self._sse is False and self._json:
            try:
                msg = json.loads(self._json)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("type") == "message":
                self.model = msg.get("model")
                self.usage = msg.get("usage") or {}

    def tokens(self, prices: dict, fallback_model: str | None = None) -> Tokens | None:
        if not self.usage:
            return None
        u = self.usage
        cc = u.get("cache_creation") or {}
        w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
        w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
        total_w = int(u.get("cache_creation_input_tokens") or 0)
        if not cc:
            w5m = total_w
        t = Tokens(
            requests=1,
            input=int(u.get("input_tokens") or 0),
            output=int(u.get("output_tokens") or 0),
            cache_read=int(u.get("cache_read_input_tokens") or 0),
            cache_write_5m=w5m,
            cache_write_1h=w1h,
        )
        (pin, pout, pcr), known = price_for(self.model or fallback_model or "", prices)
        t.unpriced = not known
        t.cost_usd = (
            t.input * pin + t.output * pout + t.cache_read * pcr
            + t.cache_write_5m * pin * 1.25 + t.cache_write_1h * pin * 2.0
        ) / 1e6
        return t


@dataclass
class Ledger:
    """Per-UTC-day, per-model token totals. In memory; resets with the process."""

    prices: dict = field(default_factory=lambda: dict(DEFAULT_PRICES))
    days: dict[str, dict[str, Tokens]] = field(default_factory=dict)
    keep_days: int = 31

    @staticmethod
    def today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def record(self, meter: UsageMeter, request_model: str | None) -> Tokens | None:
        meter.finish()
        t = meter.tokens(self.prices, request_model)
        if t is None:
            return None
        model = normalize_model(meter.model or request_model or "unknown")
        day = self.days.setdefault(self.today(), {})
        day.setdefault(model, Tokens()).add(t)
        for old in sorted(self.days)[: -self.keep_days]:
            del self.days[old]
        return t

    def summary(self, day: str | None = None) -> dict:
        def roll(models: dict[str, Tokens]) -> dict:
            tot = Tokens()
            for t in models.values():
                tot.add(t)
            return {"total": tot.to_dict(), "by_model": {m: t.to_dict() for m, t in models.items()}}

        all_models: dict[str, Tokens] = {}
        for models in self.days.values():
            for m, t in models.items():
                all_models.setdefault(m, Tokens()).add(t)
        return {
            "today": roll(self.days.get(day or self.today(), {})),
            "all": roll(all_models),
            "since": min(self.days) if self.days else None,
        }
