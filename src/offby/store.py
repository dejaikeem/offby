"""SQLite store. Usage numbers only — never prompts or completions."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("OFFBY_DB", str(Path.home() / ".offby" / "offby.sqlite")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  sentence TEXT,
  budget_usd REAL,
  terms TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'running',
  halted_term TEXT,
  baseline_n INTEGER,
  min_calls INTEGER NOT NULL DEFAULT 10,
  threshold REAL NOT NULL DEFAULT 2.0,
  judge_from INTEGER NOT NULL DEFAULT 0,
  run_from INTEGER NOT NULL DEFAULT 0,
  halted_at INTEGER,
  evidence TEXT,
  diagnosis_status TEXT,
  diagnosis TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  job_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts REAL NOT NULL,
  model TEXT,
  status INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  reasoning_tokens INTEGER,
  reasoning_estimated INTEGER NOT NULL DEFAULT 0,
  usage_estimated INTEGER NOT NULL DEFAULT 0,
  finish_reason TEXT,
  cached_tokens INTEGER,
  latency_ms REAL,
  in_per_m REAL,
  out_per_m REAL,
  cost_usd REAL,
  PRIMARY KEY (job_id, seq)
);
"""

JSON_FIELDS = ("terms", "evidence")


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was created. Cheap, idempotent."""
        wanted = {
            "jobs": (("judge_from", "INTEGER NOT NULL DEFAULT 0"), ("run_from", "INTEGER NOT NULL DEFAULT 0"),
                     ("halted_at", "INTEGER")),
            "calls": (("usage_estimated", "INTEGER NOT NULL DEFAULT 0"), ("finish_reason", "TEXT")),
        }
        for table, cols in wanted.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # ---- jobs ----
    def create_job(
        self,
        job_id: str,
        terms: dict,
        *,
        sentence: str | None = None,
        budget_usd: float | None = None,
        baseline_n: int | None = None,
        min_calls: int = 10,
        threshold: float = 2.0,
    ) -> dict:
        self.conn.execute(
            "INSERT INTO jobs (id, created_at, sentence, budget_usd, terms, baseline_n, min_calls, threshold)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (job_id, time.time(), sentence, budget_usd, json.dumps(terms), baseline_n, min_calls, threshold),
        )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._job(r) for r in rows]

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if k in JSON_FIELDS and v is not None else v)
        vals.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)

    def ensure_job(self, job_id: str, terms: dict | None, *, sentence=None, budget_usd=None, baseline_n=None,
                   min_calls: int = 10, threshold: float = 2.0) -> tuple[dict, bool]:
        """Create the named job, or start a NEW RUN of an existing one: counting, judging and
        state reset from the next call; history in `calls` is kept. Returns (job, created)."""
        job = self.get_job(job_id)
        if job is None:
            return self.create_job(job_id, terms or {}, sentence=sentence, budget_usd=budget_usd,
                                   baseline_n=baseline_n, min_calls=min_calls, threshold=threshold), True
        n = self.count_calls(job_id)
        fields = dict(state="running", halted_term=None, halted_at=None, diagnosis_status=None, diagnosis=None,
                      evidence=None, judge_from=n, run_from=n, min_calls=min_calls, threshold=threshold)
        if terms:
            fields["terms"] = terms
        if sentence is not None:
            fields["sentence"] = sentence
        if budget_usd is not None:
            fields["budget_usd"] = budget_usd
        if baseline_n is not None:
            fields["baseline_n"] = baseline_n
            if not terms:
                fields["terms"] = {}
        self.update_job(job_id, **fields)
        return self.get_job(job_id), False

    def resume(self, job_id: str, terms: dict | None = None) -> dict:
        """Un-halt. Per-call judging restarts from the next call; totals keep counting."""
        fields = dict(state="running", halted_term=None, halted_at=None, diagnosis_status=None, judge_from=self.count_calls(job_id))
        if terms is not None:
            fields["terms"] = terms
        self.update_job(job_id, **fields)
        return self.get_job(job_id)

    @staticmethod
    def _job(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in JSON_FIELDS:
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k == "terms" else None)
        return d

    # ---- calls ----
    def add_call(self, job_id: str, **fields) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM calls WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            fields.setdefault("ts", time.time())
            cols = ["job_id", "seq", *fields.keys()]
            self.conn.execute(
                f"INSERT INTO calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [job_id, seq, *fields.values()],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return seq

    def calls(self, job_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM calls WHERE job_id=? ORDER BY seq", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_calls(self, job_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM calls WHERE job_id=?", (job_id,)).fetchone()[0]
