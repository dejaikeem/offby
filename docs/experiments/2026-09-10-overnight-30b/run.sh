#!/bin/zsh
# Overnight scale run — 30b, three phases, hard wall-clock cap. Writes everything under $OUT.
set -u
cd /Users/dejay/Desktop/personal/offby
OUT=/private/tmp/claude-501/-Users-dejay-Desktop-personal/49f023e1-7ed0-4bc5-8ae6-7ca454585ba2/scratchpad/overnight
DATA=$OUT/reviews-6k.jsonl
UP=http://127.0.0.1:11434/v1
M=nemotron-3-nano:30b
JOB=overnight-30b
T0=$(date +%s)
CAP=$((6*3600+1800))            # 6.5 h total
left() { echo $(( CAP - ($(date +%s) - T0) )); }
cap() { local secs=$1; shift; perl -e 'alarm shift; exec @ARGV' "$secs" "$@"; }   # macOS has no GNU timeout
stamp() { echo "[$(date +%H:%M:%S) +$(( ($(date +%s)-T0)/60 ))m] $*"; }

stamp "PHASE 0 start ollama (NUM_PARALLEL=4) + proxy"
OLLAMA_NUM_PARALLEL=4 OLLAMA_KEEP_ALIVE=-1 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 nohup ollama serve > $OUT/ollama.log 2>&1 &
OLL=$!
export OFFBY_DIAG_MODEL=$M
uv run offby serve --port 8402 --upstream $UP > $OUT/proxy.log 2>&1 &
PROXY=$!
uv run python - <<'PY'
import time, urllib.request
for url in ("http://127.0.0.1:11434/api/tags","http://127.0.0.1:8402/healthz"):
    for _ in range(300):
        try: urllib.request.urlopen(url, timeout=1); break
        except Exception: time.sleep(0.5)
    else: raise SystemExit(f"not up: {url}")
PY
stamp "PHASE 0 job ensure (forecast from lessons: 30b think-on mean ≈400)"
uv run offby job ensure $JOB --calls 6000 --input 40 --output 400 --model $M --price 0.06,0.24 --budget 5 --upstream $UP -y | grep -E "created|new run|expected"

stamp "PHASE A rows 0-1999, default prompt, thinking on — expect NO halt (false-halt test at scale). cap 3h"
cap $(( 3*3600 )) env OPENAI_BASE_URL=http://localhost:8402/j/$JOB/v1 uv run python examples/classify.py --data $DATA --model $M --concurrency 4 --start 0 --limit 2000 > $OUT/phaseA.out 2>&1
stamp "PHASE A exit=$? ; $(tail -1 $OUT/phaseA.out)"
uv run offby report $JOB > $OUT/reportA.txt 2>&1; sed -n 2,14p $OUT/reportA.txt
curl -s http://127.0.0.1:8402/healthz > $OUT/healthA.json; cat $OUT/healthA.json; echo
NA=$(uv run python -c "import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/.offby/offby.sqlite')); print(c.execute(\"select count(*) from calls where job_id='$JOB'\").fetchone()[0])")
stamp "calls after A: $NA"

stamp "PHASE B rows 2000+, LONG prompt, thinking on — expect halt within ~10-40 calls (regime change after $NA healthy calls). cap 1h"
cap 3600 env OPENAI_BASE_URL=http://localhost:8402/j/$JOB/v1 uv run python examples/classify.py --data $DATA --model $M --concurrency 4 --start 2000 --limit 4000 \
  --system "Explain your reasoning step by step in detail — consider tone, specific claims, and what the reviewer left unsaid — and only then state the sentiment. Write at least three paragraphs." > $OUT/phaseB.out 2>&1
stamp "PHASE B exit=$? ; $(grep -E '402|done:|error' $OUT/phaseB.out | tail -2)"
sleep 30
uv run offby report $JOB > $OUT/reportB.txt 2>&1; sed -n 2,24p $OUT/reportB.txt
NB=$(uv run python -c "import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/.offby/offby.sqlite')); print(c.execute(\"select count(*) from calls where job_id='$JOB'\").fetchone()[0])")
stamp "calls after B: $NB (B used $((NB-NA)) calls before the halt)"

stamp "PHASE C accept (cause fixed) + rows 2000-5999, default prompt, thinking OFF — judge-timing growth. cap = remaining $(( $(left)/60 ))m"
uv run offby accept $JOB | head -1
L=$(left); [ $L -gt 600 ] && cap $L env OPENAI_BASE_URL=http://localhost:8402/j/$JOB/v1 uv run python examples/classify.py --data $DATA --model $M --concurrency 4 --start 2000 --limit 4000 --no-think > $OUT/phaseC.out 2>&1
stamp "PHASE C exit=$? ; $(tail -1 $OUT/phaseC.out)"
uv run offby report $JOB > $OUT/reportC.txt 2>&1; sed -n 2,14p $OUT/reportC.txt
curl -s http://127.0.0.1:8402/healthz > $OUT/healthC.json; cat $OUT/healthC.json; echo

stamp "STATS"
uv run python - "$JOB" "$NA" "$NB" > $OUT/stats.txt 2>&1 <<'PY'
import sqlite3, statistics, sys, os, math
job, na, nb = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
c=sqlite3.connect(os.path.expanduser("~/.offby/offby.sqlite")); c.row_factory=sqlite3.Row
rows=c.execute("select * from calls where job_id=? order by seq",(job,)).fetchall()
def q(v,p): v=sorted(v); return v[min(len(v)-1,int(p*len(v)))]
for label, sel in (("A think-on default", rows[:na]), ("B think-on long prompt", rows[na:nb]), ("C think-off default", rows[nb:])):
    ok=[r for r in sel if (r["status"] or 0)==200 and r["completion_tokens"] is not None]
    if not ok: print(f"[{label}] n=0"); continue
    comp=[r["completion_tokens"] for r in ok]; lat=[r["latency_ms"] for r in ok]; ls=[math.log(x) for x in comp]
    err=len(sel)-len(ok)
    print(f"[{label}] n={len(sel)} ok={len(ok)} err={err} completion median={statistics.median(comp):.0f} mean={statistics.fmean(comp):.0f} p95={q(comp,.95)} max={max(comp)} σ={statistics.pstdev(ls):.2f} length-cut={sum(1 for r in ok if r['finish_reason']=='length')} latency median={statistics.median(lat)/1000:.1f}s p95={q(lat,.95)/1000:.1f}s")
PY
cat $OUT/stats.txt
grep -h '"event"' $OUT/proxy.log > $OUT/events.jsonl; stamp "events: $(wc -l < $OUT/events.jsonl)"; cat $OUT/events.jsonl | cut -c1-260
uv run offby lessons | sed -n 3,7p > $OUT/lessons.txt; cat $OUT/lessons.txt
kill $PROXY 2>/dev/null; kill $OLL 2>/dev/null; wait 2>/dev/null
stamp "DONE total $(( ($(date +%s)-T0)/60 ))m — ollama and proxy stopped"
