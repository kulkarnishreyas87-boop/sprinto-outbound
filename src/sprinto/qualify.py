"""S5 Qualify and tier. The model proposes {moment, tier, rationale, signals_used};
code computes the signals itself from VERIFIED fields and may only lower the tier.

Tier rules (brief section 3, thresholds in config):
  A = clear moment + at least one strong signal (verified)
  B = fit signals present, no recent trigger
  C = plausible fit, key fact (usually stage) unknown, or moment not backed by evidence -> review
Stage is never inferred: it is "known" only through explicit proxies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel

from .clean import normalize
from .config import ROOT, Config
from .llm import LLMProvider, LLMResult
from .models import ExtractedFields

PROMPT = ROOT / "prompts" / "qualify.md"
RANK = {"A": 0, "B": 1, "C": 2}


class QualifyOutput(BaseModel):
    moment: Literal["first_certification", "expansion", "none"]
    tier: Literal["A", "B", "C"]
    rationale: str
    signals_used: list[str]


@dataclass
class Signals:
    strong: list[str] = field(default_factory=list)
    fit: list[str] = field(default_factory=list)
    stage_proxies: list[str] = field(default_factory=list)
    cert_held: bool = False
    cert_in_progress: bool = False


def _days_old(iso: str | None, today: date) -> int | None:
    if not iso:
        return None
    try:
        return (today - date.fromisoformat(iso[:10])).days
    except ValueError:
        return None


def compute_signals(f: ExtractedFields, cfg: Config, today: date) -> Signals:
    t = cfg.tiers
    s = Signals()
    if f.certifications_named.known:
        quote = normalize(f.certifications_named.evidence_quote or "")
        if any(normalize(p) in quote for p in t.in_progress_phrases):
            s.cert_in_progress = True
        else:
            s.cert_held = True

    # strong signals
    if f.compliance_security_roles.known:
        recent = [r for r in f.compliance_security_roles.value
                  if (d := _days_old(r.posted_date, today)) is not None and d <= t.strong_signal_role_max_age_days]
        if recent:
            s.strong.append(f"open_security_role: {recent[0].title} (posted {recent[0].posted_date}, "
                            f"within {t.strong_signal_role_max_age_days} days)")
        else:
            s.strong.append(f"open_security_role: {f.compliance_security_roles.value[0].title} "
                            f"(posting date unknown or older than {t.strong_signal_role_max_age_days} days)")
    if s.cert_in_progress:
        s.strong.append(f"certification_in_progress: {', '.join(f.certifications_named.value)}")

    # fit signals
    if f.enterprise_tier_present.known and f.enterprise_tier_present.value:
        s.fit.append("enterprise_tier_present")
    if f.enterprise_customers_named.known:
        s.fit.append(f"enterprise_customers_named: {', '.join(f.enterprise_customers_named.value[:3])}")
    if f.data_sensitivity.known and f.data_sensitivity.value in t.sensitive_data:
        s.fit.append(f"data_sensitivity: {f.data_sensitivity.value}")
    if f.regions_served.known and len(f.regions_served.value) >= 2:
        s.fit.append(f"regions_served: {', '.join(f.regions_served.value)}")
    if s.cert_held and len(f.certifications_named.value) >= 1:
        s.fit.append(f"certification_held: {', '.join(f.certifications_named.value)}")

    # stage proxies (explicit only)
    p = t.stage_proxies
    if p.named_round and f.funding_stage_stated.known:
        s.stage_proxies.append(f"named round: {f.funding_stage_stated.value}")
    if f.open_roles_total.known and f.open_roles_total.value >= p.min_open_roles:
        s.stage_proxies.append(f"{f.open_roles_total.value} open roles")
    if p.enterprise_tier and f.enterprise_tier_present.known and f.enterprise_tier_present.value:
        s.stage_proxies.append("enterprise pricing tier")
    return s


@dataclass
class Decision:
    moment: str
    tier: str | None            # None when moment is "none"
    rationale: str
    signals_used: list[str]
    notes: list[str]            # what code changed and why
    signals: Signals


def decide(proposal: QualifyOutput, f: ExtractedFields, sig: Signals) -> Decision:
    notes: list[str] = []
    verified = {n for n in ExtractedFields.model_fields if getattr(f, n).known}
    used = [x for x in proposal.signals_used if x in verified]
    dropped = [x for x in proposal.signals_used if x not in verified]
    if dropped:
        notes.append(f"ignored signals not backed by verified facts: {', '.join(dropped)}")

    if proposal.moment == "none":
        if sig.strong:
            # code never upgrades, but it won't silently drop a company with a verified trigger either:
            # the disagreement goes to a human (pipeline routes moment "disputed" to the review queue)
            return Decision("disputed", None, proposal.rationale, used,
                            notes + ["model found no buying moment, but code sees a verified strong signal: "
                                     + sig.strong[0]], sig)
        return Decision("none", None, proposal.rationale, used, notes + ["model found no buying moment"], sig)

    code_tier = "A" if sig.strong else ("B" if sig.fit else "C")
    reason = ("strong signal: " + sig.strong[0]) if sig.strong else \
             ("fit signals only: " + "; ".join(sig.fit)) if sig.fit else "no strong or fit signals verified"

    if proposal.moment == "expansion" and not sig.cert_held:
        code_tier, reason = "C", "moment 'expansion' needs a verified certification the company already holds"
    elif proposal.moment == "first_certification" and sig.cert_held:
        code_tier, reason = "C", ("moment 'first_certification' contradicts verified certification: "
                                  + ", ".join(f.certifications_named.value))
    elif not sig.stage_proxies and code_tier != "C":
        code_tier, reason = "C", "stage unknown: no named round, open-role count or enterprise tier verified"

    final = max(proposal.tier, code_tier, key=RANK.get)
    if final != proposal.tier:
        notes.append(f"downgraded {proposal.tier}->{final}: {reason}")
    else:
        notes.append(f"code allows {code_tier} ({reason})")
    if sig.stage_proxies:
        notes.append("stage proxies: " + "; ".join(sig.stage_proxies))
    return Decision(proposal.moment, final, proposal.rationale, used, notes, sig)


def qualify(llm: LLMProvider, domain: str, f: ExtractedFields, cfg: Config, today: date | None = None,
            record_id: str | None = None) -> tuple[Decision, LLMResult]:
    today = today or datetime.now(timezone.utc).date()
    facts = {}
    for n in ExtractedFields.model_fields:
        ev = getattr(f, n)
        if ev.known:
            v = ev.value
            if isinstance(v, list):
                v = [x.model_dump() if hasattr(x, "model_dump") else x for x in v]
            facts[n] = {"value": v, "quote": ev.evidence_quote}
    user = (f"Company: {domain}\nToday: {today.isoformat()}\n\nVerified facts (JSON):\n"
            + json.dumps(facts, indent=1, ensure_ascii=False))
    res = llm.complete_json("qualify", PROMPT.read_text(encoding="utf-8"), user, QualifyOutput, record_id)
    sig = compute_signals(f, cfg, today)
    return decide(res.parsed, f, sig), res
