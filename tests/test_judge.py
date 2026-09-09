import math
import random

from offby import judge as J

TERMS = {
    "calls": {"value": 200_000, "source": "stated"},
    "input_tokens": {"value": 400, "source": "assumed"},
    "output_tokens": {"value": 250, "source": "assumed"},
    "price": {"in_per_m": 0.06, "out_per_m": 0.24, "source": "oracle"},
}
SIGMA = math.log(900 / 250) / 1.6449  # lognormal: median 250, p95 900 (CV ≈ 0.9) — what real reasoning output looks like


def call(inp=400, out=250, pin=0.06, pout=0.24, status=200):
    return J.Call(inp, out, pin, pout, status=status)


def healthy(rng, n, median=250, p_spike=0.02, cap=4000):
    return [cap if rng.random() < p_spike else min(cap, median * math.exp(SIGMA * rng.gauss(0, 1))) for _ in range(n)]


def first_halt(xs, terms=TERMS, **kw):
    calls = []
    for i, x in enumerate(xs, 1):
        calls.append(call(out=round(x)) if x is not None else J.Call(None, None, None, None, status=429))
        if J.judge(terms, calls, **kw).breached:
            return i
    return None


# ---------- verdict mechanics ----------

def test_no_verdict_before_min_valid_calls():
    v = J.judge(TERMS, [call(out=2150)] * 9)
    assert not v.breached and v.terms["output_tokens"].ratio > 8


def test_mock_demo_still_halts_at_min_calls():
    rng = random.Random(1)
    xs = [2150 * (1 + rng.uniform(-0.05, 0.05)) for _ in range(10)]
    assert first_halt(xs) == 10


def test_constant_ratio_sd_zero_breaches_on_mean():
    v = J.judge(TERMS, [call(out=750)] * 10)  # exactly 3x, no variance → t = +cap
    assert v.breached and v.term == "output_tokens" and v.terms["output_tokens"].t == J.T_CAP
    v = J.judge(TERMS, [call(out=400)] * 10)  # 1.6x, no variance → no breach
    assert not v.breached and v.terms["output_tokens"].t == -J.T_CAP


def test_single_max_tokens_answer_never_halts_alone():
    for n in (10, 15, 22, 30):
        xs = [250.0] * (n - 1) + [4000.0]
        assert first_halt(xs) is None, n


def test_early_spike_then_normal_does_not_halt():
    calls = [call(out=3000)] * 5 + [call()] * 10
    v = J.judge(TERMS, calls)
    assert v.terms["output_tokens"].ratio > 2 and not v.breached


def test_healthy_lognormal_never_false_halts():
    for seed in range(40):
        rng = random.Random(seed)
        assert first_halt(healthy(rng, 200)) is None, seed


def test_sudden_8x_from_call_one_detected_at_min_calls():
    for seed in range(10):
        rng = random.Random(100 + seed)
        h = first_halt(healthy(rng, 60, median=2000))
        assert h is not None and h <= 14, (seed, h)


def test_regime_change_after_long_history_detected_fast():
    rng = random.Random(7)
    xs = healthy(rng, 1000) + healthy(rng, 200, median=2000)
    h = first_halt(xs)
    assert h is not None and h - 1000 <= 40, h  # the old overall-mean anchor needed ~286


def test_bimodal_cost_overrun_detected():
    rng = random.Random(3)
    xs = [min(4000, 250 * (10 if rng.random() < 0.2 else 1) * math.exp(SIGMA * rng.gauss(0, 1))) for _ in range(300)]
    h = first_halt(xs)
    assert h is not None and h <= 200, h


def test_mild_2_5x_overrun_detected_within_60_calls():
    rng = random.Random(11)
    h = first_halt(healthy(rng, 100, median=625))
    assert h is not None and h <= 60, h


def test_drift_to_3x_detected():
    rng = random.Random(5)
    xs = [min(4000, 250 * (1 + 2 * k / 199) * math.exp(SIGMA * rng.gauss(0, 1))) for k in range(200)]
    h = first_halt(xs)
    assert h is not None and h <= 200, h


# ---------- valid-calls accounting ----------

def test_error_rows_do_not_count_toward_min_calls_or_calls_term():
    xs = [None] * 9 + [4000.0]
    assert first_halt(xs) is None
    calls = [J.Call(None, None, None, None, status=401)] * 121 + [call(out=2150)]
    v = J.judge({**TERMS, "calls": {"value": 120, "source": "stated"}}, calls)
    assert not v.breached
    assert v.valid == 1 and v.n == 122 and v.errors == {"401": 121}
    assert v.terms["calls"].observed == 1 and not v.terms["calls"].breached


def test_unmetered_2xx_is_counted_separately():
    calls = [J.Call(None, None, None, None, status=200)] * 3 + [call()] * 2
    v = J.judge(TERMS, calls)
    assert v.unmetered == 3 and v.valid == 2 and v.errors == {}


def test_one_unpriced_call_keeps_projection():
    calls = [call(out=2150)] * 12 + [J.Call(400, 2150, None, None)]
    v = J.judge(TERMS, calls)
    assert v.unpriced_calls == 1 and v.projected_usd is not None
    per_call = (400 * 0.06 + 2150 * 0.24) / 1e6
    assert abs(v.projected_usd - (12 * per_call + (200_000 - 13) * per_call)) < 1e-6


def test_price_term_breaches_when_unit_price_5x():
    v = J.judge(TERMS, [call(pin=0.09, pout=0.40)] * 12)  # super-vs-nano ≈ 1.6x: under threshold
    assert not v.terms["price"].breached
    v = J.judge(TERMS, [call(pin=0.30, pout=1.20)] * 12)
    assert v.breached and v.term == "price" and abs(v.terms["price"].ratio - 5.0) < 1e-6


def test_expected_and_spent():
    v = J.judge(TERMS, [call(out=2150)] * 25)
    assert abs(v.expected_usd - 16.80) < 1e-9
    assert abs(v.spent_usd - 25 * (400 * 0.06 + 2150 * 0.24) / 1e6) < 1e-9


def test_run_since_resets_counting_but_keeps_history():
    old = [call(out=2150)] * 30
    new = [call(out=290)] * 12
    v = J.judge(TERMS, old + new, run_since=30)
    assert v.total == 42 and v.n == 12 and v.valid == 12 and v.judged == 12
    assert abs(v.terms["output_tokens"].observed - 290) < 1e-9 and not v.breached
    assert v.terms["calls"].observed == 12


def test_baseline_terms_use_median_and_skip_errors():
    calls = [J.Call(None, None, None, None, status=429)] + [call(inp=380, out=260)] * 9 + [call(inp=380, out=9000)]
    t = J.baseline_terms(calls, model="m")
    assert t["input_tokens"]["value"] == 380 and t["output_tokens"]["value"] == 260
    assert t["output_tokens"]["source"] == "baseline" and t["price"]["in_per_m"] == 0.06
    v = J.judge(t, [call(inp=380, out=260)] * 10 + [call(inp=380, out=900)] * 20)
    assert v.breached and v.term == "output_tokens"


def test_suggest_accept_uses_window_mean_not_last5():
    tv = J.TermVerdict("output_tokens", 250, observed=2137, recent=3900, ratio=8.5, ratio_recent=15.6, breached=True)
    assert J.suggest_accept(tv) == 2150
    assert J.suggest_accept(J.TermVerdict("price", 1.0, observed=1.66, recent=1.7)) == 1.66
