"""The referee. Pure arithmetic — no model calls, no I/O.

A per-call term (output_tokens, input_tokens, price) breaches when the mean ratio
observed/forecast is over the threshold WITH CONFIDENCE: a one-sided t statistic
(mean − T) / (sd / √k) above Z on the whole judged window OR on the last 30 valid
calls. Only calls that actually carried usage count; errors and usage-less
responses are tallied separately and never feed a mean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import fmean, median, stdev

MIN_CALLS = 10       # valid calls before any verdict
THRESHOLD = 2.0      # observed / forecast
Z = 2.5              # confidence: one-sided t on the mean ratio
WINDOW = 30          # second test window: the last N valid calls
RECENT = 5           # display only
T_CAP = 99.0         # sd == 0 → ±T_CAP instead of ±inf (JSON-safe)


@dataclass
class Call:
    prompt_tokens: int | None
    completion_tokens: int | None
    in_per_m: float | None  # unit price of the model actually observed, $/M tokens
    out_per_m: float | None
    status: int = 200
    estimated: bool = False  # usage was missing; tokens estimated from streamed content
    price_source: str | None = None  # oracle | forecast | gateway — None when unpriced

    @property
    def ok(self) -> bool:
        return self.status < 400

    @property
    def metered(self) -> bool:
        return self.ok and (self.prompt_tokens is not None or self.completion_tokens is not None)

    @property
    def cost_usd(self) -> float | None:
        if not self.metered or self.in_per_m is None or self.out_per_m is None:
            return None
        return ((self.prompt_tokens or 0) * self.in_per_m + (self.completion_tokens or 0) * self.out_per_m) / 1e6


@dataclass
class TermVerdict:
    term: str
    forecast: float | None
    observed: float | None = None  # mean over the judged window (price: mean cost ratio)
    recent: float | None = None  # last-5 mean, display only
    ratio: float | None = None
    ratio_recent: float | None = None
    breached: bool = False
    t: float | None = None  # the t statistic that decided (max over the two windows)
    n_valid: int = 0


@dataclass
class Verdict:
    n: int  # calls in the current run (since the last `job ensure`), every status
    valid: int  # run calls that returned 2xx with a usage object
    judged: int  # valid calls since the last resume — what the per-call terms are judged on
    min_calls: int
    threshold: float
    total: int = 0  # every call ever recorded under this job name
    terms: dict[str, TermVerdict] = field(default_factory=dict)
    spent_usd: float = 0.0
    unpriced_calls: int = 0
    forecast_priced: int = 0  # calls priced at the forecast unit price because the upstream has no price list
    errors: dict[str, int] = field(default_factory=dict)  # status → count, this run
    unmetered: int = 0  # 2xx responses without usage, this run
    estimated: int = 0  # calls whose tokens were estimated, this run
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


def tstat(ratios: list[float], threshold: float) -> float | None:
    """One-sided t of the mean ratio against the threshold. sd == 0 → ±T_CAP."""
    k = len(ratios)
    if k < 2:
        return None
    m = fmean(ratios)
    sd = stdev(ratios)
    if sd == 0:
        return T_CAP if m > threshold else -T_CAP
    return max(-T_CAP, min(T_CAP, (m - threshold) / (sd / math.sqrt(k))))


def _series(name: str, forecast: float | None, series: list[float | None], min_calls: int, threshold: float,
            z: float = Z) -> TermVerdict:
    vals = [v for v in series if v is not None]
    k = len(vals)
    if not vals:
        return TermVerdict(name, forecast)
    obs, rec = fmean(vals), fmean(vals[-RECENT:])
    if not forecast:
        return TermVerdict(name, forecast, obs, rec, n_valid=k)
    ratio, ratio_recent = obs / forecast, rec / forecast
    tv = TermVerdict(name, forecast, obs, rec, ratio, ratio_recent, n_valid=k)
    if k < min_calls:
        return tv
    ratios = [v / forecast for v in vals]
    best: float | None = None
    for w in (ratios, ratios[-WINDOW:]):
        if len(w) < min_calls:
            continue
        t = tstat(w, threshold)
        if t is None:
            continue
        best = t if best is None else max(best, t)
        if t > z:
            tv.breached = True
            break
    tv.t = best
    return tv


def judge(terms: dict, calls: list[Call], *, min_calls: int = MIN_CALLS, threshold: float = THRESHOLD,
          since: int = 0, run_since: int = 0, z: float = Z) -> Verdict:
    """`run_since`: first call of the current run (set by `job ensure`) — spend, projection, the
    calls term and the error tallies count from here. `since`: first call to judge per-call terms
    on (set by resume/accept)."""
    total = len(calls)
    run = calls[run_since:]
    window = [c for c in calls[max(since, run_since):] if c.metered]
    valid_run = [c for c in run if c.metered]
    v = Verdict(n=len(run), valid=len(valid_run), judged=len(window), total=total,
                min_calls=min_calls, threshold=threshold)
    for c in run:
        if not c.ok:
            v.errors[str(c.status)] = v.errors.get(str(c.status), 0) + 1
        elif not c.metered:
            v.unmetered += 1
        if c.estimated:
            v.estimated += 1
    costs = [c.cost_usd for c in valid_run]
    v.spent_usd = sum(c for c in costs if c is not None)
    v.unpriced_calls = sum(1 for c in costs if c is None)
    v.forecast_priced = sum(1 for c in valid_run if c.price_source == "forecast")
    v.expected_usd = forecast_total(terms)

    fc_calls = term_value(terms, "calls")
    fc_in, fc_out = term_value(terms, "input_tokens"), term_value(terms, "output_tokens")
    fc_in_pm, fc_out_pm = price_terms(terms)

    if fc_calls and valid_run:
        # remaining calls count from the last resume (a rerun after `accept` starts the file over);
        # rate = cost per priced call in the judged window, else the whole run
        priced = [c.cost_usd for c in window if c.cost_usd is not None] or [c for c in costs if c is not None]
        if priced:
            v.projected_usd = v.spent_usd + max(0, fc_calls - len(window)) * fmean(priced)

    v.terms["output_tokens"] = _series("output_tokens", fc_out, [c.completion_tokens for c in window], min_calls, threshold, z)
    v.terms["input_tokens"] = _series("input_tokens", fc_in, [c.prompt_tokens for c in window], min_calls, threshold, z)

    # price: cost of the observed tokens at the observed model's price vs. at the forecast price.
    # Only calls the oracle/gateway actually priced can say anything; forecast-priced calls are 1.0 by construction.
    if fc_in_pm is not None and fc_out_pm is not None:
        ratios: list[float | None] = []
        for c in window:
            if c.cost_usd is None or c.price_source == "forecast":
                ratios.append(None)
                continue
            fc_cost = ((c.prompt_tokens or 0) * fc_in_pm + (c.completion_tokens or 0) * fc_out_pm) / 1e6
            ratios.append(c.cost_usd / fc_cost if fc_cost > 0 else None)
        v.terms["price"] = _series("price", 1.0, ratios, min_calls, threshold, z)
    else:
        v.terms["price"] = TermVerdict("price", None)

    nv = len(window)  # calls since the last resume: a full rerun after `accept` must not double-count
    if fc_calls:
        r = nv / fc_calls
        v.terms["calls"] = TermVerdict("calls", fc_calls, nv, nv, r, r, breached=r > threshold, n_valid=nv)
    else:
        v.terms["calls"] = TermVerdict("calls", None, nv, nv, n_valid=nv)

    breached = [t for t in v.terms.values() if t.breached]
    if breached:
        worst = max(breached, key=lambda t: t.ratio or 0)
        v.breached, v.term, v.ratio = True, worst.term, worst.ratio
    return v


def baseline_terms(calls: list[Call], model: str | None = None) -> dict:
    """Terms derived from the first N valid calls (baseline mode): the median of what you got
    becomes the forecast. Median, not mean — one long first answer must not set the bar."""
    valid = [c for c in calls if c.metered]
    ins = [c.prompt_tokens for c in valid if c.prompt_tokens is not None]
    outs = [c.completion_tokens for c in valid if c.completion_tokens is not None]
    terms: dict = {}
    if ins:
        terms["input_tokens"] = {"value": round(median(ins)), "source": "baseline"}
    if outs:
        terms["output_tokens"] = {"value": round(median(outs)), "source": "baseline"}
    priced = next((c for c in valid if c.in_per_m is not None), None)
    if priced:
        terms["price"] = {"in_per_m": priced.in_per_m, "out_per_m": priced.out_per_m, "source": "baseline", "model": model}
    return terms


def suggest_accept(tv: TermVerdict) -> float | None:
    """A value that would make the observed rate pass: the judged-window mean, rounded up."""
    if tv.observed is None:
        return None
    if tv.term == "price":
        return round(tv.observed, 3)
    step = 50 if tv.observed >= 200 else 10
    return int(math.ceil(tv.observed / step) * step)


__all__ = ["Call", "TermVerdict", "Verdict", "judge", "baseline_terms", "suggest_accept", "tstat",
           "term_value", "price_terms", "forecast_total", "MIN_CALLS", "THRESHOLD", "Z", "WINDOW", "RECENT"]
