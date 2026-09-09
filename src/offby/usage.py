"""Read the `usage` object off OpenAI-compatible responses (JSON or SSE).

Rules: reasoning tokens are unknown until observed — if completion_tokens_details is
absent we may ESTIMATE from reasoning_content / <think> text and flag it, never coerce
null to 0. A streamed response that never carries a usage chunk is not silently
dropped either: we estimate the completion from the streamed text and flag
`estimated`, or report `None` so the proxy can count it as unmetered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)


@dataclass
class Usage:
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None = None
    reasoning_estimated: bool = False
    cached_tokens: int | None = None
    finish_reason: str | None = None
    estimated: bool = False  # no usage object: tokens estimated from content
    error_status: int | None = None  # a top-level error arrived (mid-stream or in body)


def _rough_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _reasoning_from_details(usage: dict) -> int | None:
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        return int(details["reasoning_tokens"])
    return None


def _cached_from_details(usage: dict) -> int | None:
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details["cached_tokens"])
    return None


def estimate_reasoning(choices) -> int | None:
    """Fallback: reasoning_content field or <think>…</think> in content. Rough char/4 count."""
    if not isinstance(choices, list):
        return None
    total = 0
    found = False
    for ch in choices:
        msg = (ch or {}).get("message") or (ch or {}).get("delta") or {}
        rc = msg.get("reasoning_content") or msg.get("reasoning")
        if isinstance(rc, str) and rc:
            total += _rough_tokens(rc)
            found = True
        content = msg.get("content")
        if isinstance(content, str):
            for m in THINK_RE.finditer(content):
                total += _rough_tokens(m.group(1))
                found = True
    return total if found else None


def _error_status(body: dict) -> int | None:
    err = body.get("error")
    if not isinstance(err, dict):
        return None
    code = err.get("code") or err.get("status")
    return code if isinstance(code, int) and 400 <= code < 600 else 500


def from_response(body: dict) -> Usage | None:
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    choices = body.get("choices")
    finish = None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    if not isinstance(usage, dict):
        err = _error_status(body)
        return Usage(body.get("model"), None, None, error_status=err) if err else None
    reasoning, estimated = _reasoning_from_details(usage), False
    if reasoning is None:
        est = estimate_reasoning(choices)
        if est is not None:
            reasoning, estimated = est, True
    # input_tokens/output_tokens: Responses-API and Anthropic-shaped usage objects
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if estimated and reasoning is not None and isinstance(completion, int):
        reasoning = min(reasoning, completion)  # chars/4 overshoots; the trace cannot exceed what was billed
    return Usage(
        model=body.get("model"),
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        reasoning_estimated=estimated,
        cached_tokens=_cached_from_details(usage),
        finish_reason=finish,
    )


def from_sse(text: str) -> Usage | None:
    """Scan an SSE body for the chunk carrying `usage` (stream_options.include_usage).
    Without one, estimate completion tokens from the streamed text and flag `estimated`."""
    model, usage_chunk, finish, error = None, None, None, None
    content_chars = reasoning_chars = 0
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        model = model or obj.get("model")
        error = error or _error_status(obj)
        for ch in obj.get("choices") or []:
            delta = (ch or {}).get("delta") or {}
            rc = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(rc, str):
                reasoning_chars += len(rc)
            c = delta.get("content")
            if isinstance(c, str):
                content_chars += len(c)
            finish = (ch or {}).get("finish_reason") or finish
        if isinstance(obj.get("usage"), dict):
            usage_chunk = obj
    if usage_chunk is not None:
        u = from_response({"model": model, "usage": usage_chunk["usage"], "choices": []})
        if u and u.reasoning_tokens is None and reasoning_chars:
            est = max(1, reasoning_chars // 4)
            u.reasoning_tokens = min(est, u.completion_tokens) if isinstance(u.completion_tokens, int) else est
            u.reasoning_estimated = True
        if u:
            u.finish_reason, u.error_status = finish, error
        return u
    if error:
        return Usage(model, None, None, error_status=error, finish_reason=finish)
    if content_chars or reasoning_chars:
        return Usage(model, None, max(1, (content_chars + reasoning_chars) // 4),
                     reasoning_tokens=(max(1, reasoning_chars // 4) if reasoning_chars else None),
                     reasoning_estimated=bool(reasoning_chars), finish_reason=finish, estimated=True)
    return None
