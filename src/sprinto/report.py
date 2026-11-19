"""`cli report`: explain every decision in a run from its stored decision records.

For each company: what was decided and why, the evidence behind each fact, what was
rejected, why this contact, and why the email says what it says. Then run totals.
"""
from __future__ import annotations

from collections import Counter

from .config import Claims
from .models import DecisionRecord, ExtractedFields, Status

GATE_FIELD = {
    "dropped:b2c": "buyer_type",
    "dropped:agency_or_services": "is_agency_or_services",
    "dropped:compliance_vendor": "is_compliance_vendor",
    "dropped:competitor_tool": "compliance_tool_named",
}


def _short(text: str | None, n: int) -> str:
    if text is None:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _item(x) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return str(x.get("title", x))
    if hasattr(x, "model_fields") and "title" in x.model_fields:   # Role
        return str(x.title)
    return str(x)


def _val(v) -> str:
    if isinstance(v, list):
        return ", ".join(_item(x) for x in v)
    return str(v)


def _fact(fields: ExtractedFields, name: str, q: int) -> str:
    ev = getattr(fields, name)
    if not ev.known:
        return f"{name} = unknown"
    where = {"raw_html": " [found in page HTML, not visible text]", "api": " [job-board API]"}.get(ev.kind, "")
    return f'{name} = {_short(_val(ev.value), 60)}  ← "{_short(ev.evidence_quote, q)}" ({ev.source_url}){where}'


def decision_line(rec: DecisionRecord, q: int) -> list[str]:
    s = rec.status
    if s == Status.dropped:
        out = [f"Dropped by hard gate{'s' if len(rec.gates_hit) > 1 else ''}: {', '.join(rec.gates_hit)}"]
        for g in rec.gates_hit:
            if g in GATE_FIELD:
                out.append("  because " + _fact(rec.fields, GATE_FIELD[g], q))
        return out
    if s == Status.needs_review:
        out = [f"Needs review: {rec.reason_code}"]
        out += [f"  {f.stage}: {f.reason_code} {f.detail}".rstrip() for f in rec.failures]
        return out
    if s == Status.failed:
        f = rec.failures[-1] if rec.failures else None
        return [f"Failed at {f.stage if f else '?'}: {rec.reason_code}" + (f" ({_short(f.detail, 120)})" if f and f.detail else "")]
    if s == Status.drafted:
        return ["Passed gates; contact chosen; draft waiting for review (`send <record_id>` to approve)"]
    if s == Status.sent:
        return [f"Sent to MY_EMAIL at {rec.send.sent_at:%Y-%m-%d %H:%M} UTC, message id {rec.send.message_id}"
                + (f"; review: {rec.review.decision}" if rec.review.decision else "")]
    return [f"Status: {s.value}"]


