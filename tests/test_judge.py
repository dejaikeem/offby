from offby import judge as J

TERMS = {
    "calls": {"value": 200_000, "source": "stated"},
    "input_tokens": {"value": 400, "source": "assumed"},
    "output_tokens": {"value": 250, "source": "assumed"},
    "price": {"in_per_m": 0.06, "out_per_m": 0.24, "source": "oracle"},
}


def call(inp=400, out=250, pin=0.06, pout=0.24):
    return J.Call(inp, out, pin, pout)


def test_no_verdict_before_min_calls():
    v = J.judge(TERMS, [call(out=2150)] * 9)
    assert not v.breached
    assert v.terms["output_tokens"].ratio > 8


def test_breach_when_both_means_exceed():
    v = J.judge(TERMS, [call(out=2150)] * 10)
    assert v.breached and v.term == "output_tokens"
    assert 8.5 < v.ratio < 8.7
    assert v.terms["input_tokens"].breached is False
    assert v.terms["price"].breached is False


def test_one_long_answer_does_not_halt_once_there_is_history():
    calls = [call()] * 40 + [call(out=9000)]  # recent mean 8x, but overall mean (250*40+9000)/41 ≈ 1.85x
    v = J.judge(TERMS, calls)
    assert v.terms["output_tokens"].ratio_recent > 2
    assert not v.breached


def test_early_spike_then_normal_does_not_halt():
    calls = [call(out=3000)] * 5 + [call()] * 10  # overall mean high, recent normal
    v = J.judge(TERMS, calls)
    assert v.terms["output_tokens"].ratio > 2
    assert v.terms["output_tokens"].ratio_recent == 1.0
    assert not v.breached


def test_price_term_breaches_when_model_swapped():
    # same tokens, but billed at super's price (0.09/0.40): ratio ≈ (400*.09+250*.40)/(400*.06+250*.24) = 136/84 ≈ 1.62 → not 2x
    v = J.judge(TERMS, [call(pin=0.09, pout=0.40)] * 12)
    assert not v.terms["price"].breached
    v = J.judge(TERMS, [call(pin=0.30, pout=1.20)] * 12)  # 5x price
    assert v.breached and v.term == "price"
    assert abs(v.terms["price"].ratio - 5.0) < 1e-6


def test_projection_and_expected():
    v = J.judge(TERMS, [call(out=2150)] * 25)
    assert abs(v.expected_usd - 200_000 * (400 * 0.06 + 250 * 0.24) / 1e6) < 1e-9  # $16.80
    per_call = (400 * 0.06 + 2150 * 0.24) / 1e6
    assert abs(v.spent_usd - 25 * per_call) < 1e-9
    assert abs(v.projected_usd - 200_000 * per_call) < 1e-6  # ≈ $108


def test_unpriced_calls_block_projection_but_not_verdict():
    v = J.judge(TERMS, [J.Call(400, 2150, None, None)] * 12)
    assert v.unpriced_calls == 12 and v.projected_usd is None
    assert v.breached and v.term == "output_tokens"


def test_baseline_terms():
    t = J.baseline_terms([call(inp=380, out=260)] * 10, model="m")
    assert t["input_tokens"]["value"] == 380 and t["output_tokens"]["source"] == "baseline"
    assert t["price"]["in_per_m"] == 0.06
    v = J.judge(t, [call(inp=380, out=260)] * 10 + [call(inp=380, out=900)] * 10)
    assert v.breached and v.term == "output_tokens"


def test_suggest_accept_rounds_up():
    tv = J.TermVerdict("output_tokens", 250, 2137, 2140, 8.5, 8.6, True)
    assert J.suggest_accept(tv) == 2150


def test_run_since_resets_counting_but_keeps_history():
    old = [call(out=2150)] * 30          # yesterday's run, overran
    new = [call(out=290)] * 12           # today's run after the fix
    v = J.judge(TERMS, old + new, run_since=30)
    assert v.total == 42 and v.n == 12 and v.judged == 12
    assert abs(v.terms["output_tokens"].observed - 290) < 1e-9
    assert not v.breached
    assert v.terms["calls"].observed == 12
    per_call = (400 * 0.06 + 290 * 0.24) / 1e6
    assert abs(v.spent_usd - 12 * per_call) < 1e-9
