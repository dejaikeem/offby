"""Read the `usage` object off OpenAI-compatible responses (JSON or SSE).

Rule: reasoning tokens are unknown until observed. If completion_tokens_details is
absent we may ESTIMATE from reasoning_content / <think> text and flag it — never
silently coerce null to 0.
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


def from_response(body: dict) -> Usage | None:
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    reasoning, estimated = _reasoning_from_details(usage), False
    if reasoning is None:
        est = estimate_reasoning(body.get("choices"))
        if est is not None:
            reasoning, estimated = est, True
    return Usage(
        model=body.get("model"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=reasoning,
        reasoning_estimated=estimated,
        cached_tokens=_cached_from_details(usage),
    )


def from_sse(text: str) -> Usage | None:
    """Scan an SSE body for the chunk carrying `usage` (stream_options.include_usage)."""
    model, usage_chunk = None, None
    reasoning_chars = 0
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
        model = model or obj.get("model")
        for ch in obj.get("choices") or []:
            delta = (ch or {}).get("delta") or {}
            rc = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(rc, str):
                reasoning_chars += len(rc)
        if isinstance(obj.get("usage"), dict):
            usage_chunk = obj
    if usage_chunk is None:
        return None
    u = from_response({"model": model, "usage": usage_chunk["usage"], "choices": []})
    if u and u.reasoning_tokens is None and reasoning_chars:
        u.reasoning_tokens, u.reasoning_estimated = max(1, reasoning_chars // 4), True
    return u
