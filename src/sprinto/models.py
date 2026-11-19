"""Pydantic schemas for every stage input/output and the decision record."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- evidence ----------

class Evidence(BaseModel, Generic[T]):
    """One extracted fact. `verified` is set by code, never by the model."""

    value: T | None = None
    evidence_quote: str | None = None
    source_url: str | None = None
    verified: bool = False
    # where the quote was checked: visible page text (default), a job-board API, or raw page HTML
    kind: Literal["text", "api", "raw_html"] = "text"

    @property
    def known(self) -> bool:
        return self.verified and self.value is not None

    @classmethod
    def unknown(cls) -> "Evidence[T]":
        return cls()


class Role(BaseModel):
    title: str
    posted_date: str | None = None
    url: str | None = None


class ExtractedFields(BaseModel):
    product_summary: Evidence[str] = Field(default_factory=Evidence[str])
    buyer_type: Evidence[Literal["b2b", "b2c", "unclear"]] = Field(default_factory=Evidence)
    is_saas: Evidence[bool] = Field(default_factory=Evidence[bool])
    is_agency_or_services: Evidence[bool] = Field(default_factory=Evidence[bool])
    is_compliance_vendor: Evidence[bool] = Field(default_factory=Evidence[bool])
    certifications_named: Evidence[list[str]] = Field(default_factory=Evidence[list[str]])
    compliance_tool_named: Evidence[str] = Field(default_factory=Evidence[str])
    enterprise_tier_present: Evidence[bool] = Field(default_factory=Evidence[bool])
    enterprise_customers_named: Evidence[list[str]] = Field(default_factory=Evidence[list[str]])
    regions_served: Evidence[list[str]] = Field(default_factory=Evidence[list[str]])
    open_roles_total: Evidence[int] = Field(default_factory=Evidence[int])
    compliance_security_roles: Evidence[list[Role]] = Field(default_factory=Evidence[list[Role]])
    funding_stage_stated: Evidence[str] = Field(default_factory=Evidence[str])
    data_sensitivity: Evidence[Literal["pii", "health", "payments", "none_stated"]] = Field(default_factory=Evidence)


# ---------- statuses and reasons ----------

class Status(str, Enum):
    pending = "pending"
    drafted = "drafted"          # waiting for review
    sent = "sent"
    dropped = "dropped"
    needs_review = "needs_review"
    failed = "failed"


class StageFailure(BaseModel):
    stage: str
    reason_code: str
    detail: str = ""


class Rejection(BaseModel):
    """A fact the model proposed that code (S2) or the support check (S2b) refused."""
    stage: str
    field: str
    reason: str
    proposed_value: Any = None
    evidence_quote: str | None = None
    source_url: str | None = None


# ---------- pages ----------

class PageFetch(BaseModel):
    url: str
    page_type: str = "home"
    method: Literal["httpx", "playwright", "jobboard", "cache"] = "httpx"
    words: int = 0
    status: Literal["ok", "blocked", "robots_disallowed", "timeout", "empty_after_render", "error"] = "ok"
    note: str = ""


# ---------- contact ----------

class Contact(BaseModel):
    name: str
    title: str | None = None
    email: str
    hunter_confidence: int | None = None
    why_chosen: str = ""
    alternatives: list[dict[str, Any]] = Field(default_factory=list)


# ---------- drafting ----------

class Pain(BaseModel):
    hypothesis: str
    linked_fields: list[str]


class Draft(BaseModel):
    variant: int
    subject: str
    body: str
    hook_field: str | None = None
    claim_id: str | None = None
    pain_index: int | None = None
    facts_used: list[str] = Field(default_factory=list)


class Judge(BaseModel):
    chosen_variant: int | None = None
    rationale: str = ""
    scores: dict[str, int] = Field(default_factory=dict)   # "variant" -> score
    skipped: str = ""                                        # why the judge wasn't called


class QAResult(BaseModel):
    variant: int
    passed: bool
    failures: list[str] = Field(default_factory=list)


class Review(BaseModel):
    decision: Literal["approve", "edit", "reject", "qualify", "drop", "needs_more_info"] | None = None
    reason_code: str | None = None
    note: str = ""
    edited_text: str | None = None
    edited_subject: str | None = None
    edit_ratio: float | None = None
    hook_or_pain_changed: bool | None = None
    rewrite: bool | None = None        # edit_ratio > threshold or hook/pain changed
    reviewed_at: datetime | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)   # earlier verdicts, oldest first


class SendInfo(BaseModel):
    message_id: str | None = None
    sent_at: datetime | None = None
    provider: str = "resend"


class Outcome(BaseModel):
    replied: Literal["positive", "neutral", "negative"] | None = None
    meeting: bool | None = None
    bounced: bool | None = None
    source: Literal["manual", "auto"] = "manual"   # room for automatic reply tracking later
    recorded_at: datetime | None = None


class Cost(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    usd_estimate: float = 0.0
    hunter_calls: int = 0


# ---------- decision record ----------

class DecisionRecord(BaseModel):
    record_id: str
    run_id: str
    config_version: int
    domain: str
    status: Status = Status.pending
    reason_code: str | None = None
    pages_fetched: list[PageFetch] = Field(default_factory=list)
    pages_discovered: dict[str, str] = Field(default_factory=dict)   # page_type -> url
    fields: ExtractedFields = Field(default_factory=ExtractedFields)
    rejections: list[Rejection] = Field(default_factory=list)
    gates_hit: list[str] = Field(default_factory=list)
    loop_iterations: int = 0
    moment: Literal["first_certification", "expansion", "none"] | None = None
    tier: Literal["A", "B", "C"] | None = None
    tier_rationale: str = ""
    signals_used: list[str] = Field(default_factory=list)
    contact: Contact | None = None
    pains: list[Pain] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    chosen_draft: int | None = None
    qa_results: list[QAResult] = Field(default_factory=list)
    judge: Judge = Field(default_factory=Judge)
    review: Review = Field(default_factory=Review)
    send: SendInfo = Field(default_factory=SendInfo)
    outcome: Outcome = Field(default_factory=Outcome)
    cost: Cost = Field(default_factory=Cost)
    timings: dict[str, int] = Field(default_factory=dict)   # stage -> ms
    failures: list[StageFailure] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def finish(self, status: Status, reason_code: str | None = None) -> "DecisionRecord":
        self.status = status
        self.reason_code = reason_code
        self.updated_at = utcnow()
        return self
