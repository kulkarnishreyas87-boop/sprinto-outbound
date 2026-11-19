"""S10 Review and S11 send-on-approve. The Growth Lead decides; code records why.

Drafts: approve (send), edit (send the edited text), reject. Edits and rejects need a reason code.
Review-queue items: qualify, drop, needs_more_info. Every decision is logged to the feedback table.
"""
from __future__ import annotations

import difflib
from datetime import datetime, timezone

from .config import Config, Settings
from .models import DecisionRecord, Draft, ExtractedFields, Status
from .send import send_email
from .store import Store

TIER_ORDER = {"A": 0, "B": 1, "C": 2}


class ReviewError(ValueError):
    pass


def evidence_strength(rec: DecisionRecord) -> int:
    verified = sum(1 for n in ExtractedFields.model_fields if getattr(rec.fields, n).known)
    return verified + 2 * len(rec.signals_used)


def order_queue(recs: list[DecisionRecord]) -> list[DecisionRecord]:
    """By tier (A, B, C, untiered), then strength of evidence, then domain."""
    return sorted(recs, key=lambda r: (TIER_ORDER.get(r.tier or "", 3), -evidence_strength(r), r.domain))


def edit_ratio(original: str, final: str) -> float:
    """Normalised word-level edit distance: 0 = unchanged, 1 = completely rewritten."""
    a, b = original.split(), final.split()
    if not a and not b:
        return 0.0
    return round(1 - difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio(), 3)


def chosen(rec: DecisionRecord) -> Draft:
    d = next((x for x in rec.drafts if x.variant == rec.chosen_draft), None)
    if d is None:
        raise ReviewError("record has no chosen draft")
    return d


def _check_reason(code: str | None, cfg: Config) -> str:
    if not code or code not in cfg.feedback.reason_codes:
        raise ReviewError(f"reason code required, one of: {', '.join(cfg.feedback.reason_codes)}")
    return code


def _stamp(rec: DecisionRecord, decision: str, reason: str | None, note: str) -> None:
    if rec.review.decision is not None:
        rec.review.history.append(rec.review.model_dump(mode="json", exclude={"history"}))
    rec.review.decision = decision
    rec.review.reason_code = reason
    rec.review.note = note
    rec.review.reviewed_at = datetime.now(timezone.utc)


def send_approved(rec: DecisionRecord, store: Store, settings: Settings, transport=None) -> DecisionRecord:
    """Send an approved (or edited) draft to MY_EMAIL. On failure the approval is kept, status stays drafted."""
    if rec.review.decision not in ("approve", "edit"):
        raise ReviewError("record has not been approved")
    d = chosen(rec)
    try:
        info = send_email(settings, subject=rec.review.edited_subject or d.subject,
                          body=rec.review.edited_text or d.body,
                          prospect_name=rec.contact.name if rec.contact else "unknown", company=rec.domain,
                          transport=transport)
    finally:
        store.save_record(rec)      # the human decision is never lost, even if the send fails
    rec.send = info
    rec.finish(Status.sent)
    store.save_record(rec)
    return rec


def _send(rec, store, settings, transport):
    return send_approved(rec, store, settings, transport)


# ---------- drafts ----------

def approve(rec: DecisionRecord, store: Store, settings: Settings, *, note: str = "", send: bool = True,
            transport=None) -> DecisionRecord:
    if rec.status != Status.drafted:
        raise ReviewError(f"only drafted records can be approved (this one is {rec.status.value})")
    chosen(rec)
    _stamp(rec, "approve", None, note)
    rec.review.edit_ratio, rec.review.rewrite = 0.0, False
    store.log_feedback(rec, "review", "approve", note=note, edit_ratio=0.0, rewrite=False, system_status="drafted")
    if send:
        return send_approved(rec, store, settings, transport)
    store.save_record(rec)
    return rec


def edit(rec: DecisionRecord, store: Store, settings: Settings, cfg: Config, *, new_body: str,
         reason_code: str, hook_or_pain_changed: bool, new_subject: str | None = None, note: str = "",
         send: bool = True, transport=None) -> DecisionRecord:
    if rec.status != Status.drafted:
        raise ReviewError(f"only drafted records can be edited (this one is {rec.status.value})")
    reason = _check_reason(reason_code, cfg)
    d = chosen(rec)
    if not new_body.strip():
        raise ReviewError("edited text is empty; use reject instead")
    ratio = edit_ratio(d.body, new_body)
    rewrite = ratio > cfg.feedback.rewrite_edit_ratio or hook_or_pain_changed
    _stamp(rec, "edit", reason, note)
    rec.review.edited_text = new_body.strip()
    rec.review.edited_subject = new_subject.strip() if new_subject and new_subject.strip() != d.subject else None
    rec.review.edit_ratio, rec.review.hook_or_pain_changed, rec.review.rewrite = ratio, hook_or_pain_changed, rewrite
    store.log_feedback(rec, "review", "edit", reason_code=reason, note=note, edit_ratio=ratio, rewrite=rewrite,
                       system_status="drafted",
                       data={"original": d.body, "final": rec.review.edited_text, "hook_field": d.hook_field,
                             "hook_or_pain_changed": hook_or_pain_changed})
    if send:
        return send_approved(rec, store, settings, transport)
    store.save_record(rec)
    return rec


