import httpx
import pytest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from offby.mock import PROFILES, create_mock
from offby.proxy import create_app
from offby.store import Store

pytestmark = pytest.mark.anyio

TERMS = {
    "calls": {"value": 200_000, "source": "stated"},
    "input_tokens": {"value": 400, "source": "assumed"},
    "output_tokens": {"value": 250, "source": "assumed"},
    "model": {"value": "mock/nemotron-3-nano-30b-a3b", "source": "stated"},
    "price": {"in_per_m": 0.06, "out_per_m": 0.24, "source": "oracle"},
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def stack(tmp_path):
    mock = create_mock(seed=1)
    app = create_app("http://mock/v1", db_path=tmp_path / "t.sqlite", transport=httpx.ASGITransport(app=mock))
    return app, app.state.store


def chat(stream=False, think=True):
    body = {"model": "mock/nemotron-3-nano-30b-a3b", "messages": [{"role": "user", "content": "classify: great battery"}]}
    if stream:
        body["stream"] = True
    if not think:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return body


async def test_unknown_job_is_404(stack):
    app, _ = stack
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        r = await c.post("/j/nope/v1/chat/completions", json=chat())
    assert r.status_code == 404 and r.json()["error"]["type"] == "offby_unknown_job"


async def test_halts_after_ten_calls_with_named_term(stack):
    app, store = stack
    store.create_job("j_test", TERMS, budget_usd=20)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        codes = []
        for _ in range(14):
            r = await c.post("/j/j_test/v1/chat/completions", json=chat())
            codes.append(r.status_code)
        assert codes[:10] == [200] * 10          # judged from the 10th; the 10th itself is still returned
        assert codes[10] == 402                  # the 11th request is refused
        assert r.status_code == 402
        assert r.headers["x-offby-halt"] == "output_tokens"
        err = r.json()["error"]
        assert err["type"] == "offby_term_breach" and err["term"] == "output_tokens"
        assert 8 < err["ratio"] < 9.5
        assert err["resume"].startswith("offby accept j_test output_tokens=")
        assert "projected $" in err["message"]

        rep = (await c.get("/j/j_test/report")).json()
        assert rep["state"] == "halted" and rep["verdict"]["n"] == 10
        diag = (await c.get("/j/j_test/diagnosis")).json()
        assert diag["status"] in ("pending", "unavailable")  # no API key in tests
        assert diag["evidence"]["reasoning_share"] > 0.8


async def test_accept_resumes_and_think_off_passes(stack):
    app, store = stack
    store.create_job("j_a", TERMS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        for _ in range(11):
            r = await c.post("/j/j_a/v1/chat/completions", json=chat())
        assert r.status_code == 402
        store.resume("j_a")  # `offby accept j_a` with no terms: cause fixed, judge again from the next call
        for _ in range(12):
            r = await c.post("/j/j_a/v1/chat/completions", json=chat(think=False))  # ≈290 output
        assert r.status_code == 200
        rep = (await c.get("/j/j_a/report")).json()
        assert rep["state"] == "running" and rep["verdict"]["n"] == 22 and rep["verdict"]["judged"] == 12
        assert rep["verdict"]["per_term"]["output_tokens"]["ratio"] < 1.5

        # accepting the observed number is the other way out
        store.create_job("j_c", TERMS)
        for _ in range(11):
            r = await c.post("/j/j_c/v1/chat/completions", json=chat())
        assert r.status_code == 402
        terms = dict(TERMS); terms["output_tokens"] = {"value": 2200, "source": "accepted"}
        store.resume("j_c", terms)
        for _ in range(12):
            r = await c.post("/j/j_c/v1/chat/completions", json=chat())  # still ≈2150, now within forecast
        assert r.status_code == 200


async def test_streaming_is_metered(stack):
    app, store = stack
    store.create_job("j_s", TERMS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        r = await c.post("/j/j_s/v1/chat/completions", json=chat(stream=True))
        assert r.status_code == 200 and "data:" in r.text
    rows = store.calls("j_s")
    assert len(rows) == 1 and rows[0]["completion_tokens"] > 1000 and rows[0]["reasoning_tokens"] > 0


async def test_baseline_mode_sets_terms_then_judges(stack):
    app, store = stack
    store.create_job("j_b", {}, baseline_n=5, min_calls=5)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        for _ in range(5):
            await c.post("/j/j_b/v1/chat/completions", json=chat(think=False))  # baseline ≈290 output
        assert store.get_job("j_b")["terms"]["output_tokens"]["source"] == "baseline"
        for i in range(25):
            r = await c.post("/j/j_b/v1/chat/completions", json=chat(think=True))  # now ≈2150
            if r.status_code == 402:
                break
        assert r.status_code == 402 and r.headers["x-offby-halt"] == "output_tokens" and i < 20


async def test_stream_without_usage_chunk_is_estimated_and_still_halts(tmp_path):
    mock = create_mock(seed=2, ignore_include_usage=True, reasoning_report="reasoning-field")
    app = create_app("http://mock/v1", db_path=tmp_path / "s.sqlite", transport=httpx.ASGITransport(app=mock))
    app.state.store.create_job("j_est", TERMS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        for i in range(20):
            r = await c.post("/j/j_est/v1/chat/completions", json=chat(stream=True))
            if r.status_code == 402:
                break
        assert r.status_code == 402 and r.headers["x-offby-halt"] == "output_tokens"
        rep = (await c.get("/j/j_est/report")).json()
        assert rep["verdict"]["estimated"] >= 10  # every call was estimated from streamed text, none silently dropped


async def test_five_usage_less_responses_halt_with_usage_term(tmp_path):
    bare = FastAPI()

    @bare.post("/v1/chat/completions")
    async def chat_no_usage(request: Request):
        return JSONResponse({"id": "x", "object": "chat.completion", "model": "m",
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]})

    @bare.get("/v1/models")
    async def models():
        return {"data": []}

    app = create_app("http://bare/v1", db_path=tmp_path / "u.sqlite", transport=httpx.ASGITransport(app=bare))
    app.state.store.create_job("j_u", TERMS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        codes = [(await c.post("/j/j_u/v1/chat/completions", json=chat())).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5] == 402
    rep = (await httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby").get("/j/j_u/report")).json()
    assert rep["halted_term"] == "usage" and rep["verdict"]["unmetered"] == 5 and rep["verdict"]["valid"] == 0


async def test_error_responses_never_halt_and_are_tallied(tmp_path):
    mock = create_mock(seed=4, error_rate=1.0, error_codes=(429,))
    app = create_app("http://mock/v1", db_path=tmp_path / "e.sqlite", transport=httpx.ASGITransport(app=mock))
    app.state.store.create_job("j_err", TERMS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        codes = [(await c.post("/j/j_err/v1/chat/completions", json=chat())).status_code for _ in range(20)]
        assert codes == [429] * 20  # upstream errors pass through untouched; never Offby's 402
        rep = (await c.get("/j/j_err/report")).json()
    assert rep["state"] == "running" and rep["verdict"]["errors"] == {"429": 20} and rep["verdict"]["valid"] == 0


async def test_hostile_profile_still_halts_and_prices_per_token_strings(tmp_path):
    mock = create_mock(seed=9, **PROFILES["hostile"])
    app = create_app("http://mock/v1", db_path=tmp_path / "h.sqlite", transport=httpx.ASGITransport(app=mock))
    app.state.store.create_job("j_h", TERMS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        for i in range(60):
            r = await c.post("/j/j_h/v1/chat/completions", json=chat())
            if r.status_code == 402:
                break
        assert r.status_code == 402 and r.headers["x-offby-halt"] == "output_tokens"
        rep = (await c.get("/j/j_h/report")).json()
    v = rep["verdict"]
    assert v["spent_usd"] > 0 and v["unpriced_calls"] == 0  # canonical id priced from "0.00000006"-style strings
    assert "429" in v["errors"] or "500" in v["errors"] or v["n"] == v["valid"]
    diag = app.state.store.get_job("j_h")["evidence"]
    assert diag["reasoning_share"] > 0.5 and diag["reasoning_estimated"]  # trace arrived as reasoning_content → estimated


async def test_embeddings_are_metered_and_can_halt_on_input(tmp_path):
    mock = create_mock(seed=1)
    app = create_app("http://mock/v1", db_path=tmp_path / "em.sqlite", transport=httpx.ASGITransport(app=mock))
    terms = {"calls": {"value": 1000, "source": "stated"}, "input_tokens": {"value": 400, "source": "stated"},
             "price": {"in_per_m": 0.06, "out_per_m": 0.24, "source": "oracle"}}
    app.state.store.create_job("j_emb", terms)
    big = {"model": "mock/nemotron-3-nano-30b-a3b", "input": ["x" * 36_000]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        for i in range(15):
            r = await c.post("/j/j_emb/v1/embeddings", json=big)
            if r.status_code == 402:
                break
        assert r.status_code == 402 and r.headers["x-offby-halt"] == "input_tokens"


async def test_halted_job_still_serves_non_metered_paths(stack):
    app, store = stack
    store.create_job("j_x", TERMS)
    store.update_job("j_x", state="halted", halted_term="output_tokens")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        assert (await c.get("/j/j_x/v1/models")).status_code == 200
        r = await c.post("/j/j_x/v1/chat/completions", json=chat())
        assert r.status_code == 402 and r.headers["x-should-retry"] == "false"
        assert r.json()["error"]["message"].startswith("offby_term_breach:")


async def test_upstream_without_prices_uses_forecast_price_flagged(tmp_path):
    mock = create_mock(seed=3, pricing="absent")
    app = create_app("http://mock/v1", db_path=tmp_path / "fp.sqlite", transport=httpx.ASGITransport(app=mock))
    app.state.store.create_job("j_fp", TERMS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        for i in range(12):
            r = await c.post("/j/j_fp/v1/chat/completions", json=chat())
            if r.status_code == 402:
                break
        assert r.status_code == 402
        assert "at the forecast price" in r.json()["error"]["message"]
        rep = (await c.get("/j/j_fp/report")).json()["verdict"]
    assert rep["spent_usd"] > 0 and rep["unpriced_calls"] == 0 and rep["forecast_priced"] == rep["valid"]
    assert rep["per_term"]["price"]["ratio"] is None  # a forecast price cannot detect a model swap


async def test_rerun_after_accept_does_not_double_count_calls(stack):
    app, store = stack
    terms = dict(TERMS); terms["calls"] = {"value": 12, "source": "stated"}
    store.create_job("j_re", terms)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offby") as c:
        for _ in range(11):
            r = await c.post("/j/j_re/v1/chat/completions", json=chat())
        assert r.status_code == 402
        store.resume("j_re")
        for _ in range(12):  # the whole file again, thinking off
            r = await c.post("/j/j_re/v1/chat/completions", json=chat(think=False))
        assert r.status_code == 200
        rep = (await c.get("/j/j_re/report")).json()["verdict"]
    assert rep["per_term"]["calls"]["observed"] == 12 and not rep["per_term"]["calls"]["breached"]
