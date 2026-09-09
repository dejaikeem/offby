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
    u = U.from_response({"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 200},
                         "choices": [{"message": {"content": "<think>" + "x" * 400 + "</think>answer"}}]})
    assert u.reasoning_tokens == 100 and u.reasoning_estimated is True


def test_estimated_reasoning_never_exceeds_billed_completion():
    u = U.from_response({"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                         "choices": [{"message": {"content": "<think>" + "x" * 400 + "</think>answer"}}]})
    assert u.reasoning_tokens == 20 and u.reasoning_estimated is True


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


def test_sse_without_usage_is_estimated_from_text():
    u = U.from_sse('data: {"model":"m","choices":[{"delta":{"content":"' + "x" * 400 + '"}}]}\ndata: [DONE]')
    assert u.estimated and u.completion_tokens == 100 and u.prompt_tokens is None


def test_sse_without_usage_or_text_is_none():
    assert U.from_sse('data: {"model":"m","choices":[{"delta":{}}]}\ndata: [DONE]') is None


def test_midstream_error_chunk_sets_error_status():
    u = U.from_sse('data: {"model":"m","choices":[{"delta":{"content":"hi"}}]}\ndata: {"error":{"message":"boom","code":503}}')
    assert u.error_status == 503


def test_finish_reason_and_responses_api_keys():
    u = U.from_response({"model": "m", "usage": {"input_tokens": 10, "output_tokens": 20},
                         "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}]})
    assert u.prompt_tokens == 10 and u.completion_tokens == 20 and u.finish_reason == "length"


def test_error_body_without_usage_is_error_status():
    u = U.from_response({"error": {"message": "rate", "code": 429}})
    assert u.error_status == 429 and u.completion_tokens is None
