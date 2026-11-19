"""S9 QA: code checks first, then a judge model picks the best surviving variant.

Code checks (limits in config.qa):
- body word count, exactly one question mark in the email
- greeting uses the recipient's first name; no other candidate's name appears
- hook and every declared fact are verified fields; pain index valid; claim approved
- every number appears in the approved claim used or in the evidence
- every named framework appears in the evidence or the claim used; never a competitor
- no banned phrases; subject short, no "Re:"/"Fwd:"
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel

from .clean import normalize
from .config import ROOT, Claims, Config
from .llm import LLMProvider, LLMResult
from .models import Contact, Draft, ExtractedFields, Judge, Pain, QAResult

JUDGE_PROMPT = ROOT / "prompts" / "judge.md"
_NUM = re.compile(r"(?<![A-Za-z])\$?\d[\d,]*(?:\.\d+)?%?")


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(s))


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(normalize(word))}(?![a-z0-9])", normalize(text)) is not None


def _numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", "").replace("$", "").rstrip("%") for m in _NUM.finditer(text)}


def evidence_corpus(fields: ExtractedFields) -> str:
    parts = []
    for name in ExtractedFields.model_fields:
        ev = getattr(fields, name)
        if ev.known:
            parts.append(ev.evidence_quote or "")
            v = ev.value
            if isinstance(v, list):
                parts += [x.title if hasattr(x, "title") and not isinstance(x, str) else str(x) for x in v]
            else:
                parts.append(str(v))
    return "\n".join(parts)


def qa_check(d: Draft, *, fields: ExtractedFields, claims: Claims, contact: Contact, pains: list[Pain],
             cfg: Config) -> QAResult:
    q, f = cfg.qa, []
    body, subject = d.body, d.subject
    text = subject + "\n" + body
    verified = {n for n in ExtractedFields.model_fields if getattr(fields, n).known}

    n = len(body.split())
    if not q.min_words <= n <= q.max_words:
        f.append(f"length: {n} words (allowed {q.min_words}-{q.max_words})")
    qm = text.count("?")
    if qm != q.question_marks:
        f.append(f"question_marks: {qm} (need exactly {q.question_marks})")

    # recipient
    first = contact.name.split()[0] if contact.name else ""
    greeting = body.strip().splitlines()[0] if body.strip() else ""
    if not first or not _has_word(greeting, first):
        f.append(f"greeting: first line does not address {first or 'the recipient'}")
    for alt in contact.alternatives:
        other = (alt.get("name") or "").split()
        if other and other[0].lower() != first.lower() and _has_word(body, other[0]):
            f.append(f"wrong_name: mentions {other[0]}, who is not the recipient")

    # facts, pain, claim
    if d.hook_field not in verified:
        f.append(f"hook: '{d.hook_field}' is not a verified fact")
    bad_facts = [x for x in d.facts_used if x not in verified]
    if bad_facts:
        f.append(f"facts: not verified: {', '.join(bad_facts)}")
    if pains and not (d.pain_index and 1 <= d.pain_index <= len(pains)):
        f.append(f"pain: index {d.pain_index} is not one of the {len(pains)} pains")
    claim = next((c for c in claims.approved() if c.id == d.claim_id), None)
    if claim is None:
        f.append(f"claim: '{d.claim_id}' is not an approved claim")

    allowed_corpus = evidence_corpus(fields) + "\n" + (claim.text if claim else "")
    # frameworks: named only if in evidence or the claim used (strip them before the number check)
    stripped = text
    for fw in q.frameworks:
        if _squash(fw) and _squash(fw) in _squash(text):
            if _squash(fw) not in _squash(allowed_corpus):
                f.append(f"unsupported_framework: {fw}")
            pattern = r"\s*".join(re.escape(w) for w in fw.split())
            stripped = re.sub(pattern, " ", stripped, flags=re.I)
    # numbers
    extra = sorted(_numbers(stripped) - _numbers(allowed_corpus))
    if extra:
        f.append(f"numbers: {', '.join(extra)} not in the evidence or the approved claim")
    # competitors and banned phrases
    for tool in cfg.gates.competitor_tools:
        if _has_word(text, tool):
            f.append(f"competitor: mentions {tool}")
    # a name doesn't tell you someone's pronouns: never guess them in a prospect email
    pron = re.search(r"\b(he|she|him|her|his|hers|himself|herself)\b", body, re.I)
    if pron:
        f.append(f"pronoun: uses '{pron.group(0)}'; address the recipient as 'you' or by role")
    for phrase in q.banned_phrases:
        if normalize(phrase) in normalize(text):
            f.append(f"banned_phrase: '{phrase}'")
    # subject
    if re.match(r"^\s*(re|fwd?|fw)\s*:", subject, re.I):
        f.append("subject: starts with Re:/Fwd:")
    if len(subject.split()) > q.subject_max_words:
        f.append(f"subject: {len(subject.split())} words (max {q.subject_max_words})")
    if "\n" in subject:
        f.append("subject: contains a line break")
    return QAResult(variant=d.variant, passed=not f, failures=f)


# ---------- judge ----------

class Score(BaseModel):
    variant: int
    score: int
    note: str


class JudgeOutput(BaseModel):
    scores: list[Score]
    chosen_variant: int
    rationale: str


def judge(llm: LLMProvider, survivors: list[Draft], *, fields: ExtractedFields, contact: Contact,
          pains: list[Pain], record_id: str | None = None) -> tuple[Judge, LLMResult | None]:
    if len(survivors) == 1:
        return Judge(chosen_variant=survivors[0].variant, skipped="only one variant passed QA"), None
    from .draft import verified_facts

    payload = {
        "recipient": {"name": contact.name, "title": contact.title},
        "pains": [p.hypothesis for p in pains],
        "verified_facts": {k: v["quote"] for k, v in verified_facts(fields).items()},
        "variants": [{"variant": d.variant, "subject": d.subject, "body": d.body} for d in survivors],
    }
    res = llm.complete_json("judge", JUDGE_PROMPT.read_text(encoding="utf-8"),
                            json.dumps(payload, indent=1, ensure_ascii=False), JudgeOutput, record_id)
    ids = {d.variant for d in survivors}
    chosen = res.parsed.chosen_variant
    rationale = res.parsed.rationale
    if chosen not in ids:   # code decides: an invalid pick falls back to the best-scored survivor
        valid = [s for s in res.parsed.scores if s.variant in ids]
        chosen = max(valid, key=lambda s: s.score).variant if valid else survivors[0].variant
        rationale = f"(judge picked an invalid variant; code chose {chosen}) " + rationale
    return Judge(chosen_variant=chosen, rationale=rationale,
                 scores={str(s.variant): s.score for s in res.parsed.scores if s.variant in ids}), res
