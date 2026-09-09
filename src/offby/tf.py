"""Everything that talks to the upstream OUTSIDE the hot path: price oracle,
forecast parsing (nano), diagnosis (super). The proxy's relay never calls these
per request.
"""

from __future__ import annotations

import json
import os
import re

import httpx

DEFAULT_UPSTREAM = os.environ.get("OFFBY_UPSTREAM", "https://api.tokenfactory.nebius.com/v1")
PARSE_MODEL = os.environ.get("OFFBY_PARSE_MODEL", "nvidia/nemotron-3-nano-30b-a3b")
DIAG_MODEL = os.environ.get("OFFBY_DIAG_MODEL", "nvidia/nemotron-3-super-120b-a12b")
API_KEY_ENV = "NEBIUS_API_KEY"

_IN_KEYS = ("in_per_m", "input", "prompt", "input_per_million", "prompt_per_million", "input_tokens")
_OUT_KEYS = ("out_per_m", "output", "completion", "output_per_million", "completion_per_million", "output_tokens")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _extract_price(entry: dict) -> tuple[float, float] | None:
    """Tolerant read of a model entry's pricing. Shape of Token Factory's verbose listing is
    not pinned down here — anything unrecognised stays UNPRICED (never $0)."""
    pricing = None
    for k in ("pricing", "price", "prices", "cost"):
        if isinstance(entry.get(k), dict):
            pricing = entry[k]
            break
    if pricing is None:
        return None
    pin = next((_num(pricing[k]) for k in _IN_KEYS if k in pricing), None)
    pout = next((_num(pricing[k]) for k in _OUT_KEYS if k in pricing), None)
    if pin is None or pout is None or pin <= 0 or pout <= 0:
        return None  # "0" / absent / negative is not a price — UNPRICED, never $0
    unit = str(pricing.get("unit", "")).lower().replace(" ", "")
    if "1m" in unit or "million" in unit or "per_m" in unit:
        scale = 1.0
    elif "1k" in unit or "thousand" in unit:
        scale = 1e3
    elif "token" in unit:
        scale = 1e6  # per single token
    elif pin < 0.001 and pout < 0.001:
        scale = 1e6  # unlabeled but clearly per-token
    else:
        scale = 1.0
    return pin * scale, pout * scale


async def fetch_prices(client: httpx.AsyncClient, upstream: str, api_key: str | None = None) -> dict[str, tuple[float, float]]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = await client.get(f"{upstream.rstrip('/')}/models", params={"verbose": "true"}, headers=headers)
    r.raise_for_status()
    body = r.json()
    entries = body.get("data") if isinstance(body, dict) else body
    prices: dict[str, tuple[float, float]] = {}
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        p = _extract_price(e)
        if p:
            prices[e["id"]] = p
    return prices


def price_for(prices: dict[str, tuple[float, float]], model: str | None) -> tuple[float, float] | None:
    if not model:
        return None
    if model in prices:
        return prices[model]
    lower = {mid.lower(): p for mid, p in prices.items()}
    if model.lower() in lower:
        return lower[model.lower()]
    tail = model.split("/")[-1].lower()
    hits = [p for mid, p in prices.items() if mid.split("/")[-1].lower() == tail]
    return hits[0] if len(hits) == 1 else None  # ambiguous tail → UNPRICED rather than a guess


# ---------------- forecast parsing ----------------

FORECAST_SCHEMA = {
    "type": "object",
    "properties": {
        "calls": {"$ref": "#/$defs/int_term"},
        "input_tokens": {"$ref": "#/$defs/int_term"},
        "output_tokens": {"$ref": "#/$defs/int_term"},
        "model": {"$ref": "#/$defs/str_term"},
        "window": {"$ref": "#/$defs/str_term"},
    },
    "required": ["calls", "input_tokens", "output_tokens", "model", "window"],
    "additionalProperties": False,
    "$defs": {
        "int_term": {
            "type": "object",
            "properties": {
                "value": {"type": ["integer", "null"]},
                "source": {"type": "string", "enum": ["stated", "assumed"]},
                "why": {"type": "string"},
            },
            "required": ["value", "source", "why"],
            "additionalProperties": False,
        },
        "str_term": {
            "type": "object",
            "properties": {
                "value": {"type": ["string", "null"]},
                "source": {"type": "string", "enum": ["stated", "assumed"]},
                "why": {"type": "string"},
            },
            "required": ["value", "source", "why"],
            "additionalProperties": False,
        },
    },
}

