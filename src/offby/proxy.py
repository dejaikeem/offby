"""The relay. Forwards OpenAI-compatible requests unchanged, records `usage`
per response, asks the judge, and returns Offby's own 402 once a term breaches.
Zero model calls on the hot path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import judge as J
from . import tf, usage as U
from .store import Store

log = logging.getLogger("offby")

HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length", "content-encoding", "accept-encoding",
}
METERED_PATHS = {"chat/completions", "completions", "embeddings", "responses"}
UNMETERED_HALT_AFTER = 5  # consecutive 2xx responses without a usage object


def _usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def create_app(
    upstream: str = tf.DEFAULT_UPSTREAM,
    *,
    api_key: str | None = None,
    fail_open: bool = False,
    db_path=None,
    transport: httpx.AsyncBaseTransport | None = None,
    price_ttl: float = 3600.0,
) -> FastAPI:
    store = Store(db_path)
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600.0, connect=30.0))

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    app = FastAPI(title="offby", lifespan=lifespan)
    prices: dict = {"at": 0.0, "data": {}, "error": None}
    agg: dict[str, list[J.Call]] = {}
    inflight: dict[str, int] = {}
    unmetered_streak: dict[str, int] = {}
    tasks: set[asyncio.Task] = set()  # keep references, or the diagnosis task can be garbage-collected mid-flight
    app.state.store, app.state.client = store, client

    # ---------- helpers ----------
    async def get_prices(key: str | None):
        stale = time.time() - prices["at"]
        retry_after = price_ttl if prices["data"] else 60.0  # a failed/empty fetch is retried after a minute
        if stale > retry_after:
            try:
                prices["data"] = await tf.fetch_prices(client, upstream, key or api_key)
                prices["error"] = None
            except Exception as e:  # noqa: BLE001
                prices["error"] = str(e)
                log.warning("price oracle unavailable: %s", e)
            prices["at"] = time.time()
        return prices["data"]

    def row_call(r: dict) -> J.Call:
        return J.Call(r["prompt_tokens"], r["completion_tokens"], r["in_per_m"], r["out_per_m"],
                      status=r.get("status") or 200, estimated=bool(r.get("usage_estimated")),
                      price_source=r.get("price_source") or ("oracle" if r["in_per_m"] is not None else None))

    def calls_of(job_id: str) -> list[J.Call]:
        if job_id not in agg:
            agg[job_id] = [row_call(r) for r in store.calls(job_id)]
        return agg[job_id]

    def verdict_of(job: dict, calls: list[J.Call] | None = None) -> J.Verdict:
        return J.judge(job["terms"], calls if calls is not None else calls_of(job["id"]),
                       min_calls=job["min_calls"], threshold=job["threshold"],
                       since=job.get("judge_from") or 0, run_since=job.get("run_from") or 0)

    def halt_response(request: Request, job: dict) -> JSONResponse:
        v = verdict_of(job)
        term = job.get("halted_term") or v.term or "unknown"
        tv = v.terms.get(term)
        fc_calls = J.term_value(job["terms"], "calls")
        judged_at = job.get("halted_at")
        where = f"{v.valid}/{fc_calls:,}" if fc_calls else str(v.valid)
        if judged_at is not None and judged_at != v.valid:
            where += f" (judged at {judged_at}, {v.valid - judged_at} were in flight)"
        money = ""
        if v.projected_usd is not None and v.expected_usd is not None:
            money = f"; projected {_usd(v.projected_usd)} vs {_usd(v.expected_usd)}"
            if v.forecast_priced:
                money += " (at the forecast price — upstream lists none)"
        if term == "usage":
            msg = (f"{UNMETERED_HALT_AFTER} consecutive responses carried no usage object — Offby cannot referee "
                   f"this upstream; halted at {where}. Check that it honors stream_options.include_usage, "
                   f"or resume with `offby accept {job['id']}`")
            suggest = None
        elif tv and tv.ratio is not None:
            change = f"unit price {tv.ratio:.1f}x forecast" if term == "price" else f"{tv.forecast:g}→{tv.observed:.0f}"
            conf = f", t={tv.t:.1f}" if tv.t is not None and tv.t < J.T_CAP else ""
            msg = f"term {term} breached {tv.ratio:.1f}x ({change}{conf}) — halted at {where}{money}"
            suggest = J.suggest_accept(tv)
        else:
            msg = f"term {term} breached — halted at {where}{money}"
            suggest = None
        resume = (f"offby accept {job['id']} {term}={suggest}" if suggest is not None
                  else f"offby accept {job['id']}")
        diag_url = f"{request.base_url}j/{job['id']}/diagnosis"
        body = {"error": {
            "type": "offby_term_breach", "code": "offby_term_breach", "term": term,
            "ratio": round(tv.ratio, 2) if tv and tv.ratio is not None else None,
            "message": f"offby_term_breach: {msg}", "resume": resume, "diagnosis": diag_url,
            "errors": v.errors, "unmetered": v.unmetered,
        }}
        return JSONResponse(body, status_code=402, headers={
            "X-Offby-Halt": term, "X-Offby-Diagnosis": diag_url, "X-Should-Retry": "false", "Retry-After": "0",
        })

    async def run_diagnosis(job_id: str, key: str | None) -> None:
        job = store.get_job(job_id)
        if not job or not job.get("evidence"):
            return
        evidence = job["evidence"]
        if not key:
            store.update_job(job_id, diagnosis_status="unavailable",
                             diagnosis="no API key for diagnosis (pass --api-key or set NEBIUS_API_KEY; the job's own bearer is reused when present)")
            return
        try:
            text = await tf.diagnose(client, upstream, key, evidence)
            if not text.strip() or ("MECHANISM" not in text.upper() and "FIX" not in text.upper()):
                store.update_job(job_id, diagnosis_status="unavailable",
                                 diagnosis=f"diagnosis model returned no usable text ({len(text)} chars)")
                return
            store.update_job(job_id, diagnosis_status="done", diagnosis=text)
        except httpx.HTTPStatusError as e:
            hint = f"diagnosis model {tf.DIAG_MODEL!r} is not served by this upstream" if e.response.status_code == 404 \
                else f"upstream returned {e.response.status_code} for the diagnosis call"
            store.update_job(job_id, diagnosis_status="unavailable",
                             diagnosis=f"{hint} — set OFFBY_DIAG_MODEL to a model it has, or read the evidence above")
        except Exception as e:  # noqa: BLE001
            store.update_job(job_id, diagnosis_status="unavailable", diagnosis=f"diagnosis failed: {e}")

    def halt(job_id: str, job: dict, term: str, v: J.Verdict, key: str | None) -> None:
        rows = store.calls(job_id)[job.get("run_from") or 0:]  # this run only
        evidence = tf.build_evidence(job, rows, v)  # arithmetic, stored right away
        store.update_job(job_id, state="halted", halted_term=term, halted_at=v.valid, evidence=evidence,
                         diagnosis_status="pending", diagnosis=None)
        log.warning(json.dumps({"event": "halt", "job": job_id, "term": term, "ratio": v.ratio,
                                "valid": v.valid, "spent_usd": round(v.spent_usd, 6), "projected_usd": v.projected_usd}))
        t = asyncio.create_task(run_diagnosis(job_id, key))
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def meter(job_id: str, status: int, content: bytes, elapsed_s: float, request: Request, *, sse: bool) -> None:
        u = None
        try:
            if content:
                u = U.from_sse(content.decode(errors="replace")) if sse else U.from_response(json.loads(content))
        except Exception:  # noqa: BLE001 — unparseable body: record the call, tokens unknown
            u = None
        if u and u.error_status and status < 400:
            status = u.error_status  # an error delivered inside a 200 (mid-stream) is still an error
        key = api_key or _bearer(request)
        price = tf.price_for(await get_prices(key), u.model) if u and u.model else None
        source = "oracle" if price else None
        job = store.get_job(job_id)
        if price is None and u is not None:
            fc_in_pm, fc_out_pm = J.price_terms(job["terms"])
            if fc_in_pm is not None and fc_out_pm is not None:
                price, source = (fc_in_pm, fc_out_pm), "forecast"  # no price list upstream: spend at the stated price, flagged
        in_pm, out_pm = price if price else (None, None)
        call = J.Call(u.prompt_tokens if u else None, u.completion_tokens if u else None, in_pm, out_pm,
                      status=status, estimated=bool(u and u.estimated), price_source=source)
        calls = calls_of(job_id)  # load history from the store BEFORE inserting this call, or it counts twice
        store.add_call(
            job_id, model=u.model if u else None, status=status,
            prompt_tokens=call.prompt_tokens, completion_tokens=call.completion_tokens,
            reasoning_tokens=u.reasoning_tokens if u else None,
            reasoning_estimated=int(bool(u and u.reasoning_estimated)),
            usage_estimated=int(call.estimated), finish_reason=u.finish_reason if u else None,
            cached_tokens=u.cached_tokens if u else None,
            latency_ms=round(elapsed_s * 1000, 1), in_per_m=in_pm, out_per_m=out_pm, price_source=source,
            cost_usd=call.cost_usd,
        )
        calls.append(call)
        if job["state"] == "halted":
            return
        # a 2xx without usage is an event, not a silent None
        if call.ok and not call.metered:
            unmetered_streak[job_id] = unmetered_streak.get(job_id, 0) + 1
            if unmetered_streak[job_id] >= UNMETERED_HALT_AFTER:
                halt(job_id, job, "usage", verdict_of(job, calls), key)
                unmetered_streak[job_id] = 0
                return
        elif call.metered:
            unmetered_streak[job_id] = 0
        terms = job["terms"]
        run_from = job.get("run_from") or 0
        if job.get("baseline_n") and not terms:
            valid = [c for c in calls[run_from:] if c.metered]
            if len(valid) >= job["baseline_n"]:
                terms = J.baseline_terms(valid[: job["baseline_n"]], model=u.model if u else None)
                store.update_job(job_id, terms=terms)
                log.warning(json.dumps({"event": "baseline_locked", "job": job_id, "terms": terms}))
        if not terms:
            return
        v = J.judge(terms, calls, min_calls=job["min_calls"], threshold=job["threshold"],
                    since=job.get("judge_from") or 0, run_since=run_from)
        if v.breached:
            halt(job_id, job, v.term, v, key)

    # ---------- routes ----------
    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "upstream": upstream,
                "price_oracle": "ok" if prices["data"] else (prices["error"] or "not fetched"),
                "inflight": {k: n for k, n in inflight.items() if n}}

    @app.get("/j/{job_id}/diagnosis")
    async def diagnosis(job_id: str):
        job = store.get_job(job_id)
        if not job:
            return JSONResponse({"error": {"type": "offby_unknown_job"}}, status_code=404)
        return {"job": job_id, "status": job.get("diagnosis_status") or "none",
                "diagnosis": job.get("diagnosis"), "evidence": job.get("evidence")}

    @app.get("/j/{job_id}/report")
    async def report(job_id: str):
        job = store.get_job(job_id)
        if not job:
            return JSONResponse({"error": {"type": "offby_unknown_job"}}, status_code=404)
        v = verdict_of(job)
        return {"job": job_id, "state": job["state"], "halted_term": job.get("halted_term"), "terms": job["terms"],
                "verdict": {"n": v.n, "valid": v.valid, "judged": v.judged, "total": v.total, "breached": v.breached,
                            "term": v.term, "ratio": v.ratio, "spent_usd": v.spent_usd, "expected_usd": v.expected_usd,
                            "projected_usd": v.projected_usd, "unpriced_calls": v.unpriced_calls,
                            "forecast_priced": v.forecast_priced,
                            "errors": v.errors, "unmetered": v.unmetered, "estimated": v.estimated,
                            "inflight": inflight.get(job_id, 0),
                            "per_term": {k: t.__dict__ for k, t in v.terms.items()}},
                "diagnosis_status": job.get("diagnosis_status"), "diagnosis": job.get("diagnosis")}

    @app.api_route("/j/{job_id}/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    async def relay(job_id: str, path: str, request: Request):
        job = store.get_job(job_id)
        if not job:
            known = [j["id"] for j in store.list_jobs()][:10]
            return JSONResponse({"error": {"type": "offby_unknown_job",
                                           "message": f"no job {job_id!r}; run `offby job ensure {job_id} ...` first",
                                           "known_jobs": known}}, status_code=404)
        metered = request.method == "POST" and path.strip("/") in METERED_PATHS
        if job["state"] == "halted" and metered:
            return halt_response(request, job)

        body = await request.body()
        stream = False
        if metered and body and request.headers.get("content-type", "").startswith("application/json"):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("stream") and path.strip("/") in ("chat/completions", "completions"):
                stream = True
                so = payload.get("stream_options") or {}
                so["include_usage"] = True
                payload["stream_options"] = so
                body = json.dumps(payload).encode()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "gzip, deflate"
        if api_key and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {api_key}"
        url = f"{upstream.rstrip('/')}/{path}"
        params = str(request.url.query) or None
        t0 = time.perf_counter()
        if metered:
            inflight[job_id] = inflight.get(job_id, 0) + 1
        try:
            if stream:
                up = await client.send(client.build_request(request.method, url, headers=headers, content=body, params=params), stream=True)
            else:
                up = await client.request(request.method, url, headers=headers, content=body, params=params)
        except httpx.HTTPError as e:
            if metered:
                inflight[job_id] -= 1
            return JSONResponse({"error": {"type": "offby_upstream_unreachable", "message": str(e)}}, status_code=502)
        resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP | {"date", "server"}}

        async def do_meter(content: bytes, sse: bool):
            try:
                await meter(job_id, up.status_code, content, time.perf_counter() - t0, request, sse=sse)
            except Exception as e:  # noqa: BLE001
                if fail_open:
                    log.error("meter failed (fail-open, passing traffic): %s", e)
                else:
                    raise
            finally:
                inflight[job_id] = max(0, inflight.get(job_id, 1) - 1)

        if not stream:
            content = up.content
            if metered:
                await do_meter(content, sse=False)
            return Response(content=content, status_code=up.status_code, headers=resp_headers)

        async def gen():
            buf = bytearray()
            try:
                async for chunk in up.aiter_bytes():
                    buf.extend(chunk)
                    yield chunk
            finally:
                await up.aclose()
                if metered:
                    await do_meter(bytes(buf), sse=True)

        return StreamingResponse(gen(), status_code=up.status_code, headers=resp_headers)

    return app
