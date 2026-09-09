import httpx
import pytest

from offby import cli
from offby.mock import create_mock
from offby.proxy import create_app
from offby.store import Store

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_ensure_creates_then_starts_new_run(tmp_path, capsys):
    db = str(tmp_path / "t.sqlite")
    rc = cli.main(["--db", db, "job", "ensure", "classify-reviews", "--calls", "120", "--input", "50", "--output", "250",
                   "--price", "0.06,0.24", "--model", "m", "-y"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "created" in out and "/j/classify-reviews/v1" in out and "expected total" in out
    store = Store(db)
    for _ in range(7):
        store.add_call("classify-reviews", model="m", status=200, prompt_tokens=50, completion_tokens=2000,
                       in_per_m=0.06, out_per_m=0.24, cost_usd=0.0005)
    store.update_job("classify-reviews", state="halted", halted_term="output_tokens")
    rc = cli.main(["--db", db, "job", "ensure", "classify-reviews", "-y"])  # no flags: keep forecast, new run
    assert rc == 0
    out = capsys.readouterr().out
    assert "new run started (7 calls in history, forecast kept)" in out
    job = store.get_job("classify-reviews")
    assert job["state"] == "running" and job["run_from"] == 7 and job["judge_from"] == 7
    assert job["terms"]["output_tokens"]["value"] == 250


def test_ensure_rejects_bad_name_and_empty_forecast(tmp_path, capsys):
    db = str(tmp_path / "t.sqlite")
    assert cli.main(["--db", db, "job", "ensure", "Bad Name", "--calls", "1"]) == 2
    assert cli.main(["--db", db, "job", "ensure", "fine", "-y"]) == 2  # new job, nothing to forecast


async def test_lessons_reads_thinking_split_and_breach(tmp_path, capsys):
    db = tmp_path / "t.sqlite"
    mock = create_mock(seed=3)
    app = create_app("http://mock/v1", db_path=db, transport=httpx.ASGITransport(app=mock))
    store = app.state.store
    store.create_job("nightly", {
        "calls": {"value": 1000, "source": "stated"}, "input_tokens": {"value": 50, "source": "stated"},
        "output_tokens": {"value": 250, "source": "stated"},
        "price": {"in_per_m": 0.06, "out_per_m": 0.24, "source": "oracle"}})
    body = {"model": "nvidia/nemotron-3-nano-30b-a3b", "messages": [{"role": "user", "content": "x"}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://o") as c:
        for _ in range(11):
            r = await c.post("/j/nightly/v1/chat/completions", json=body)
        assert r.status_code == 402
        store.resume("nightly")
        for _ in range(5):
            r = await c.post("/j/nightly/v1/chat/completions", json={**body, "chat_template_kwargs": {"enable_thinking": False}})
        assert r.status_code == 200
    capsys.readouterr()
    assert cli.main(["--db", str(db), "lessons"]) == 0
    out = capsys.readouterr().out
    assert "nvidia/nemotron-3-nano-30b-a3b" in out and "with thinking" in out and "without" in out
    assert "thinking multiplies output" in out
    assert "nightly: output_tokens" in out and "resumed after a fix" in out