def reject(rec: DecisionRecord, store: Store, cfg: Config, *, reason_code: str, note: str = "") -> DecisionRecord:
    if rec.status != Status.drafted:
        raise ReviewError(f"only drafted records can be rejected (this one is {rec.status.value})")
    reason = _check_reason(reason_code, cfg)
    _stamp(rec, "reject", reason, note)
    store.log_feedback(rec, "review", "reject", reason_code=reason, note=note, system_status="drafted",
                       data={"hook_field": chosen(rec).hook_field})
    rec.finish(Status.dropped, f"rejected:{reason}")
    store.save_record(rec)
    return rec


# ---------- review-queue verdicts ----------

VERDICTS = ("qualify", "drop", "needs_more_info")


def verdict(rec: DecisionRecord, store: Store, cfg: Config, *, decision: str, note: str = "",
            reason_code: str | None = None) -> DecisionRecord:
    if rec.status != Status.needs_review:
        raise ReviewError(f"verdicts are for review-queue items (this one is {rec.status.value})")
    if decision not in VERDICTS:
        raise ReviewError(f"verdict must be one of {', '.join(VERDICTS)}")
    if reason_code is not None:
        _check_reason(reason_code, cfg)
    system = rec.reason_code
    _stamp(rec, decision, reason_code, note)
    store.log_feedback(rec, "verdict", decision, reason_code=reason_code, note=note, system_status="needs_review",
                       data={"system_reason": system})
    if decision == "drop":
        rec.finish(Status.dropped, f"review:drop ({system})")
    store.save_record(rec)
    return rec


def can_resume(rec: DecisionRecord) -> bool:
    """A 'qualify' verdict can continue the pipeline only where S5 already set a real moment."""
    return rec.reason_code == "tier_c" and rec.moment in ("first_certification", "expansion")


# ---------- editor ----------

def editor_command() -> list[str]:
    import os
    import shlex
    import sys

    cmd = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if cmd:
        return shlex.split(cmd, posix=not sys.platform.startswith("win"))
    return ["notepad"] if sys.platform.startswith("win") else ["nano"]


def edit_in_editor(subject: str, body: str, runner=None) -> tuple[str, str]:
    """Open 'Subject: ...' + body in the editor; return (subject, body) after it closes."""
    import os
    import subprocess
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="sprinto-draft-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"Subject: {subject}\n\n{body}\n")
        (runner or subprocess.run)(editor_command() + [path], check=False)
        text = open(path, encoding="utf-8").read()
    finally:
        os.unlink(path)
    first, _, rest = text.partition("\n")
    if first.lower().startswith("subject:"):
        return first.split(":", 1)[1].strip(), rest.strip()
    return subject, text.strip()


# ---------- outcomes (S12, manual for now) ----------

_YES = {"yes": True, "y": True, "no": False, "n": False}


def record_outcome(rec: DecisionRecord, store: Store, *, replied: str | None = None, meeting: str | None = None,
                   bounced: str | None = None) -> DecisionRecord:
    if rec.status != Status.sent:
        raise ReviewError(f"outcomes are for sent records (this one is {rec.status.value})")
    if replied is not None and replied not in ("positive", "neutral", "negative"):
        raise ReviewError("--replied must be positive, neutral or negative")
    for name, val in (("meeting", meeting), ("bounced", bounced)):
        if val is not None and val.lower() not in _YES:
            raise ReviewError(f"--{name} must be yes or no")
    if replied is None and meeting is None and bounced is None:
        raise ReviewError("give at least one of --replied, --meeting, --bounced")
    o = rec.outcome
    if replied is not None:
        o.replied = replied
    if meeting is not None:
        o.meeting = _YES[meeting.lower()]
    if bounced is not None:
        o.bounced = _YES[bounced.lower()]
    o.source, o.recorded_at = "manual", datetime.now(timezone.utc)
    store.log_feedback(rec, "outcome", "outcome", system_status="sent",
                       data=o.model_dump(mode="json"))
    store.save_record(rec)
    return rec
