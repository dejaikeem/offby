from offby import usage as U


def test_reasoning_from_details():
    u = U.from_response({"model": "m", "usage": {"prompt_tokens": 402, "completion_tokens": 2180,
                                                 "completion_tokens_details": {"reasoning_tokens": 1905}},
                         "choices": [{"message": {"content": "ok"}}]})
    assert u.prompt_tokens == 402 and u.completion_tokens == 2180
    assert u.reasoning_tokens == 1905 and u.reasoning_estimated is False


def test_reasoning_null_stays_none_without_evidence():
    u = U.from_response({"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                         "choices": [{"message": {"content": "ok"}}]})
    assert u.reasoning_tokens is None and u.reasoning_estimated is False


def test_reasoning_estimated_from_think_tag():
    u = U.from_response({"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                         "choices": [{"message": {"content": "<think>" + "x" * 400 + "</think>answer"}}]})
    assert u.reasoning_tokens == 100 and u.reasoning_estimated is True


def test_no_usage_returns_none():
    assert U.from_response({"choices": []}) is None


def test_sse_usage_chunk():
    body = "\n".join([
        'data: {"model":"m","choices":[{"delta":{"content":"hi"}}]}',
        'data: {"model":"m","choices":[{"delta":{}}],"usage":{"prompt_tokens":5,"completion_tokens":7,'
        '"completion_tokens_details":{"reasoning_tokens":3}}}',
        "data: [DONE]",
    ])
    u = U.from_sse(body)
    assert u.model == "m" and u.completion_tokens == 7 and u.reasoning_tokens == 3


def test_sse_without_usage_is_none():
    assert U.from_sse('data: {"model":"m","choices":[{"delta":{"content":"hi"}}]}\ndata: [DONE]') is None
