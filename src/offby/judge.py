"""The referee. Pure arithmetic — no model calls, no I/O.

Per-call terms (output_tokens, input_tokens, price) breach only when BOTH the
overall mean and the last-5 mean exceed forecast × threshold, and only after
min_calls. A single long answer never halts a job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import fmean

MIN_CALLS = 10
THRESHOLD = 2.0
RECENT = 5


@dataclass
class Call:
    prompt_tokens: int | None
    completion_tokens: int | None
    in_per_m: float | None  # unit price of the model actually observed, $/M tokens
    out_per_m: float | None

    @property
    def cost_usd(self) -> float | None:
        if None in (self.prompt_tokens, self.completion_tokens, self.in_per_m, self.out_per_m):
            return None
        return (self.prompt_tokens * self.in_per_m + self.completion_tokens * self.out_per_m) / 1e6


@dataclass
class TermVerdict:
    term: str
    forecast: float | None
    observed: float | None = None  # overall mean (price: mean cost ratio)
    recent: float | None = None  # last-5 mean
    ratio: float | None = None
    ratio_recent: float | None = None
    breached: bool = False


@dataclass
class Verdict:
    n: int  # calls in the current run (since the last `job ensure`)
    judged: int  # calls since the last resume — what the per-call terms are judged on
    min_calls: int
    threshold: float
    total: int = 0  # every call ever recorded under this job name
    terms: dict[str, TermVerdict] = field(default_factory=dict)
    spent_usd: float = 0.0
    unpriced_calls: int = 0
    expected_usd: float | None = None  # forecast total
    projected_usd: float | None = None  # total at the observed per-call rate
    breached: bool = False
    term: str | None = None
    ratio: float | None = None


def term_value(terms: dict, name: str):
    t = terms.get(name)
    return None if not isinstance(t, dict) else t.get("value")


def price_terms(terms: dict) -> tuple[float | None, float | None]:
    p = terms.get("price") or {}
    return p.get("in_per_m"), p.get("out_per_m")


def forecast_total(terms: dict) -> float | None:
    calls, tin, tout = (term_value(terms, k) for k in ("calls", "input_tokens", "output_tokens"))
    in_pm, out_pm = price_terms(terms)
    if None in (calls, tin, tout, in_pm, out_pm):
        return None
    return calls * (tin * in_pm + tout * out_pm) / 1e6


def _series(name: str, forecast: float | None, series: list[float | None], n: int, min_calls: int, threshold: float) -> TermVerdict:
    vals = [v for v in series if v is not None]
    if not vals:
        return TermVerdict(name, forecast)
    obs, rec = fmean(vals), fmean(vals[-RECENT:])
    if not forecast:
        return TermVerdict(name, forecast, obs, rec)
    ratio, ratio_recent = obs / forecast, rec / forecast
    breached = n >= min_calls and ratio > threshold and ratio_recent > threshold
    return TermVerdict(name, forecast, obs, rec, ratio, ratio_recent, breached)


def judge(terms: dict, calls: list[Call], *, min_calls: int = MIN_CALLS, threshold: float = THRESHOLD,
          since: int = 0, run_since: int = 0) -> Verdict:
    """`run_since`: first call of the current run (set by `job ensure`) — spend, projection and the
    calls term count from here. `since`: first call to judge per-call terms on (set by resume/accept)."""
    total = len(calls)
    run = calls[run_since:]
    n = len(run)
    window = calls[max(since, run_since):]
    m = len(window)
    costs = [c.cost_usd for c in run]
    v = Verdict(n=n, judged=m, total=total, min_calls=min_calls, threshold=threshold)
    v.spent_usd = sum(c for c in costs if c is not None)
    v.unpriced_calls = sum(1 for c in costs if c is None)
    v.expected_usd = forecast_total(terms)

    fc_calls = term_value(terms, "calls")
    fc_in, fc_out = term_value(terms, "input_tokens"), term_value(terms, "output_tokens")
    fc_in_pm, fc_out_pm = price_terms(terms)

    if fc_calls and n > 0 and v.unpriced_calls == 0:
        # rate = cost per call observed in the judged window (since the last resume), else whole job
        wcosts = [c.cost_usd for c in window if c.cost_usd is not None]
        rate = (sum(wcosts) / len(wcosts)) if wcosts else (v.spent_usd / n)
        v.projected_usd = v.spent_usd + max(0, fc_calls - n) * rate

    v.terms["output_tokens"] = _series("output_tokens", fc_out, [c.completion_tokens for c in window], m, min_calls, threshold)
    v.terms["input_tokens"] = _series("input_tokens", fc_in, [c.prompt_tokens for c in window], m, min_calls, threshold)

    # price: cost of the observed tokens at the observed model's price vs. at the forecast price
    if fc_in_pm is not None and fc_out_pm is not None:
        ratios: list[float | None] = []
        for c in window:
            if c.cost_usd is None:
                ratios.append(None)
                continue
            fc_cost = (c.prompt_tokens * fc_in_pm + c.completion_tokens * fc_out_pm) / 1e6
            ratios.append(c.cost_usd / fc_cost if fc_cost > 0 else None)
        v.terms["price"] = _series("price", 1.0, ratios, m, min_calls, threshold)
    else:
        v.terms["price"] = TermVerdict("price", None)

    if fc_calls:
        r = n / fc_calls
        v.terms["calls"] = TermVerdict("calls", fc_calls, n, n, r, r, breached=r > threshold)
    else:
        v.terms["calls"] = TermVerdict("calls", None, n, n)

    breached = [t for t in v.terms.values() if t.breached]
    if breached:
        worst = max(breached, key=lambda t: t.ratio or 0)
        v.breached, v.term, v.ratio = True, worst.term, worst.ratio
    return v


def baseline_terms(calls: list[Call], model: str | None = None) -> dict:
    """Terms derived from the first N calls (baseline mode): 'what you got' becomes the forecast."""
    ins = [c.prompt_tokens for c in calls if c.prompt_tokens is not None]
    outs = [c.completion_tokens for c in calls if c.completion_tokens is not None]
    terms: dict = {}
    if ins:
        terms["input_tokens"] = {"value": round(fmean(ins)), "source": "baseline"}
    if outs:
        terms["output_tokens"] = {"value": round(fmean(outs)), "source": "baseline"}
    priced = next((c for c in calls if c.in_per_m is not None), None)
    if priced:
        terms["price"] = {"in_per_m": priced.in_per_m, "out_per_m": priced.out_per_m, "source": "baseline", "model": model}
    return terms


def suggest_accept(tv: TermVerdict) -> float | None:
    """A value that would make the observed rate pass: last-5 mean rounded up."""
    if tv.recent is None:
        return None
    if tv.term == "price":
        return round(tv.recent, 3)
    step = 50 if tv.recent >= 200 else 10
    return int(math.ceil(tv.recent / step) * step)
