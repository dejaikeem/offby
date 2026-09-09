"""offby — job ensure | forecast | serve | report | accept | lessons | jobs | mock"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from statistics import fmean

import httpx

from . import judge as J
from . import tf
from .store import Store

BOLD, DIM, RED, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _c(code: str, s: str) -> str:
    return f"{code}{s}{RESET}" if sys.stdout.isatty() else s


def _table(rows: list[list[str]], header: list[str]) -> str:
    cols = list(zip(header, *rows))
    widths = [max(len(str(x)) for x in col) for col in cols]
    fmt = lambda r: "  " + "  ".join(str(x).ljust(w) for x, w in zip(r, widths))  # noqa: E731
    return "\n".join([fmt(header), fmt(["-" * w for w in widths]), *(fmt(r) for r in rows)])


def _usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


def _parse_price(s: str | None) -> tuple[float, float] | None:
    if not s:
        return None
    a, b = s.split(",")
    return float(a), float(b)


def _key(args) -> str | None:
    return getattr(args, "api_key", None) or os.environ.get(tf.API_KEY_ENV)


# ---------------- terms from CLI / sentence ----------------

def _terms_from_args(args) -> dict | None:
    """Sentence (nano-parsed) and/or explicit flags → terms dict. Returns None on a hard error
    (already printed). May return {} when nothing was given."""
    terms: dict = {}
    key = _key(args)
    upstream = args.upstream

    if getattr(args, "sentence", None):
        if not key:
            print(_c(RED, f"a sentence needs a model to parse it: set {tf.API_KEY_ENV} or pass --api-key, "
                          "or give the terms directly (--calls/--input/--output/--model)."))
            return None

        async def _parse():
            async with httpx.AsyncClient(timeout=60) as client:
                return await tf.parse_forecast(client, upstream, key, args.sentence)
        try:
            terms = asyncio.run(_parse())
        except Exception as e:  # noqa: BLE001
            print(_c(RED, f"forecast parse failed: {e}"))
            return None

    for name, val in (("calls", args.calls), ("input_tokens", args.input), ("output_tokens", args.output)):
        if val is not None:
            terms[name] = {"value": val, "source": "stated"}
    if args.model:
        terms["model"] = {"value": args.model, "source": "stated"}
    if args.window:
        terms["window"] = {"value": args.window, "source": "stated"}

    model = J.term_value(terms, "model")
    price = _parse_price(args.price)
    if price:
        terms["price"] = {"in_per_m": price[0], "out_per_m": price[1], "source": "stated", "model": model}
    elif model:
        async def _prices():
            async with httpx.AsyncClient(timeout=30) as client:
                return await tf.fetch_prices(client, upstream, key)
        try:
            found = tf.price_for(asyncio.run(_prices()), model)
        except Exception as e:  # noqa: BLE001
            found = None
            print(_c(YELLOW, f"price oracle unavailable ({e}); model stays UNPRICED"))
        if found:
            terms["price"] = {"in_per_m": found[0], "out_per_m": found[1], "source": "oracle", "model": model}
    return terms


def _confirm_assumed(terms: dict, args) -> None:
    assumed = [k for k, t in terms.items() if t.get("source") == "assumed"]
    if assumed and not args.yes and sys.stdin.isatty():
        ans = input(f"\n  {len(assumed)} ASSUMED term(s) are Offby's guesses. Enter to accept, or type fixes like output_tokens=300: ").strip()
        if ans:
            for k, v in _kv(ans).items():
                _apply(terms, k, v, source="stated")
            _print_terms(terms, args.budget)


def _print_terms(terms: dict, budget: float | None) -> None:
    rows = []
    for k, label in (("calls", "calls"), ("input_tokens", "input tok/call"), ("output_tokens", "output tok/call")):
        t = terms.get(k)
        if t:
            src = t["source"].upper() if t["source"] == "assumed" else t["source"]
            why = f" ← {t['why']}" if t.get("why") else ""
            rows.append([label, f"{t['value']:,}", f"{src}{why}"])
    p = terms.get("price")
    if p:
        rows.append(["price $/M", f"{p['in_per_m']:g} / {p['out_per_m']:g}", f"{p['source']} ({p.get('model') or '?'})"])
    else:
        rows.append(["price $/M", _c(RED, "UNPRICED"), "no price for this model — pass --price in,out"])
    for k in ("model", "window"):
        t = terms.get(k)
        if t:
            rows.append([k, t["value"], t["source"]])
    total = J.forecast_total(terms)
    if total is not None:
        ok = "" if budget is None else (" ✓" if total <= budget else _c(RED, f"  > budget ${budget:g}"))
        rows.append(["expected total", _usd(total), (f"budget ${budget:g}{ok}" if budget is not None else "")])
    print()
    print(_table(rows, ["term", "forecast", "source"]))


def _kv(s: str) -> dict:
    out = {}
    for part in s.replace(",", " ").split():
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _apply(terms: dict, k: str, v: str, *, source: str) -> None:
    if k in ("calls", "input_tokens", "output_tokens"):
        terms[k] = {"value": int(float(v)), "source": source}
    elif k == "price":
        parts = re.split(r"[/:,]", v)
        if len(parts) != 2:
            raise SystemExit(f"price needs two numbers like price=0.06/0.24 (got {v!r})")
        terms["price"] = {"in_per_m": float(parts[0]), "out_per_m": float(parts[1]), "source": source, "model": J.term_value(terms, "model")}
    elif k in ("model", "window"):
        terms[k] = {"value": v, "source": source}
    else:
        raise SystemExit(f"unknown term {k!r} (calls, input_tokens, output_tokens, price=in/out, model, window)")


def _base_url(port: int, job_id: str) -> str:
    return f"http://localhost:{port}/j/{job_id}/v1"


# ---------------- job ensure / forecast ----------------

def cmd_job_ensure(args) -> int:
    """Named job. First time: create. Again: start a new run (counters and state reset, history kept)."""
    if not NAME_RE.match(args.name):
        print(_c(RED, f"job name must match {NAME_RE.pattern} (lowercase, digits, . _ -)"))
        return 2
    store = Store(args.db)
    terms = _terms_from_args(args)
    if terms is None:
        return 2
    existing = store.get_job(args.name)
    if not terms and not args.baseline and (existing is None or not existing["terms"]):
        print(_c(RED, "a new job needs a forecast: --calls/--input/--output/--model, a sentence, or --baseline N"))
        return 2
    if terms:
        _print_terms(terms, args.budget)
        _confirm_assumed(terms, args)
    job, created = store.ensure_job(args.name, terms or None, sentence=getattr(args, "sentence", None), budget_usd=args.budget,
                                    baseline_n=args.baseline, min_calls=args.min_calls, threshold=args.threshold)
    if created:
        print(f"\n  job {_c(BOLD, args.name)} created" + (f" in baseline mode (first {args.baseline} calls)" if args.baseline else ""))
    else:
        print(f"\n  job {_c(BOLD, args.name)}: new run started ({job['run_from']:,} calls in history"
              + (", forecast updated)" if terms else ", forecast kept)"))
        if not terms:
            _print_terms(job["terms"], job.get("budget_usd"))
    print(f"  base_url → {_base_url(args.port, args.name)}")
    print("  " + _c(DIM, f"OPENAI_BASE_URL={_base_url(args.port, args.name)} python your_job.py"))
    return 0


def cmd_forecast(args) -> int:
    """One-off job with a random id (the original CLI). `job ensure` is the named form."""
    store = Store(args.db)
    if args.baseline:
        job_id = f"j_{uuid.uuid4().hex[:4]}"
        store.create_job(job_id, {}, budget_usd=args.budget, baseline_n=args.baseline,
                         min_calls=max(args.min_calls, args.baseline), threshold=args.threshold)
        print(f"job {job_id} created in baseline mode: first {args.baseline} calls become the forecast.")
        print(f"base_url → {_base_url(args.port, job_id)}")
        return 0
    terms = _terms_from_args(args)
    if terms is None:
        return 2
    if not any(k in terms for k in ("calls", "input_tokens", "output_tokens")):
        print(_c(RED, "nothing to forecast: give a sentence, or --calls/--input/--output, or --baseline N"))
        return 2
    _print_terms(terms, args.budget)
    _confirm_assumed(terms, args)
    job_id = f"j_{uuid.uuid4().hex[:4]}"
    store.create_job(job_id, terms, sentence=args.sentence, budget_usd=args.budget,
                     min_calls=args.min_calls, threshold=args.threshold)
    print(f"\n  job {job_id} created. base_url → {_base_url(args.port, job_id)}")
    print("  " + _c(DIM, f"OPENAI_BASE_URL={_base_url(args.port, job_id)} python your_job.py"))
    return 0


# ---------------- report ----------------

def _verdict(store: Store, job: dict) -> J.Verdict:
    rows = store.calls(job["id"])
    calls = [J.Call(r["prompt_tokens"], r["completion_tokens"], r["in_per_m"], r["out_per_m"],
                    status=r.get("status") or 200, estimated=bool(r.get("usage_estimated")),
                    price_source=r.get("price_source") or ("oracle" if r["in_per_m"] is not None else None)) for r in rows]
    return J.judge(job["terms"], calls, min_calls=job["min_calls"], threshold=job["threshold"],
                   since=job.get("judge_from") or 0, run_since=job.get("run_from") or 0)


def cmd_report(args) -> int:
    store = Store(args.db)
    job = store.get_job(args.job)
    if not job:
        print(_c(RED, f"no job {args.job}"))
        return 1
    v = _verdict(store, job)
    state = job["state"].upper()
    print(f"\n  {_c(BOLD, job['id'])}  {_c(RED if state == 'HALTED' else GREEN, state)}"
          + (f"  halted on {job['halted_term']}" if job.get("halted_term") else "")
          + (f"\n  {_c(DIM, job['sentence'])}" if job.get("sentence") else ""))
    if job.get("baseline_n") and not job["terms"]:
        print(f"  baseline mode: {v.n}/{job['baseline_n']} calls observed, forecast not set yet")
    table = []
    for name in ("calls", "input_tokens", "output_tokens", "price"):
        t = v.terms.get(name)
        if not t:
            continue
        fc = "—" if t.forecast is None else ("1.0x" if name == "price" else f"{t.forecast:,g}")
        obs = "—" if t.observed is None else (f"{t.observed:.2f}x" if name == "price" else f"{t.observed:,.0f}")
        rec = "—" if t.recent is None else (f"{t.recent:.2f}x" if name == "price" else f"{t.recent:,.0f}")
        ratio = "—" if t.ratio is None else f"{t.ratio:.2f}x"
        conf = "—" if t.t is None else (">99" if t.t >= J.T_CAP else ("<-99" if t.t <= -J.T_CAP else f"{t.t:.1f}"))
        if name == "calls":
            conf = ""
        status = _c(RED, "BREACH") if t.breached else ("—" if t.ratio is None else _c(GREEN, "ok"))
        table.append([name, fc, obs, rec, ratio, conf, status])
    print()
    print(_table(table, ["term", "forecast", "observed", "last 5", "ratio", "t", ""]))
    money = f"  spent {_usd(v.spent_usd)}"
    if v.expected_usd is not None:
        money += f"  expected {_usd(v.expected_usd)}"
    if v.projected_usd is not None:
        money += f"  projected {_usd(v.projected_usd)}"
    if v.unpriced_calls:
        money += _c(RED, f"  UNPRICED calls: {v.unpriced_calls}")
    if v.forecast_priced:
        money += _c(YELLOW, f"  (priced at the forecast rate — upstream lists no price; {v.forecast_priced} calls)")
    since = f", judging the last {v.judged}" if v.judged != v.valid else ""
    hist = f"  ({v.total:,} in history)" if v.total != v.n else ""
    print(f"\n  calls this run {v.n}, with usage {v.valid}{since}{hist}  "
          f"(verdict from {v.min_calls} valid calls, threshold {v.threshold:g}x, confidence t>{J.Z:g})")
    if v.errors or v.unmetered or v.estimated:
        parts = []
        if v.errors:
            parts.append("failed calls: " + ", ".join(f"{n} ({code})" for code, n in sorted(v.errors.items())))
        if v.unmetered:
            parts.append(f"no usage object: {v.unmetered}")
        if v.estimated:
            parts.append(f"tokens estimated from streamed text: {v.estimated}")
        print("  " + _c(YELLOW, " · ".join(parts)))
    print(money)
    ev = job.get("evidence") or {}
    if ev:
        rs = ev.get("reasoning_share")
        line = "  evidence:" if job["state"] == "halted" else "  last halt evidence:"
        if rs is not None:
            line += f" reasoning share of completion {rs:.0%}{' (estimated)' if ev.get('reasoning_estimated') else ''};"
        ts = ev.get("truncated_share")
        if ts:
            line += f" answers cut by max_tokens {ts:.0%};"
        if ev.get("models_seen"):
            line += f" models {', '.join(ev['models_seen'])};"
        if ev.get("status_counts"):
            line += f" statuses {ev['status_counts']}"
        print(line)
    if job.get("diagnosis_status"):
        print(f"\n  diagnosis [{job['diagnosis_status']}]:")
        for ln in (job.get("diagnosis") or "").splitlines():
            print(f"    {ln}")
    if job["state"] == "halted":
        term = job.get("halted_term")
        tv = v.terms.get(term) if term else None
        sug = J.suggest_accept(tv) if tv else None
        if job.get("diagnosis_status") == "pending":
            print("\n  diagnosis pending — run `offby report` again in a moment")
        elif sug is not None:
            print(f"\n  resume: offby accept {job['id']} {term}={sug}   (or fix the cause and: offby accept {job['id']})")
        else:
            print(f"\n  resume: offby accept {job['id']}")
    print()
    return 0


# ---------------- accept ----------------

def cmd_accept(args) -> int:
    store = Store(args.db)
    job = store.get_job(args.job)
    if not job:
        print(_c(RED, f"no job {args.job}"))
        return 1
    terms = job["terms"]
    for pair in args.terms:
        if "=" not in pair:
            raise SystemExit(f"expected term=value, got {pair!r}")
        k, v = pair.split("=", 1)
        _apply(terms, k, v, source="accepted")
    store.resume(args.job, terms)
    print(f"  {args.job} resumed" + (f" with {' '.join(args.terms)}" if args.terms else " (terms unchanged; judging restarts from the next call)"))
    _print_terms(terms, job.get("budget_usd"))
    return 0


# ---------------- lessons ----------------

def cmd_lessons(args) -> int:
    """What the meter has learned across every job — for an agent (or a person) to read before
    writing the next forecast. Usage numbers only; nothing here came from a prompt."""
    store = Store(args.db)
    jobs = store.list_jobs()
    if not jobs:
        print("  no jobs yet — nothing learned.")
        return 0
    per_model: dict[str, dict] = {}
    for j in jobs:
        for r in store.calls(j["id"]):
            if not r.get("model") or r.get("completion_tokens") is None:
                continue
            m = per_model.setdefault(r["model"], {"calls": 0, "think_out": [], "nothink_out": [], "in": [], "price": None})
            m["calls"] += 1
            m["in"].append(r["prompt_tokens"] or 0)
            (m["think_out"] if (r.get("reasoning_tokens") or 0) > 0 else m["nothink_out"]).append(r["completion_tokens"])
            if r.get("in_per_m") is not None:
                m["price"] = (r["in_per_m"], r["out_per_m"], r.get("price_source") or "oracle")
    total_calls = sum(m["calls"] for m in per_model.values())
    print(f"\n  offby lessons — {len(jobs)} job(s), {total_calls:,} metered call(s)\n")
    print("  MODELS (from usage objects; 'thinking' = reasoning_tokens > 0)")
    for mid, m in sorted(per_model.items(), key=lambda kv: -kv[1]["calls"]):
        price = (f"${m['price'][0]:g}/{m['price'][1]:g} per M" + (" (forecast price)" if m["price"][2] == "forecast" else "")) if m["price"] else "UNPRICED"
        line = f"  - {mid}: {m['calls']:,} calls, {price}, input mean {fmean(m['in']):.0f}"
        if m["think_out"]:
            line += f", output mean {fmean(m['think_out']):.0f} with thinking ({len(m['think_out'])} calls)"
        if m["nothink_out"]:
            line += f", {fmean(m['nothink_out']):.0f} without ({len(m['nothink_out'])} calls)"
        if m["think_out"] and m["nothink_out"]:
            line += f" → thinking multiplies output ≈{fmean(m['think_out']) / max(1, fmean(m['nothink_out'])):.1f}x"
        if m["think_out"] and not m["nothink_out"]:
            line += " → every call so far carried a reasoning trace; the model thinks by default unless told not to"
        if mid.startswith("mock/"):
            line += "   [mock upstream — constants, not measurements]"
        print(line)
    print("\n  BREACHES (latest per job)")
    any_breach = False
    for j in jobs:
        ev = j.get("evidence") or {}
        term = ev.get("breached_term") or j.get("halted_term")
        if not term:
            continue
        any_breach = True
        t = (ev.get("terms") or {}).get(term, {})
        accepted = [f"{k}={v.get('value')}" for k, v in (j.get("terms") or {}).items() if isinstance(v, dict) and v.get("source") == "accepted"]
        line = f"  - {j['id']}: {term} {t.get('ratio', 0):.1f}x ({t.get('forecast')}→{(t.get('observed') or 0):.0f})"
        line += f", now {j['state']}"
        if accepted:
            line += f", accepted {' '.join(accepted)}"
        elif j["state"] == "running":
            line += ", resumed after a fix"
        diag = (j.get("diagnosis") or "").strip().splitlines()
        fix = next((ln for ln in diag if ln.upper().startswith("FIX")), None)
        if fix:
            line += f"\n      {fix}"
        print(line)
    if not any_breach:
        print("  - none")
    print("\n  RULES OF THUMB")
    print("  - Forecast output tokens for the model's *billed* output. If a model thinks by default, the trace is billed as output.")
    print("  - Never quote a price from memory: the proxy reads /v1/models live; UNPRICED means it could not.")
    print("  - A job halts when BOTH the overall mean and the last-5 mean of a term exceed forecast × threshold (default 2x), from call 10.")
    print()
    return 0


def cmd_jobs(args) -> int:
    store = Store(args.db)
    rows = []
    for j in store.list_jobs():
        n = store.count_calls(j["id"])
        run = n - (j.get("run_from") or 0)
        rows.append([j["id"], j["state"], f"{run}/{n}", j.get("halted_term") or "", (j.get("sentence") or "")[:60]])
    print(_table(rows, ["job", "state", "calls run/total", "halted on", "sentence"]) if rows else "  no jobs yet")
    return 0


# ---------------- serve / mock ----------------

def cmd_serve(args) -> int:
    import uvicorn
    from .proxy import create_app
    app = create_app(args.upstream, api_key=_key(args), fail_open=args.fail_open, db_path=args.db)
    print(f"offby proxy on http://{args.host}:{args.port}  →  {args.upstream}"
          + ("  (fail-open)" if args.fail_open else ""), flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_mock(args) -> int:
    import uvicorn
    from .mock import PROFILES, create_mock
    kw = dict(model=args.model, output_tokens=args.output_tokens, reasoning_share=args.reasoning_share,
              output_tokens_off=args.output_tokens_off, price=_parse_price(args.price) or (0.06, 0.24),
              latency_ms=args.latency_ms, tail_sigma_on=args.tail_on, tail_sigma_off=args.tail_off,
              error_rate=args.error_rate, error_codes=tuple(int(c) for c in args.error_codes.split(",")),
              ignore_include_usage=args.ignore_include_usage, reasoning_report=args.reasoning_report,
              respond_model=args.respond_model, pricing=args.pricing, honor_max_tokens=not args.ignore_max_tokens,
              seed=args.seed)
    for k, v in PROFILES[args.profile].items():
        kw[k] = v
    app = create_mock(**kw)
    print(f"mock upstream on http://{args.host}:{args.port}/v1  profile={args.profile} model={kw['model']} "
          f"output≈{args.output_tokens}×lognormal(σ={kw['tail_sigma_on']}) think-off≈{args.output_tokens_off} "
          f"latency≈{kw['latency_ms']:g}ms errors={kw['error_rate']:.0%} reasoning={kw['reasoning_report']} "
          f"pricing={kw['pricing']}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------- main ----------------

def _forecast_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("sentence", nargs="?", help="casual forecast sentence; needs an API key to parse")
    p.add_argument("--budget", type=float)
    p.add_argument("--calls", type=int)
    p.add_argument("--input", type=int, help="input tokens per call")
    p.add_argument("--output", type=int, help="output tokens per call")
    p.add_argument("--model")
    p.add_argument("--price", help="in,out in $/M tokens (overrides the oracle)")
    p.add_argument("--window")
    p.add_argument("--baseline", type=int, help="no forecast: first N calls become the reference")
    p.add_argument("--min-calls", type=int, default=J.MIN_CALLS)
    p.add_argument("--threshold", type=float, default=J.THRESHOLD)
    p.add_argument("--upstream", default=tf.DEFAULT_UPSTREAM)
    p.add_argument("--api-key")
    p.add_argument("--port", type=int, default=8402, help="proxy port, for the printed base_url")
    p.add_argument("-y", "--yes", action="store_true", help="skip the ASSUMED confirmation")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="offby", description="Forecast referee for LLM batch jobs.")
    p.add_argument("--db", default=None, help="SQLite path (default ~/.offby/offby.sqlite or $OFFBY_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    jb = sub.add_parser("job", help="named jobs")
    jbs = jb.add_subparsers(dest="jobcmd", required=True)
    je = jbs.add_parser("ensure", help="create the named job, or start a new run of it")
    je.add_argument("name", help="lowercase, digits, . _ -  (goes into the URL: /j/<name>/v1)")
    _forecast_flags(je)
    je.set_defaults(fn=cmd_job_ensure)

    f = sub.add_parser("forecast", help="one-off job with a random id")
    _forecast_flags(f)
    f.set_defaults(fn=cmd_forecast)

    s = sub.add_parser("serve", help="run the proxy")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8402)
    s.add_argument("--upstream", default=tf.DEFAULT_UPSTREAM)
    s.add_argument("--api-key", help=f"injected when the job sends none (default ${tf.API_KEY_ENV})")
    s.add_argument("--fail-open", action="store_true", help="if the meter itself fails, pass traffic through")
    s.set_defaults(fn=cmd_serve)

    r = sub.add_parser("report", help="forecast vs observed, per term")
    r.add_argument("job")
    r.set_defaults(fn=cmd_report)

    a = sub.add_parser("accept", help="accept a new value for a term and resume")
    a.add_argument("job")
    a.add_argument("terms", nargs="*", help="e.g. output_tokens=2200 price=0.09/0.40; none = resume after fixing the cause")
    a.set_defaults(fn=cmd_accept)

    le = sub.add_parser("lessons", help="what the meter learned across jobs (read before the next forecast)")
    le.set_defaults(fn=cmd_lessons)

    j = sub.add_parser("jobs", help="list jobs")
    j.set_defaults(fn=cmd_jobs)

    m = sub.add_parser("mock", help="fake upstream that reproduces the think-on overrun")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8499)
    m.add_argument("--model", default="mock/nemotron-3-nano-30b-a3b", help="ids are prefixed mock/ so lessons never mistake them for measurements")
    m.add_argument("--output-tokens", type=int, default=2150, help="median completion tokens with thinking on")
    m.add_argument("--output-tokens-off", type=int, default=290, help="median with chat_template_kwargs.enable_thinking=false")
    m.add_argument("--reasoning-share", type=float, default=0.87)
    m.add_argument("--price", default="0.06,0.24")
    m.add_argument("--latency-ms", type=float, default=1000, help="per call, ±50%%; 0 for tests")
    m.add_argument("--tail-on", type=float, default=0.8, help="lognormal σ of completion length with thinking on (0 = ±5%% jitter)")
    m.add_argument("--tail-off", type=float, default=0.3, help="lognormal σ with thinking off")
    m.add_argument("--error-rate", type=float, default=0.0)
    m.add_argument("--error-codes", default="429,500")
    m.add_argument("--ignore-include-usage", action="store_true", help="stream without a usage chunk (older vLLM / some gateways)")
    m.add_argument("--reasoning-report", choices=["details", "reasoning-field", "think-tags", "hidden"], default="details")
    m.add_argument("--respond-model", choices=["echo", "canonical", "base"], default="echo")
    m.add_argument("--pricing", choices=["per-1m", "per-token-strings", "zero", "absent"], default="per-1m")
    m.add_argument("--ignore-max-tokens", action="store_true")
    m.add_argument("--profile", choices=["friendly", "hostile"], default="friendly", help="hostile = errors 10%%, reasoning in reasoning_content, canonical ids, per-token price strings")
    m.add_argument("--seed", type=int)
    m.set_defaults(fn=cmd_mock)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
