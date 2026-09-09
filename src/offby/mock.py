"""A fake OpenAI-compatible upstream so you can watch Offby halt a job without
spending a cent. Reproduces the README scenario: think-on by default (reasoning
billed as output), think-off when the request says so.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def _think_off(body: dict) -> bool:
    kw = body.get("chat_template_kwargs") or {}
    if kw.get("enable_thinking") is False:
        return True
    return str(body.get("reasoning_effort", "")).lower() == "none"


def create_mock(
    *,
    model: str = "nvidia/nemotron-3-nano-30b-a3b",
    output_tokens: int = 2150,
    reasoning_share: float = 0.87,
    output_tokens_off: int = 290,
    price: tuple[float, float] = (0.06, 0.24),
    latency_ms: float = 0.0,
    jitter: float = 0.05,
    seed: int | None = None,
) -> FastAPI:
    rng = random.Random(seed)
    app = FastAPI(title="offby mock upstream")

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [
            {"id": model, "object": "model", "context_length": 131072,
             "pricing": {"prompt": price[0], "completion": price[1], "unit": "usd_per_1m_tokens"}},
            {"id": "nvidia/nemotron-3-super-120b-a12b", "object": "model", "context_length": 262144,
             "pricing": {"prompt": 0.09, "completion": 0.40, "unit": "usd_per_1m_tokens"}},
        ]}

    def mock_diagnosis(msgs: list[dict]) -> str | None:
        """If this is Offby's own diagnosis call, answer from the evidence instead of pretending to be a reviewer."""
        sys_msg = next((m.get("content", "") for m in msgs if m.get("role") == "system"), "")
        if not str(sys_msg).startswith("You are Offby's diagnosis engine"):
            return None
        try:
            ev = json.loads(next(m["content"] for m in msgs if m.get("role") == "user"))
        except (StopIteration, json.JSONDecodeError, KeyError):
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

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        if latency_ms:
            await asyncio.sleep(latency_ms / 1000)
        msgs = body.get("messages") or []
        diag = mock_diagnosis(msgs)
        if diag is not None:
            return JSONResponse({"id": "chatcmpl-mock-diag", "object": "chat.completion", "created": int(time.time()),
                                 "model": body.get("model") or model,
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": diag}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 400, "completion_tokens": 120, "total_tokens": 520}})
        prompt_tokens = 8 + sum(len(str(m.get("content", ""))) // 4 for m in msgs)
        off = _think_off(body)
        base = output_tokens_off if off else output_tokens
        completion = max(1, round(base * (1 + rng.uniform(-jitter, jitter))))
        reasoning = 0 if off else round(completion * reasoning_share)
        mid = body.get("model") or model
        content = "Positive. The reviewer praises build quality and battery life." if off else \
            "Positive. The reviewer praises build quality and battery life, with a minor note on price."
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion,
            "total_tokens": prompt_tokens + completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        }
        rid = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

            async def gen():
                words = content.split()
                for i in range(0, len(words), 4):
                    chunk = {"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid,
                             "choices": [{"index": 0, "delta": {"content": " ".join(words[i:i + 4]) + " "}, "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                final = {"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                if include_usage:
                    final["usage"] = usage
                yield f"data: {json.dumps(final)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse({
            "id": rid, "object": "chat.completion", "created": created, "model": mid,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": usage,
        })

    return app
