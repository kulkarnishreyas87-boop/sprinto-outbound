"""Feedback: weekly pattern report and PROPOSED config changes.

The pipeline never changes config, prompts or examples by itself. `propose` writes a candidate
config/v{N+1}.yaml (not active) plus example candidates under proposals/v{N+1}/, and prints a diff.
Only `promote` (a human command, after the gold set passes) makes a version active.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CONFIG_DIR, ROOT, Config, active_version, config_path
from .store import Store

PROPOSALS_DIR = ROOT / "proposals"
LIGHT_EDIT_RATIO = 0.15

# Which stage a reviewer's reason code points at (for the "by stage" view).
REASON_STAGE = {
    "wrong_company": "S5 qualify / S3 gates",
    "wrong_person": "S6 contact",
    "factual_error": "S2 extract / S9 QA",
    "wrong_pain": "S7 pains",
    "weak_hook": "S8 draft",
    "wrong_tone": "S8 draft",
    "too_long": "S9 QA",
    "other": "unassigned",
}


def parse_since(text: str) -> datetime:
    m = re.fullmatch(r"(\d+)([dh])", text.strip().lower())
    if not m:
        raise ValueError("--since must look like 7d or 24h")
    n, unit = int(m.group(1)), m.group(2)
    return datetime.now(timezone.utc) - (timedelta(days=n) if unit == "d" else timedelta(hours=n))


@dataclass
class PatternReport:
    since: datetime
    reviews: int = 0
    rewrites: int = 0
    reviewed_drafts: int = 0
    qual_judged: int = 0
    qual_agree: int = 0
    wrong_person: int = 0
    factual_errors: list[dict] = field(default_factory=list)
    reasons_by_stage: dict[str, Counter] = field(default_factory=dict)
    wrong_person_titles: Counter = field(default_factory=Counter)
    wrong_company: list[dict] = field(default_factory=list)
    example_candidates: list[dict] = field(default_factory=list)
    companies: int = 0
    usd: float = 0.0
    seconds: float = 0.0
    outcomes: Counter = field(default_factory=Counter)

    @staticmethod
    def _pct(a: int, b: int) -> str:
        return f"{100 * a / b:.0f}% ({a}/{b})" if b else "n/a (no data)"

    def render(self) -> str:
        L = [f"━━ Pattern report since {self.since:%Y-%m-%d %H:%M} UTC",
             f"Rewrite rate: {self._pct(self.rewrites, self.reviewed_drafts)}",
             f"Qualification agreement: {self._pct(self.qual_agree, self.qual_judged)}",
             f"Contact accuracy: {self._pct(self.reviewed_drafts - self.wrong_person, self.reviewed_drafts)}",
             f"Factual errors: {len(self.factual_errors)}"]
        for fe in self.factual_errors:
            L.append(f"  BUG: {fe['domain']} record {fe['record_id']} — {fe['note'] or 'no note'}")
        if self.reasons_by_stage:
            L.append("Reason codes by stage:")
            for stage, c in sorted(self.reasons_by_stage.items()):
                L.append(f"  {stage}: " + ", ".join(f"{k} ×{v}" for k, v in c.most_common()))
        if self.outcomes:
            L.append("Outcomes: " + ", ".join(f"{k} ×{v}" for k, v in self.outcomes.most_common()))
        per = f"${self.usd / self.companies:.4f} and {self.seconds / self.companies:.1f}s per company" \
            if self.companies else "n/a"
        L.append(f"Companies processed: {self.companies} | cost ${self.usd:.4f} total | {per}")
        return "\n".join(L)


def build_report(store: Store, since: datetime) -> PatternReport:
    r = PatternReport(since=since)
    rows = store.feedback_since(since.isoformat())
    reasons: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        kind, decision, reason = row["kind"], row["decision"], row["reason_code"]
        data = json.loads(row["data_json"] or "{}")
        if kind == "outcome":
            if data.get("replied"):
                r.outcomes[f"replied:{data['replied']}"] += 1
            if data.get("meeting"):
                r.outcomes["meeting"] += 1
            if data.get("bounced"):
                r.outcomes["bounced"] += 1
            continue
        r.reviews += 1
        if reason:
            reasons[REASON_STAGE.get(reason, "unassigned")][reason] += 1
        if reason == "factual_error":
            r.factual_errors.append({"record_id": row["record_id"], "domain": row["domain"], "note": row["note"]})

        if kind == "review":                       # a draft the system qualified
            r.reviewed_drafts += 1
            if row["rewrite"]:
                r.rewrites += 1
            if reason == "wrong_person":
                r.wrong_person += 1
                if row["contact_title"]:
                    r.wrong_person_titles[row["contact_title"].strip().lower()] += 1
            if reason == "wrong_company":
                r.wrong_company.append({"record_id": row["record_id"], "domain": row["domain"], "note": row["note"]})
            human_qualified = not (decision == "reject" and reason == "wrong_company")
            r.qual_judged += 1
            r.qual_agree += int(human_qualified)
            if decision == "approve" or (decision == "edit" and (row["edit_ratio"] or 0) <= LIGHT_EDIT_RATIO
                                         and not row["rewrite"]):
                r.example_candidates.append({"record_id": row["record_id"], "domain": row["domain"],
                                             "decision": decision, "edit_ratio": row["edit_ratio"]})
        elif kind == "verdict" and decision in ("qualify", "drop"):   # the system was unsure (review queue)
            r.qual_judged += 1
            r.qual_agree += int(decision == "drop")     # agreeing = the human also didn't qualify it
    r.reasons_by_stage = dict(reasons)
    recs = store.conn.execute("SELECT record_json FROM decision_records WHERE updated_at >= ?",
                              (since.isoformat(),)).fetchall()
    for row in recs:
        rec = json.loads(row["record_json"])
        r.companies += 1
        r.usd += rec.get("cost", {}).get("usd_estimate", 0.0)
        r.seconds += sum((rec.get("timings") or {}).values()) / 1000
    return r


# ---------- proposals ----------

@dataclass
class Proposal:
    version: int | None
    changes: list[str]
    flags: list[str]
    config_text: str | None
    diff: str
    example_files: list[Path]


def _add_to_flow_list(yaml_text: str, key: str, items: list[str]) -> str:
    """Add items to a one-line `key: [a, b]` list, keeping the rest of the file (and comments) intact."""
    pat = re.compile(rf"^(\s*{re.escape(key)}:\s*\[)(.*?)(\])", re.M)
    m = pat.search(yaml_text)
    if not m:
        raise ValueError(f"could not find a one-line list for {key}")
    extra = ", ".join(json.dumps(i) if re.search(r"[\s,:#]", i) else i for i in items)
    inner = m.group(2).rstrip()
    return yaml_text[:m.start()] + m.group(1) + (inner + ", " if inner else "") + extra + m.group(3) + \
        yaml_text[m.end():]


def next_version(config_dir: Path = CONFIG_DIR) -> int:
    nums = [int(p.stem[1:]) for p in config_dir.glob("v*.yaml") if p.stem[1:].isdigit()]
    return max(nums, default=0) + 1


def make_proposal(report: PatternReport, store: Store, cfg: Config, *, config_dir: Path = CONFIG_DIR,
                  proposals_dir: Path = PROPOSALS_DIR, write: bool = True) -> Proposal:
    min_n = cfg.feedback.min_occurrences_for_proposal
    base_v = active_version(config_dir)
    base_text = config_path(base_v, config_dir).read_text(encoding="utf-8")
    text, changes, flags = base_text, [], []

    # 1. contact exclusions after repeated wrong_person for the same title
    existing = {k.lower() for k in cfg.contacts.exclude_title_keywords}
    to_exclude = [t for t, n in report.wrong_person_titles.most_common() if n >= min_n and t not in existing]
    if to_exclude:
        text = _add_to_flow_list(text, "exclude_title_keywords", to_exclude)
        changes += [f"contacts.exclude_title_keywords += '{t}' ({report.wrong_person_titles[t]} wrong_person "
                    f"rejections)" for t in to_exclude]

    # 2. repeated wrong_company: flag with evidence (which gate to tighten is a human design decision)
    if len(report.wrong_company) >= min_n:
        flags.append(f"{len(report.wrong_company)} wrong_company rejections: review these and tighten a gate or "
                     "tier rule by hand: " + ", ".join(f"{w['domain']} ({w['record_id']})"
                                                       for w in report.wrong_company))
    # 3. factual errors are bugs, flagged immediately (no minimum)
    for fe in report.factual_errors:
        flags.append(f"BUG factual_error: {fe['domain']} record {fe['record_id']} — inspect with "
                     f"`report` and fix the extraction/QA rule before the next run")

    version = None
    diff = ""
    example_files: list[Path] = []
    if changes or report.example_candidates:
        version = next_version(config_dir)
    if changes:
        text = re.sub(r"^version:\s*\d+", f"version: {version}", text, count=1, flags=re.M)
        text = re.sub(r"^description:.*$", f'description: "PROPOSED from feedback since {report.since:%Y-%m-%d}; '
                      f'based on v{base_v}; not active until promoted"', text, count=1, flags=re.M)
        Config.model_validate(__import__("yaml").safe_load(text))       # never write an invalid config
        diff = "".join(difflib.unified_diff(base_text.splitlines(True), text.splitlines(True),
                                            f"config/v{base_v}.yaml", f"config/v{version}.yaml"))
    if write and version is not None:
        if changes:
            config_path(version, config_dir).write_text(text, encoding="utf-8")
        out = proposals_dir / f"v{version}"
        for c in report.example_candidates:
            rec = store.get_record(c["record_id"])
            if not rec or rec.chosen_draft is None:
                continue
            d = next(x for x in rec.drafts if x.variant == rec.chosen_draft)
            body = rec.review.edited_text or d.body
            subject = rec.review.edited_subject or d.subject
            out.joinpath("examples").mkdir(parents=True, exist_ok=True)
            f = out / "examples" / f"{rec.domain}-{rec.record_id}.md"
            f.write_text(f"approved: false\n# Candidate from {c['decision']} review of {rec.domain} "
                         f"(moment {rec.moment}, hook {d.hook_field}). Set approved: true to use.\n"
                         f"Subject: {subject}\n\n{body}\n", encoding="utf-8")
            example_files.append(f)
        if example_files:
            changes.append(f"{len(example_files)} example candidate(s) in {out / 'examples'} "
                           "(copy into config/examples/ and approve by hand)")
    return Proposal(version if (changes or example_files) else None, changes, flags,
                    text if diff else None, diff, example_files)
