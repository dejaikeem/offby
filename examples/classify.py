"""A batch job that looks like the README scenario: classify N reviews with a
short prompt and paragraph answers. Point it at Offby and watch the referee.

  OPENAI_BASE_URL=http://localhost:8402/j/<job>/v1 OPENAI_API_KEY=... \
    python examples/classify.py --data examples/reviews.jsonl --concurrency 8 [--no-think]
  (or --n 200 for synthetic rows)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import openai

REVIEWS = [
    "Battery lasts two days, build feels premium, but the price stings.",
    "Screen cracked in a week. Support was slow. Would not buy again.",
    "Does exactly what it says. Setup took five minutes.",
    "Louder than expected and the app crashes on login.",
    "Great value for the money, shipping was fast.",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200, help="synthetic rows when --data is not given")
    ap.add_argument("--data", help="JSONL with a 'text' field; one call per line")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--model", default=os.environ.get("OFFBY_JOB_MODEL", "mock/nemotron-3-nano-30b-a3b"),
                    help="the mock id by default; pass the real id (or set OFFBY_JOB_MODEL) for a real upstream")
    ap.add_argument("--no-think", action="store_true", help="send chat_template_kwargs.enable_thinking=false")
    ap.add_argument("--stream", action="store_true")
    args = ap.parse_args()

    if args.data:
        texts = [json.loads(ln)["text"] for ln in open(args.data) if ln.strip()]
    else:
        texts = [REVIEWS[i % len(REVIEWS)] for i in range(args.n)]
    # the SDK retries 429/5xx on its own and never retries a 402, so Offby's halt is seen immediately
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "offby-mock"), max_retries=2)
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if args.no_think else None

    def one(i: int):
        review = texts[i]
        kw = dict(model=args.model, max_tokens=4000,
                  messages=[{"role": "system", "content": "Classify the review sentiment and justify in one paragraph."},
                            {"role": "user", "content": review}])
        if extra:
            kw["extra_body"] = extra
        if args.stream:
            out = ""
            for chunk in client.chat.completions.create(stream=True, **kw):
                if chunk.choices and chunk.choices[0].delta.content:
                    out += chunk.choices[0].delta.content
            return i, out
        r = client.chat.completions.create(**kw)
        return i, r.choices[0].message.content or ""

    done = 0
    ex = ThreadPoolExecutor(max_workers=args.concurrency)
    futs = [ex.submit(one, i) for i in range(len(texts))]
    try:
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(texts)} classified", flush=True)
    except openai.APIStatusError as e:
        if e.status_code == 402 and e.response.headers.get("x-offby-halt"):
            err = e.body if isinstance(e.body, dict) else {}
            err = err.get("error", err)  # SDK versions differ on whether the inner object is unwrapped
            print(f"\nopenai.APIStatusError: 402 offby: {err.get('message')}", file=sys.stderr)
            print(f"  resume:    {err.get('resume')}", file=sys.stderr)
            print(f"  diagnosis: {err.get('diagnosis')}", file=sys.stderr)
            return 3
        print(f"\nupstream error after {done} rows: {e}", file=sys.stderr)
        return 1
    finally:
        ex.shutdown(cancel_futures=True)  # any error stops the queue — never keep burning on a dead run
    print(f"done: {done}/{len(texts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
