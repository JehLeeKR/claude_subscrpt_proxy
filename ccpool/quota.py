"""Quota state read from upstream response headers, and the score used to pick an account.

Responses to subscription (OAuth) requests carry "unified" rate-limit headers.
They are not documented, so parsing is lenient: a missing or odd value is
ignored rather than treated as an error.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Mapping

PREFIX = "anthropic-ratelimit-unified-"
WINDOW_5H = 5 * 3600
WINDOW_7D = 7 * 24 * 3600


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _util(v: str | None) -> float | None:
    x = _num(v)
    if x is None:
        return None
    if x > 1.0:  # tolerate a percentage
        x /= 100.0
    return max(0.0, min(1.0, x))


@dataclass
class Quota:
    util_5h: float = 0.0
    reset_5h: float | None = None  # unix seconds
    util_7d: float = 0.0
    reset_7d: float | None = None
    status: str = "unknown"  # allowed | allowed_warning | rejected | unknown
    cooldown_until: float = 0.0  # set after a 429
    updated_at: float = 0.0
    errors: int = 0  # consecutive transport/auth failures
    extra: dict = field(default_factory=dict)

    # updating --------------------------------------------------------------

    def update_from_headers(self, headers: Mapping[str, str], status_code: int, now: float | None = None) -> bool:
        """Apply headers from one response. Returns True if any quota header was present."""
        now = now or time.time()
        h = {k.lower(): v for k, v in headers.items()}
        seen = False

        for key, attr in (("5h-utilization", "util_5h"), ("7d-utilization", "util_7d")):
            u = _util(h.get(PREFIX + key))
            if u is not None:
                setattr(self, attr, u)
                seen = True
        for key, attr in (("5h-reset", "reset_5h"), ("7d-reset", "reset_7d")):
            r = _num(h.get(PREFIX + key))
            if r is not None:
                setattr(self, attr, r)
                seen = True
        st = h.get(PREFIX + "status")
        if st:
            self.status = st
            seen = True
        for k, v in h.items():
            if k.startswith(PREFIX) and k[len(PREFIX):] not in (
                "5h-utilization", "7d-utilization", "5h-reset", "7d-reset", "status",
            ):
                self.extra[k[len(PREFIX):]] = v

        if status_code == 429 or self.status == "rejected":
            self.cooldown_until = max(self.cooldown_until, self._cooldown_end(h, now))
        elif 200 <= status_code < 300:
            self.errors = 0

        if seen:
            self.updated_at = now
        return seen

    def _cooldown_end(self, h: dict, now: float) -> float:
        ra = _num(h.get("retry-after"))
        if ra is not None:
            return now + ra
        r = _num(h.get(PREFIX + "reset"))
        if r is not None and r > now:
            return r
        # Use the reset of whichever window is exhausted.
        candidates = []
        if self.util_5h >= 0.999 and self.reset_5h:
            candidates.append(self.reset_5h)
        if self.util_7d >= 0.999 and self.reset_7d:
            candidates.append(self.reset_7d)
        future = [c for c in candidates if c > now]
        return max(future) if future else now + 60

    # decisions -------------------------------------------------------------

    def available(self, now: float | None = None) -> bool:
        now = now or time.time()
        return self.cooldown_until <= now and self.errors < 3

    def max_util(self, now: float | None = None) -> float:
        return max(self._eff_5h(now), self._eff_7d(now))

    def _eff_5h(self, now: float | None) -> float:
        now = now or time.time()
        # A window that has already reset is empty again, even before new headers arrive.
        return 0.0 if self.reset_5h and self.reset_5h <= now else self.util_5h

    def _eff_7d(self, now: float | None) -> float:
        now = now or time.time()
        return 0.0 if self.reset_7d and self.reset_7d <= now else self.util_7d

    def score(self, now: float | None = None, *, expiring_weight: float = 0.3) -> float:
        """Higher is better. Unavailable accounts score -inf.

        headroom: how much of the tighter window is left.
        expiring: unused 5h quota that will be lost soon. Spending it first
                  means quota that would expire anyway is used before quota
                  that would carry over.
        """
        now = now or time.time()
        if not self.available(now):
            return float("-inf")
        u5, u7 = self._eff_5h(now), self._eff_7d(now)
        headroom = 1.0 - max(u5, u7)
        expiring = 0.0
        if self.reset_5h and self.reset_5h > now:
            remaining_frac = min(1.0, (self.reset_5h - now) / WINDOW_5H)
            # Limited by 7d headroom: 5h quota is worthless if the week is spent.
            expiring = min(1.0 - u5, 1.0 - u7) * (1.0 - remaining_frac)
        return headroom + expiring_weight * expiring

    def to_dict(self) -> dict:
        d = asdict(self)
        now = time.time()
        d["available"] = self.available(now)
        d["score"] = self.score(now) if self.available(now) else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Quota":
        names = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in names})
