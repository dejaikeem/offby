---
name: offby
description: Use BEFORE running any script or loop that calls an LLM many times (batch classification, embeddings, eval sweeps, agent fleets over a dataset) — or when the user says "offby", "배치 돌려", "비용 감시", "예산 안에서". Puts the run behind the Offby proxy so it halts at ~10 calls if usage breaks the forecast, with the broken term named. Not for interactive/chat use.
---

# Offby — run a batch behind the forecast referee

Offby is a local OpenAI-compatible proxy. You give it a forecast (calls · input tok/call · output tok/call · model), point the job's `OPENAI_BASE_URL` at it, and from call 10 it compares observed `usage` to the forecast per term. If a term runs ≥2x (overall mean AND last-5 mean), the job gets `HTTP 402` with `X-Offby-Halt: <term>` and stops spending. You are the one who sets it up and reacts — the proxy does the counting.

Repo: `~/Desktop/personal/offby`. All commands below run from there with `uv run offby …` (or `uv run --project ~/Desktop/personal/offby offby …` from elsewhere).

## When this applies

- You are about to execute code that calls `chat/completions` (or `completions`) **in a loop over items** — N ≥ ~50 — and nobody will watch it finish.
- It does NOT apply to interactive sessions, chat UIs, or single calls. Do not wrap those.

## Procedure

**1. Upstream and proxy.** Decide the upstream:
- Real: `NEBIUS_API_KEY` is set → upstream is Token Factory (the proxy default).
- PoC / no key / the user said "mock": start `uv run offby mock --port 8499` and use `--upstream http://127.0.0.1:8499/v1` everywhere below.

Check the proxy: `curl -s localhost:8402/healthz`. If it is not up, start it in the background:
`uv run offby serve --port 8402 [--upstream http://127.0.0.1:8499/v1]`.

**2. Read what the meter already knows.** `uv run offby lessons`. Note per-model facts (does it think by default? output multiplier with/without thinking? price) and past breaches. Apply them to the forecast below — e.g. if the model is known to bill a reasoning trace and the code does not disable thinking, either add the flag to the code now or forecast the trace.

**3. Derive the forecast from the code, not from a guess.**
- `calls` = number of items the loop will process (count the data file's rows, the list length, the query's row count). Say how you counted.
- `input_tokens` = (system prompt + user template + one typical item) in characters ÷ 4. Sample one real item.
- `output_tokens` = what the prompt asks for (a label ≈ 5, a sentence ≈ 30, a paragraph ≈ 250) — the *billed* output, so include a reasoning trace if the model thinks by default and the code does not turn it off.
- `model` = the exact id in the code. Prices come from the proxy's oracle; never type a price from memory. If the oracle cannot price it, pass `--price in,out` only with a source you can cite, else leave it UNPRICED.
- `budget` = what the user said, or ask.

**4. Register the job (named after the script or task, lowercase).**
```
uv run offby job ensure <name> --calls N --input I --output O --model <id> --budget B [--upstream …] -y
```
Running this again later starts a **new run** of the same name (counters reset, history kept). Show the user the printed term table; if `expected total` exceeds the budget, stop and say so before running anything.

**5. Run the job through the proxy.**
```
OPENAI_BASE_URL=http://localhost:8402/j/<name>/v1 <the command>
```
The job's own API key still goes in the request; the proxy passes it through.

**6. If the job dies with `402 … offby: term X breached …` (exit code 3 in the example runner):**
- `uv run offby report <name>` — read the per-term table, the evidence line, and the diagnosis.
- Prefer fixing the cause in the code (e.g. `chat_template_kwargs.enable_thinking=false`, a smaller `max_tokens`, the intended model id). Then `uv run offby accept <name>` (no terms) and rerun — judging restarts from the next call.
- If the observed number is legitimate, `uv run offby accept <name> X=<value>` — but first show the new expected total; if it now exceeds the budget, ask the user rather than accepting.
- Never loop accept→rerun more than twice without telling the user what changed.

**7. When it finishes:** `uv run offby report <name>` and tell the user spent vs expected in one line. Then `uv run offby lessons` once more; if it says something new about this model (e.g. a thinking multiplier), mention it — the next forecast should use it.

## Rules

- List every `ASSUMED` term to the user; never silently accept one.
- Do not quote model numbers (prices, context length, whether it thinks) from memory. The meter measured them or it did not.
- The proxy stores usage only — never prompts or completions. Do not add prompt logging to it.
- Offby's 402 has `X-Offby-Halt`; an upstream 402 (balance exhausted) does not. Read the header before deciding what happened.
