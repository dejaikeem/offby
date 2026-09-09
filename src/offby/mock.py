"""A fake OpenAI-compatible upstream so you can watch Offby referee a job without
spending a cent. Reproduces the README scenario — thinking on by default, reasoning
billed as output, off when the request says so — and, with the realism knobs, the
things a real upstream does that a happy-path mock hides: heavy-tailed output
lengths, latency, 429/5xx, max_tokens truncation, reasoning reported in different
shapes, canonical model ids, and price listings in different shapes.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .tf import DIAG_SYSTEM, PARSE_SYSTEM

DEFAULT_MODEL = "mock/nemotron-3-nano-30b-a3b"
CANONICAL = {"mock/nemotron-3-nano-30b-a3b": "nvidia/nemotron-3-nano-30b-a3b",
             "mock/nemotron-3-super-120b-a12b": "nvidia/nemotron-3-super-120b-a12b"}

PROFILES = {
    # what the README quickstart shows
    "friendly": {},
    # what a real gateway does to you
    "hostile": {"error_rate": 0.1, "reasoning_report": "reasoning-field", "respond_model": "canonical",
                "pricing": "per-token-strings", "tail_sigma_on": 0.8, "tail_sigma_off": 0.3},
}


def _think_off(body: dict) -> bool:
    kw = body.get("chat_template_kwargs") or {}
    return kw.get("enable_thinking") is False


def create_mock(
    *,
    model: str = DEFAULT_MODEL,
    output_tokens: int = 2150,
    reasoning_share: float = 0.87,
    output_tokens_off: int = 290,
    price: tuple[float, float] = (0.06, 0.24),
    latency_ms: float = 0.0,
    latency_jitter: float = 0.5,
    jitter: float = 0.05,
    tail_sigma_on: float = 0.0,
    tail_sigma_off: float = 0.0,
    error_rate: float = 0.0,
    error_codes: tuple[int, ...] = (429, 500),
    ignore_include_usage: bool = False,
    reasoning_report: str = "details",  # details | reasoning-field | think-tags | hidden
    respond_model: str = "echo",  # echo | canonical | base
    pricing: str = "per-1m",  # per-1m | per-token-strings | zero | absent
    honor_max_tokens: bool = True,
    seed: int | None = None,
) -> FastAPI:
    rng = random.Random(seed)
    app = FastAPI(title="offby mock upstream")

    def model_out(requested: str | None) -> str:
        req = requested or model
        if respond_model == "canonical":
            return CANONICAL.get(req, req)
        if respond_model == "base":
            return re.sub(r"-fast$", "", req)
        return req

    def price_entry(pin: float, pout: float) -> dict:
        if pricing == "per-token-strings":
            return {"pricing": {"input": f"{pin / 1e6:.10f}".rstrip("0"), "output": f"{pout / 1e6:.10f}".rstrip("0")}}
        if pricing == "zero":
            return {"pricing": {"prompt": "0", "completion": "0"}}
        if pricing == "absent":
            return {}
        return {"pricing": {"prompt": pin, "completion": pout, "unit": "usd_per_1m_tokens"}}

    @app.get("/v1/models")
    async def models():
        ids = [model_out(model), model_out("mock/nemotron-3-super-120b-a12b")]
        return {"object": "list", "data": [
            {"id": ids[0], "object": "model", "context_length": 131072, **price_entry(*price)},
            {"id": ids[1], "object": "model", "context_length": 262144, **price_entry(0.09, 0.40)},
        ]}

    def mock_parse(sentence: str) -> str:
        """Answer Offby's forecast-parse prompt with a plausible five-term JSON (no model here)."""
        def num(pat, default):
            m = re.search(pat, sentence, re.I)
            if not m:
                return default, "assumed"
            s = m.group(1).lower().replace(",", "")
            mult = 1000 if s.endswith("k") else (1_000_000 if s.endswith("m") else 1)
            return int(float(s.rstrip("km")) * mult), "stated"
        calls, csrc = num(r"(\d[\d,]*\s*[km]?)\s*(?:rows?|calls?|items?|reviews?|행|건|개)", 200000)
        return json.dumps({
            "calls": {"value": calls, "source": csrc, "why": "count in the sentence" if csrc == "stated" else "no count given"},
            "input_tokens": {"value": 400, "source": "assumed", "why": "short prompts"},
            "output_tokens": {"value": 250, "source": "assumed", "why": "paragraph answers"},
            "model": {"value": model_out(model), "source": "stated" if "nano" in sentence.lower() else "assumed", "why": "model named" if "nano" in sentence.lower() else "default"},
            "window": {"value": "tonight" if re.search(r"tonight|오늘 밤", sentence, re.I) else None, "source": "stated" if re.search(r"tonight|오늘 밤", sentence, re.I) else "assumed", "why": ""},
        })

    def mock_diagnosis(msgs: list[dict]) -> str | None:
        """If this is Offby's own diagnosis call, answer from the evidence instead of pretending to be a reviewer."""
        try:
            ev = json.loads(next(m["content"] for m in msgs if m.get("role") == "user"))
        except (StopIteration, json.JSONDecodeError, KeyError, TypeError):
            return "(mock diagnosis) could not read the evidence bundle."
        term = ev.get("breached_term")
        t = (ev.get("terms") or {}).get(term or "", {})
        share = ev.get("reasoning_share")
        fc = (ev.get("forecast") or {})
        calls = (fc.get("calls") or {}).get("value")
        tin = (fc.get("input_tokens") or {}).get("value") or 0
        pin, pout = (fc.get("price") or {}).get("in_per_m", price[0]), (fc.get("price") or {}).get("out_per_m", price[1])
        fixed_total = calls * (tin * pin + output_tokens_off * pout) / 1e6 if calls else None
        lines = ["(mock diagnosis — a real run would come from nemotron-3-super)"]
        if term == "output_tokens" and share is not None and share > 0.5:
            lines.append(f"MECHANISM: output_tokens ran {t.get('ratio', 0):.1f}x the forecast ({t.get('forecast')}→{t.get('observed', 0):.0f}); "
                         f"{share:.0%} of completion tokens are reasoning. The model thinks by default and bills the trace as output.")
            lines.append("UNKNOWN: whether thinking was meant to be on — the forecast priced a paragraph, not a trace.")
            fix = f"FIX: send chat_template_kwargs.enable_thinking=false → ≈{output_tokens_off} output/call"
            if fixed_total is not None:
                fix += f", projected ${fixed_total:,.2f}"
            lines.append(fix + ".")
        else:
            lines.append(f"MECHANISM: term {term} ran {t.get('ratio', 0):.1f}x the forecast.")
            lines.append("UNKNOWN: which request parameter changed between the forecast and the run.")
            lines.append(f"FIX: accept the observed value (offby accept <job> {term}=<observed>) or change the request.")
        return "\n".join(lines)

    def error_response(stream: bool):
        code = rng.choice(error_codes)
        body = {"error": {"message": f"mock upstream error {code}", "type": "server_error" if code >= 500 else "rate_limit_error", "code": code}}
        headers = {"Retry-After": "1"} if code == 429 else {}
        return JSONResponse(body, status_code=code, headers=headers)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        msgs = body.get("messages") or []
        sys_msg = next((str(m.get("content", "")) for m in msgs if m.get("role") == "system"), "")
        if sys_msg.startswith(DIAG_SYSTEM[:40]):
            diag = mock_diagnosis(msgs)
            return JSONResponse({"id": "chatcmpl-mock-diag", "object": "chat.completion", "created": int(time.time()),
                                 "model": model_out(body.get("model")),
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": diag}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 400, "completion_tokens": 120, "total_tokens": 520}})
        if sys_msg.startswith(PARSE_SYSTEM[:40]):
            user = next((str(m.get("content", "")) for m in msgs if m.get("role") == "user"), "")
            return JSONResponse({"id": "chatcmpl-mock-parse", "object": "chat.completion", "created": int(time.time()),
                                 "model": model_out(body.get("model")),
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": mock_parse(user)}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 300, "completion_tokens": 90, "total_tokens": 390}})

        if latency_ms:
            await asyncio.sleep(latency_ms / 1000 * (1 + rng.uniform(-latency_jitter, latency_jitter)))
        if error_rate and rng.random() < error_rate:
            return error_response(bool(body.get("stream")))

        prompt_tokens = 8 + sum(len(str(m.get("content", ""))) // 4 for m in msgs)
        off = _think_off(body)
        base = output_tokens_off if off else output_tokens
        sigma = tail_sigma_off if off else tail_sigma_on
        if sigma:
            completion = max(1, round(base * math.exp(sigma * rng.gauss(0, 1))))
        else:
            completion = max(1, round(base * (1 + rng.uniform(-jitter, jitter))))
        finish = "stop"
        cap = body.get("max_tokens") or body.get("max_completion_tokens")
        if honor_max_tokens and isinstance(cap, int) and completion > cap:
            completion, finish = cap, "length"
        reasoning = 0 if off else round(completion * reasoning_share)
        answer_tokens = completion - reasoning
        mid = model_out(body.get("model"))
        answer = "Positive. The reviewer praises build quality and battery life, with a minor note on price."
        content = (answer * max(1, math.ceil(answer_tokens * 4 / len(answer))))[: max(4, answer_tokens * 4)]
        trace = ("Let me weigh the evidence. " * max(1, math.ceil(reasoning * 4 / 26)))[: reasoning * 4] if reasoning else ""
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion, "total_tokens": prompt_tokens + completion}
        message: dict = {"role": "assistant", "content": content}
        if reasoning:
            if reasoning_report == "details":
                usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
            elif reasoning_report == "reasoning-field":
                message["reasoning_content"] = trace
            elif reasoning_report == "think-tags":
                message["content"] = f"<think>{trace}</think>{content}"
            # hidden: nothing reveals the trace; only completion_tokens grows
        elif reasoning_report == "details":
            usage["completion_tokens_details"] = {"reasoning_tokens": 0}
        rid = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage")) and not ignore_include_usage

            async def gen():
                if reasoning and reasoning_report == "reasoning-field":
                    for i in range(0, len(trace), 64):
                        yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid,
                                                     "choices": [{"index": 0, "delta": {"reasoning_content": trace[i:i + 64]}, "finish_reason": None}]}) + "\n\n"
                text = message["content"]
                for i in range(0, len(text), 48):
                    yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid,
                                                 "choices": [{"index": 0, "delta": {"content": text[i:i + 48]}, "finish_reason": None}]}) + "\n\n"
                yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid,
                                             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\n"
                if include_usage:  # spec shape: a final chunk with empty choices carrying usage
                    yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid,
                                                 "choices": [], "usage": usage}) + "\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse({
            "id": rid, "object": "chat.completion", "created": created, "model": mid,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        })

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        if latency_ms:
            await asyncio.sleep(latency_ms / 1000 * (1 + rng.uniform(-latency_jitter, latency_jitter)))
        if error_rate and rng.random() < error_rate:
            return error_response(False)
        inp = body.get("input")
        texts = inp if isinstance(inp, list) else [inp]
        prompt_tokens = sum(len(str(t)) // 4 + 2 for t in texts)
        return JSONResponse({"object": "list", "model": model_out(body.get("model")),
                             "data": [{"object": "embedding", "index": i, "embedding": [0.0] * 8} for i in range(len(texts))],
                             "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens}})

    return app
