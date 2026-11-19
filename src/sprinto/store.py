"""SQLite storage: page cache, Hunter cache, runs, decision records, LLM calls."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ROOT
from .models import DecisionRecord

DEFAULT_DB = ROOT / "data" / "pipeline.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    config_version INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    domains_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    page_type TEXT,
    method TEXT,
    status_code INTEGER,
    raw TEXT,
    cleaned TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_domain ON pages(domain);
CREATE TABLE IF NOT EXISTS hunter_cache (
    domain TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_records (
    record_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL,
    tier TEXT,
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_domain ON decision_records(domain);
CREATE INDEX IF NOT EXISTS records_run ON decision_records(run_id);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL,
    run_id TEXT,
    config_version INTEGER,
    domain TEXT,
    kind TEXT NOT NULL,             -- review | verdict | outcome
    decision TEXT,                  -- approve | edit | reject | qualify | drop | needs_more_info | outcome
    reason_code TEXT,
    note TEXT,
    edit_ratio REAL,
    rewrite INTEGER,
    system_status TEXT,             -- what the pipeline had decided (for agreement metrics)
    system_tier TEXT,
    contact_title TEXT,
    data_json TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_at ON feedback(at);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT,
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    usd REAL NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ----- runs -----
    def create_run(self, config_version: int, domains: list[str], dry_run: bool = False) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(run_id, config_version, started_at, dry_run, domains_json) VALUES (?,?,?,?,?)",
                (run_id, config_version, _now(), int(dry_run), json.dumps(domains)),
            )
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def latest_run_id(self) -> str | None:
        row = self.conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return row["run_id"] if row else None

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.run_id, r.config_version, r.started_at, COUNT(d.record_id) AS n"
            " FROM runs r LEFT JOIN decision_records d ON d.run_id = r.run_id"
            " GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT ?", (limit,)).fetchall()

    def finish_run(self, run_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_at=? WHERE run_id=?", (_now(), run_id))

    # ----- pages -----
    def get_page(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()

    def put_page(self, url: str, domain: str, page_type: str, method: str,
                 status_code: int | None, raw: str, cleaned: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO pages(url, domain, page_type, method, status_code, raw, cleaned, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (url, domain, page_type, method, status_code, raw, cleaned, _now()),
            )

    # ----- hunter -----
    def get_hunter(self, domain: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT response_json FROM hunter_cache WHERE domain=?", (domain,)).fetchone()
        return json.loads(row["response_json"]) if row else None

    def put_hunter(self, domain: str, response: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO hunter_cache(domain, response_json, fetched_at) VALUES (?,?,?)",
                (domain, json.dumps(response), _now()),
            )

    # ----- decision records -----
    def save_record(self, rec: DecisionRecord) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO decision_records(record_id, run_id, domain, status, tier, record_json, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (rec.record_id, rec.run_id, rec.domain, rec.status.value, rec.tier,
                 rec.model_dump_json(), rec.updated_at.isoformat()),
            )

    def get_record(self, record_id: str) -> DecisionRecord | None:
        row = self.conn.execute("SELECT record_json FROM decision_records WHERE record_id=?", (record_id,)).fetchone()
        return DecisionRecord.model_validate_json(row["record_json"]) if row else None

    def records_for_run(self, run_id: str) -> list[DecisionRecord]:
        rows = self.conn.execute(
            "SELECT record_json FROM decision_records WHERE run_id=? ORDER BY domain", (run_id,)
        ).fetchall()
        return [DecisionRecord.model_validate_json(r["record_json"]) for r in rows]

    def researched_recently(self, domain: str, days: int) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT 1 FROM decision_records WHERE domain=? AND updated_at>=? AND status!='failed' LIMIT 1",
            (domain, cutoff),
        ).fetchone()
        return row is not None

    # ----- review queue + feedback -----
    def review_queue(self) -> list[DecisionRecord]:
        """Latest record per domain that is waiting for a human: drafted or needs_review, not yet decided."""
        rows = self.conn.execute(
            "SELECT record_json FROM decision_records d WHERE status IN ('drafted','needs_review')"
            " AND updated_at = (SELECT MAX(updated_at) FROM decision_records x WHERE x.domain = d.domain)"
        ).fetchall()
        recs = [DecisionRecord.model_validate_json(r["record_json"]) for r in rows]
        return [r for r in recs if r.review.decision is None]

    def log_feedback(self, rec: DecisionRecord, kind: str, decision: str, *, reason_code: str | None = None,
                     note: str = "", edit_ratio: float | None = None, rewrite: bool | None = None,
                     system_status: str | None = None, data: dict | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO feedback(record_id, run_id, config_version, domain, kind, decision, reason_code, note,"
                " edit_ratio, rewrite, system_status, system_tier, contact_title, data_json, at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.record_id, rec.run_id, rec.config_version, rec.domain, kind, decision, reason_code, note,
                 edit_ratio, None if rewrite is None else int(rewrite), system_status or rec.status.value,
                 rec.tier, rec.contact.title if rec.contact else None, json.dumps(data or {}), _now()),
            )

    def feedback_since(self, since_iso: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM feedback WHERE at >= ? ORDER BY at", (since_iso,)).fetchall()

    # ----- llm calls -----
    def log_llm_call(self, record_id: str | None, stage: str, model: str, tokens_in: int,
                     tokens_out: int, usd: float, ok: bool, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO llm_calls(record_id, stage, model, tokens_in, tokens_out, usd, ok, error, at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (record_id, stage, model, tokens_in, tokens_out, usd, int(ok), error, _now()),
            )
