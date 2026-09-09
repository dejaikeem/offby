import httpx
import pytest

from offby.mock import create_mock
from offby.proxy import create_app
from offby.store import Store

pytestmark = pytest.mark.anyio

TERMS = {
    "calls": {"value": 200_000, "source": "stated"},
    "input_tokens": {"value": 400, "source": "assumed"},
    "output_tokens": {"value": 250, "source": "assumed"},
    "model": {"value": "nvidia/nemotron-3-nano-30b-a3b", "source": "stated"},
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
    body = {"model": "nvidia/nemotron-3-nano-30b-a3b", "messages": [{"role": "user", "content": "classify: great battery"}]}
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
        for _ in range(6):
            r = await c.post("/j/j_b/v1/chat/completions", json=chat(think=True))  # now ≈2150
        assert r.status_code == 402 and r.headers["x-offby-halt"] == "output_tokens"
