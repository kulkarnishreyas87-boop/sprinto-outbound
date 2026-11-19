"""Gold-set regression test, fixtures, and promotion.

Each gold case runs the full pipeline in dry-run mode on FROZEN fixtures (pages, job boards,
Hunter responses under data/gold_fixtures/<domain>/): the network is blocked, Hunter is
cache-only, nothing is sent. Only the LLM calls are live.

A config version passes only if qualification agreement, contact accuracy and QA pass rate
are each >= the active version's. `promote` makes a version active only by human command,
and only after it passed (rollback to an older version is always allowed).
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml

from .config import CONFIG_DIR, ROOT, Claims, Settings, active_version, load_config
from .contacts import HunterClient, title_has
from .fetch import Fetcher
from .models import DecisionRecord, Status
from .store import Store

GOLD_FILE = ROOT / "data" / "gold_set.yaml"
FIXTURES = ROOT / "data" / "gold_fixtures"
METRICS = ("qualification_agreement", "contact_accuracy", "qa_pass_rate")


# ---------- fixtures ----------

def freeze(domain: str, store: Store, fixtures_dir: Path = FIXTURES) -> Path:
    """Snapshot this domain's cached pages, job-board responses and Hunter response."""
    # pages are stored under their own host, so include the company's subdomains (trust.acme.io etc.)
    rows = store.conn.execute("SELECT * FROM pages WHERE domain=? OR domain LIKE ?",
                              (domain, f"%.{domain}")).fetchall()
    if not rows:
        raise ValueError(f"no cached pages for {domain}: run it first (slice/run), then freeze")
    out = fixtures_dir / domain
    out.mkdir(parents=True, exist_ok=True)
    pages = {r["url"]: {"page_type": r["page_type"], "method": r["method"], "status_code": r["status_code"],
                        "raw": r["raw"]} for r in rows}
    (out / "pages.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    hunter = store.get_hunter(domain)
    hunter_f = out / "hunter.json"
    hand_written = hunter_f.exists() and "_fixture" in json.loads(hunter_f.read_text(encoding="utf-8"))
    if hunter is not None and not hand_written:     # never overwrite a deliberately hand-written case
        hunter_f.write_text(json.dumps(hunter, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def load_fixtures(domain: str, store: Store, fixtures_dir: Path = FIXTURES) -> bool:
    d = fixtures_dir / domain
    pages_f = d / "pages.json"
    if not pages_f.exists():
        return False
    for url, p in json.loads(pages_f.read_text(encoding="utf-8")).items():
        store.put_page(url, domain, p.get("page_type"), p.get("method"), p.get("status_code"), p["raw"], "")
    if (d / "hunter.json").exists():
        store.put_hunter(domain, json.loads((d / "hunter.json").read_text(encoding="utf-8")))
    return True


def _offline_client() -> httpx.Client:
    """Every network request gets a 404: only frozen fixtures exist during a gold run."""
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404, text="offline")))


# ---------- cases and evaluation ----------

@dataclass
class CaseResult:
    id: str
    domain: str
    ok: bool
    qualification_ok: bool
    contact_ok: bool | None
    reached_qa: bool
    qa_passed: bool
    actual: dict
    problems: list[str] = field(default_factory=list)


def load_cases(path: Path = GOLD_FILE) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [c for c in data.get("cases", []) if c.get("domain") and "TODO" not in c["domain"]]


def _status_matches(expected: str, actual: Status) -> bool:
    if expected == "sent":                  # in a dry run, "would be sent after approval" = drafted
        return actual in (Status.drafted, Status.sent)
    return actual.value == expected


def evaluate(case: dict, rec: DecisionRecord | None) -> CaseResult:
    exp = case.get("expected") or {}
    if rec is None:
        return CaseResult(case["id"], case["domain"], False, False, None, False, False, {},
                          ["missing fixtures: run `test-gold --freeze` after researching this domain"])
    p: list[str] = []
    if exp.get("status") and not _status_matches(exp["status"], rec.status):
        p.append(f"status {rec.status.value} != {exp['status']}")
    if case.get("allowed_statuses") and not any(_status_matches(s, rec.status) for s in case["allowed_statuses"]):
        p.append(f"status {rec.status.value} not in {case['allowed_statuses']}")
    if exp.get("reason_code") and rec.reason_code != exp["reason_code"]:
        p.append(f"reason {rec.reason_code} != {exp['reason_code']}")
    if exp.get("tier") and rec.tier != exp["tier"]:
        p.append(f"tier {rec.tier} != {exp['tier']}")
    if case.get("allowed_tiers") and rec.tier not in case["allowed_tiers"]:
        p.append(f"tier {rec.tier} not in {case['allowed_tiers']}")
    if exp.get("moment") and rec.moment != exp["moment"]:
        p.append(f"moment {rec.moment} != {exp['moment']}")
    if exp.get("min_loop_iterations") and rec.loop_iterations < exp["min_loop_iterations"]:
        p.append(f"loop iterations {rec.loop_iterations} < {exp['min_loop_iterations']}")
    qualification_ok = not p
    contact_ok = None
    if exp.get("contact_title"):
        actual_title = rec.contact.title if rec.contact else ""
        contact_ok = bool(actual_title) and title_has(actual_title, exp["contact_title"])
        if not contact_ok:
            p.append(f"contact title {actual_title or 'none'!r} != {exp['contact_title']!r}")
    reached_qa = bool(rec.qa_results)
    qa_passed = reached_qa and rec.status in (Status.drafted, Status.sent)
    actual = {"status": rec.status.value, "reason_code": rec.reason_code, "tier": rec.tier, "moment": rec.moment,
              "contact_title": rec.contact.title if rec.contact else None, "loop_iterations": rec.loop_iterations,
              "record_id": rec.record_id}
    return CaseResult(case["id"], case["domain"], not p, qualification_ok, contact_ok, reached_qa, qa_passed,
                      actual, p)