def render_record(rec: DecisionRecord, claims: Claims | None = None, full: bool = False,
                  public: bool = False) -> str:
    """public=True hides personal data (people's names, pains, email bodies, review notes)."""
    if public:
        full = False
    q = 10_000 if full else 90
    L = [f"━━ {rec.domain}  →  {rec.status.value.upper()}"
         + (f" ({rec.reason_code})" if rec.reason_code and rec.status != Status.drafted else "")
         + f"   record {rec.record_id}"]
    L += decision_line(rec, q)

    L.append(f"Pages fetched ({len(rec.pages_fetched)}; loop iterations {rec.loop_iterations}):")
    for p in rec.pages_fetched:
        extra = ("" if p.status == "ok" else f", {p.status}") + (f"; {p.note}" if p.note else "")
        L.append(f"  {p.page_type}: {p.url} [{p.method}, {p.words} words{extra}]")
    if rec.pages_discovered:
        fetched = {p.page_type for p in rec.pages_fetched}
        skipped = [t for t in rec.pages_discovered if t not in fetched]
        if skipped:
            L.append(f"  discovered but not needed: {', '.join(skipped)}")

    known = [n for n in ExtractedFields.model_fields if getattr(rec.fields, n).known]
    L.append(f"Verified facts ({len(known)}):")
    L += [f"  {_fact(rec.fields, n, q)}" for n in known] or ["  none"]
    if rec.rejections:
        L.append(f"Rejected proposals ({len(rec.rejections)}):")
        for r in rec.rejections:
            prop = f' value={_short(_val(r.proposed_value), 40)}' if r.proposed_value not in (None, [], "") else ""
            quote = f' quote="{_short(r.evidence_quote, 60 if not full else q)}"' if r.evidence_quote else ""
            L.append(f"  [{r.stage}] {r.field}: {_short(r.reason, 110 if not full else q)}{prop}{quote}")

    if rec.tier or rec.moment:
        model_part, _, code_part = rec.tier_rationale.partition(" | code: ")
        L.append(f"Qualification: moment={rec.moment} tier={rec.tier or '-'}")
        L.append(f"  model: {_short(model_part, 220 if not full else q)}")
        if code_part:
            for note in code_part.split(" || "):
                L.append(f"  code: {_short(note, 200 if not full else q)}")
        if rec.signals_used:
            L.append(f"  signals used: {', '.join(rec.signals_used)}")
    elif rec.status in (Status.drafted, Status.sent, Status.needs_review):
        L.append("Qualification: not assessed yet (S5 arrives in milestone 5)")

    if rec.contact:
        c = rec.contact
        L.append(f"Contact: {'(name hidden)' if public else c.name}, {c.title or 'title unknown'}"
                 + (f" (Hunter confidence {c.hunter_confidence})" if c.hunter_confidence is not None else ""))
        L.append(f"  why: {_short(c.why_chosen, 200 if not full else q)}")
        if c.alternatives:
            alts = [f"{'' if public else str(a.get('name')) + ' '}({a.get('title')}): "
                    f"{a.get('excluded') or 'rank ' + str(a.get('rank'))}"
                    for a in c.alternatives]
            L.append(f"  also considered ({len(alts)}): " + _short("; ".join(alts), 220 if not full else q))

    if rec.pains:
        L.append("Pains (hypotheses):")
        for i, p in enumerate(rec.pains, 1):
            text = '[hidden in public view]' if public else _short(p.hypothesis, 160 if not full else q)
            L.append(f"  {i}. {text}  [{', '.join(p.linked_fields)}]")

    if rec.qa_results:
        passed = [r.variant for r in rec.qa_results if r.passed]
        L.append(f"QA: {len(rec.drafts)} variant(s), passed: {', '.join(map(str, passed)) or 'none'}")
        for r in rec.qa_results:
            if not r.passed:
                L.append(f"  v{r.variant} failed: {_short('; '.join(r.failures), 200 if not full else q)}")
    if rec.judge.chosen_variant is not None:
        if rec.judge.skipped:
            L.append(f"Judge: not called ({rec.judge.skipped}); chose v{rec.judge.chosen_variant}")
        else:
            scores = ", ".join(f"v{k}={v}" for k, v in rec.judge.scores.items())
            why = '' if public else f" — {_short(rec.judge.rationale, 180)}"
            L.append(f"Judge: chose v{rec.judge.chosen_variant} ({scores}){why}")

    if rec.drafts and rec.chosen_draft is not None:
        d = next(x for x in rec.drafts if x.variant == rec.chosen_draft)
        body = rec.review.edited_text or d.body
        L.append(f'Email v{d.variant}: "{d.subject}" ({len(body.split())} words, {body.count("?")} question mark(s))')
        if d.hook_field:
            L.append(f"  hook: {_fact(rec.fields, d.hook_field, q)}")
        if d.pain_index and rec.pains and d.pain_index <= len(rec.pains):
            ptxt = '[hidden in public view]' if public else _short(rec.pains[d.pain_index - 1].hypothesis, 120)
            L.append(f"  pain: #{d.pain_index} {ptxt}")
        if d.claim_id:
            text = next((c.text for c in (claims.claims if claims else []) if c.id == d.claim_id), "")
            L.append(f'  proof point: {d.claim_id}' + (f' "{_short(text, 100)}"' if text else ""))
        if d.facts_used:
            L.append(f"  facts relied on: {', '.join(d.facts_used)}")
        if full:
            L += ["", *("    " + line for line in body.splitlines()), ""]

    rv = rec.review
    for h in rv.history:
        L.append(f"Earlier review: {h.get('decision')}" + (f" ({h.get('reason_code')})" if h.get("reason_code") else "")
                 + (f" — {_short(h.get('note'), 100)}" if h.get("note") and not public else ""))
    if rv.decision:
        bits = [rv.decision] + ([rv.reason_code] if rv.reason_code else [])
        if rv.edit_ratio is not None and rv.decision == "edit":
            bits.append(f"edit ratio {rv.edit_ratio:.2f}" + (", REWRITE" if rv.rewrite else ""))
        L.append("Review: " + ", ".join(bits) + (f" — {_short(rv.note, 120)}" if rv.note and not public else ""))
    o = rec.outcome
    if o.recorded_at:
        L.append(f"Outcome ({o.source}): replied={o.replied or '-'} meeting={o.meeting} bounced={o.bounced}")

    ms = sum(rec.timings.values())
    L.append(f"Cost: ${rec.cost.usd_estimate:.4f} | tokens {rec.cost.tokens_in:,} in / {rec.cost.tokens_out:,} out"
             f" | Hunter calls {rec.cost.hunter_calls} | time {ms / 1000:.1f}s")
    return "\n".join(L)


def render_totals(records: list[DecisionRecord]) -> str:
    n = len(records)
    if not n:
        return "No records."
    counts = Counter(r.status.value for r in records)
    usd = sum(r.cost.usd_estimate for r in records)
    secs = sum(sum(r.timings.values()) for r in records) / 1000
    hunter = sum(r.cost.hunter_calls for r in records)
    reasons = Counter(r.reason_code for r in records if r.reason_code and r.status != Status.drafted)
    lines = [
        "━━ Run totals",
        f"Companies: {n} | " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
        f"Cost: ${usd:.4f} total, ${usd / n:.4f} per company | Time: {secs:.1f}s total, {secs / n:.1f}s per company",
        f"Hunter calls: {hunter} | Tokens: {sum(r.cost.tokens_in for r in records):,} in / "
        f"{sum(r.cost.tokens_out for r in records):,} out",
    ]
    if reasons:
        lines.append("Reasons: " + ", ".join(f"{k} ×{v}" for k, v in reasons.most_common()))
    return "\n".join(lines)


def render_run(run_id: str, config_version: int | None, records: list[DecisionRecord],
               claims: Claims | None = None, full: bool = False, public: bool = False) -> str:
    head = f"Run {run_id}" + (f" (config v{config_version})" if config_version is not None else "")
    order = {Status.sent: 0, Status.drafted: 1, Status.needs_review: 2, Status.dropped: 3, Status.failed: 4}
    recs = sorted(records, key=lambda r: (order.get(r.status, 9), r.domain))
    return "\n\n".join([head, *(render_record(r, claims, full, public) for r in recs), render_totals(records)])