PARSE_SYSTEM = (
    "You turn one casual sentence about an LLM batch job into five measurable terms. "
    "Return ONLY JSON matching the schema. "
    "calls = total number of API calls; input_tokens and output_tokens are PER CALL; "
    "model = the model id or family mentioned; window = when it runs. "
    "A term is 'stated' only if the sentence gives it (a number, a model name, a time). "
    "Otherwise set source='assumed', pick a reasonable value from cues like 'short prompts' (~400 input) "
    "or 'paragraph answers' (~250 output), and explain the cue in 'why'. Unknown with no cue: value=null, source='assumed'."
)


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _first_json(text: str) -> dict:
    text = _strip_think(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


async def _chat(client, upstream, api_key, model, messages, response_format=None, max_tokens=1200) -> dict:
    # Offby's own calls want an answer, not a trace: ask for thinking off in both dialects
    # (vLLM/Token Factory read chat_template_kwargs, Ollama reads reasoning_effort). Either is ignored where unknown.
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
    if response_format:
        body["response_format"] = response_format
    r = await client.post(
        f"{upstream.rstrip('/')}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    r.raise_for_status()
    return r.json()


async def parse_forecast(client: httpx.AsyncClient, upstream: str, api_key: str, sentence: str, model: str = PARSE_MODEL) -> dict:
    """Sentence → {calls, input_tokens, output_tokens, model, window} with stated/assumed badges.
    Fallback chain: json_schema → json_object → free text + first {...} block."""
    messages = [{"role": "system", "content": PARSE_SYSTEM}, {"role": "user", "content": sentence}]
    formats = [
        {"type": "json_schema", "json_schema": {"name": "forecast", "schema": FORECAST_SCHEMA, "strict": True}},
        {"type": "json_object"},
        None,
    ]
    last_err: Exception | None = None
    for fmt in formats:
        try:
            resp = await _chat(client, upstream, api_key, model, messages, fmt)
            content = resp["choices"][0]["message"]["content"] or ""
            parsed = _first_json(content)
            return _normalise_terms(parsed)
        except Exception as e:  # noqa: BLE001 — try the next format
            last_err = e
    raise RuntimeError(f"forecast parse failed on all formats: {last_err}")


def _normalise_terms(parsed: dict) -> dict:
    out: dict = {}
    for k in ("calls", "input_tokens", "output_tokens"):
        t = parsed.get(k) or {}
        val = t.get("value")
        if val is None:
            continue
        out[k] = {"value": int(val), "source": t.get("source", "assumed"), "why": t.get("why", "")}
    for k in ("model", "window"):
        t = parsed.get(k) or {}
        if t.get("value"):
            out[k] = {"value": str(t["value"]), "source": t.get("source", "assumed"), "why": t.get("why", "")}
    return out


# ---------------- diagnosis ----------------

def build_evidence(job: dict, rows: list[dict], verdict) -> dict:
    """Numbers only. This is what the diagnosis model reads — and what `report` shows even
    when no model is reachable."""
    comp = [r["completion_tokens"] for r in rows if r.get("completion_tokens") is not None]
    reas = [(r["reasoning_tokens"], r["completion_tokens"]) for r in rows
            if r.get("reasoning_tokens") is not None and r.get("completion_tokens")]
    cached = [(r["cached_tokens"], r["prompt_tokens"]) for r in rows
              if r.get("cached_tokens") is not None and r.get("prompt_tokens")]
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[str(r.get("status"))] = statuses.get(str(r.get("status")), 0) + 1
    models = sorted({r["model"] for r in rows if r.get("model")})
    metered = [r for r in rows if (r.get("status") or 0) < 400 and r.get("completion_tokens") is not None]
    truncated = sum(1 for r in metered if r.get("finish_reason") == "length")
    ev = {
        "hypotheses": [],  # filled below: what the arithmetic already suggests
        "job": job["id"],
        "sentence": job.get("sentence"),
        "budget_usd": job.get("budget_usd"),
        "forecast": job.get("terms"),
        "n_calls": len(rows),
        "models_seen": models,
        "status_counts": statuses,
        "reasoning_share": (sum(a for a, _ in reas) / sum(b for _, b in reas)) if reas else None,
        "reasoning_estimated": any(r.get("reasoning_estimated") for r in rows),
        "reasoning_coverage": (len(reas) / len(comp)) if comp else None,
        "cached_input_share": (sum(a for a, _ in cached) / sum(b for _, b in cached)) if cached else None,
        "latency_ms_mean": (sum(lat) / len(lat)) if lat else None,
        "truncated_share": (truncated / len(metered)) if metered else None,
        "usage_estimated_calls": sum(1 for r in rows if r.get("usage_estimated")),
        "errors": verdict.errors,
        "unmetered": verdict.unmetered,
        "unpriced_calls": verdict.unpriced_calls,
        "spent_usd": verdict.spent_usd,
        "expected_usd": verdict.expected_usd,
        "projected_usd": verdict.projected_usd,
        "breached_term": verdict.term,
        "terms": {
            k: {"forecast": t.forecast, "observed": t.observed, "recent": t.recent, "ratio": t.ratio, "breached": t.breached}
            for k, t in verdict.terms.items()
        },
    }
    ev["hypotheses"] = hypotheses(ev)
    return ev


def hypotheses(ev: dict) -> list[str]:
    """What the numbers alone say. The diagnosis model starts from these and confirms or refutes."""
    hs: list[str] = []
    term = ev.get("breached_term")
    rs = ev.get("reasoning_share")
    if term == "output_tokens" and rs is not None and rs >= 0.5:
        hs.append(f"{rs:.0%} of billed completion tokens are a reasoning trace: the model thinks before answering and the "
                  "trace is billed as output. Fix: disable thinking (chat_template_kwargs.enable_thinking=false on vLLM/Token Factory, "
                  "reasoning_effort='none' on Ollama) or forecast the trace.")
    ts = ev.get("truncated_share")
    if ts:
        hs.append(f"{ts:.0%} of answers hit max_tokens (finish_reason=length): the cap is bounding cost by cutting answers.")
    fc_model = ((ev.get("forecast") or {}).get("model") or {}).get("value")
    seen = ev.get("models_seen") or []
    if fc_model and seen and all(m != fc_model and m.split("/")[-1] != fc_model.split("/")[-1] for m in seen):
        hs.append(f"responses came from {seen}, not the forecast model {fc_model!r}: an alias or a swapped model.")
    if term == "input_tokens":
        hs.append("input tokens per call exceed the forecast: longer items than the sample, a tools/system preamble, or growing context.")
    errs = ev.get("errors") or {}
    if errs:
        hs.append(f"upstream errors during the run: {errs} (not counted as calls).")
    if not hs and term:
        hs.append(f"{term} exceeded the forecast; nothing in the usage numbers singles out a mechanism.")
    return hs


DIAG_SYSTEM = (
    "You are Offby's diagnosis engine for an LLM batch job that was halted because observed usage "
    "broke the user's forecast. You get only numbers (usage objects, never prompts), plus a list of "
    "'hypotheses' the arithmetic already supports. Start from the hypotheses: confirm the one the numbers "
    "back, or refute it with a specific number. Do not restate the forecast as a fix. "
    "Answer in at most 120 words, plain text, three labelled lines: "
    "MECHANISM: the single most likely cause of the broken term, citing the numbers (reasoning_share, truncated_share, models_seen, ratios). "
    "UNKNOWN: the top thing the user never stated that matters here. "
    "FIX: one concrete change to the REQUEST (a parameter such as enable_thinking/reasoning_effort/max_tokens, or a model id) "
    "or an accepted new forecast value, and the projected total after it as arithmetic from the evidence."
)


async def diagnose(client: httpx.AsyncClient, upstream: str, api_key: str, evidence: dict, model: str = DIAG_MODEL) -> str:
    messages = [
        {"role": "system", "content": DIAG_SYSTEM},
        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False, default=str)},
    ]
    resp = await _chat(client, upstream, api_key, model, messages, max_tokens=1200)
    return _strip_think(resp["choices"][0]["message"]["content"] or "")