def metrics(results: list[CaseResult]) -> dict[str, float | None]:
    def rate(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None
    return {"qualification_agreement": rate([r.qualification_ok for r in results]),
            "contact_accuracy": rate([r.contact_ok for r in results]),
            "qa_pass_rate": rate([r.qa_passed for r in results if r.reached_qa])}


def compare(new: dict, baseline: dict | None) -> tuple[bool, list[str]]:
    """New passes if every metric is >= baseline. A metric with no data on both sides is neutral."""
    if baseline is None:
        return True, ["no baseline result: this run becomes the baseline"]
    notes, ok = [], True
    for m in METRICS:
        a, b = new.get(m), baseline.get(m)
        if b is None:
            notes.append(f"{m}: {a} (baseline has no data)")
        elif a is None or a < b:
            ok = False
            notes.append(f"{m}: {a} < baseline {b}  FAIL")
        else:
            notes.append(f"{m}: {a} >= baseline {b}")
    return ok, notes


# ---------- running ----------

def run_gold(version: int, *, settings: Settings, claims: Claims, cases: list[dict] | None = None,
             llm=None, fixtures_dir: Path = FIXTURES, today: date | None = None,
             config_dir: Path = CONFIG_DIR) -> tuple[list[CaseResult], dict]:
    from .llm import make_provider
    from .pipeline import run_batch

    cfg = load_config(version, config_dir)
    cases = cases if cases is not None else load_cases()
    with tempfile.TemporaryDirectory(prefix="gold-") as tmp:
        store = Store(Path(tmp) / "gold.db")
        try:
            have = {c["domain"]: load_fixtures(c["domain"], store, fixtures_dir) for c in cases}
            domains = [d for d, ok in have.items() if ok]
            fetcher = Fetcher(cfg.scrape, store, client=_offline_client(), sleep=lambda s: None, renderer=None)
            llm = llm or make_provider(cfg, store, settings)
            hunter = HunterClient("", store, cfg.contacts, cache_only=True)
            _, records, _ = run_batch(domains, cfg=cfg, claims=claims, store=store, fetcher=fetcher, llm=llm,
                                      hunter=hunter, force=True, dry_run=True, runs_dir=Path(tmp) / "runs",
                                      today=today)
            by_domain = {r.domain: r for r in records}
            results = [evaluate(c, by_domain.get(c["domain"])) for c in cases]
        finally:
            store.close()
    return results, metrics(results)


def save_result(store: Store, version: int, results: list[CaseResult], m: dict, passed: bool,
                baseline_version: int | None) -> None:
    with store.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS gold_results (id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER,"
                  " at TEXT, metrics_json TEXT, cases_json TEXT, passed INTEGER, baseline_version INTEGER)")
        c.execute("INSERT INTO gold_results(version, at, metrics_json, cases_json, passed, baseline_version)"
                  " VALUES (?,?,?,?,?,?)",
                  (version, datetime.now(timezone.utc).isoformat(), json.dumps(m),
                   json.dumps([asdict(r) for r in results]), int(passed), baseline_version))


def latest_result(store: Store, version: int) -> dict | None:
    try:
        row = store.conn.execute("SELECT * FROM gold_results WHERE version=? ORDER BY id DESC LIMIT 1",
                                 (version,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {"metrics": json.loads(row["metrics_json"]), "passed": bool(row["passed"]),
            "baseline_version": row["baseline_version"], "at": row["at"]}


# ---------- promotion ----------

class PromotionError(RuntimeError):
    pass


def promote(version: int, store: Store, config_dir: Path = CONFIG_DIR) -> str:
    """Human-only. Forward needs a passing gold result against the current active version; rollback is free."""
    load_config(version, config_dir)              # must exist and validate
    current = active_version(config_dir)
    if version == current:
        return f"v{version} is already active"
    if version > current:
        res = latest_result(store, version)
        if res is None:
            raise PromotionError(f"v{version} has no gold-set result: run `test-gold --config {version}` first")
        if not res["passed"] or res["baseline_version"] != current:
            raise PromotionError(f"v{version} did not pass the gold set against the active v{current} "
                                 f"(last result: passed={res['passed']}, baseline v{res['baseline_version']})")
    (config_dir / "ACTIVE").write_text(f"v{version}\n", encoding="utf-8")
    with store.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS promotions (id INTEGER PRIMARY KEY AUTOINCREMENT, from_version"
                  " INTEGER, to_version INTEGER, at TEXT)")
        c.execute("INSERT INTO promotions(from_version, to_version, at) VALUES (?,?,?)",
                  (current, version, datetime.now(timezone.utc).isoformat()))
    kind = "rolled back" if version < current else "promoted"
    return f"{kind}: v{current} -> v{version}"
