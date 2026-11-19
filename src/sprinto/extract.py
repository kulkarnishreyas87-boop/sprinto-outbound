"""S2 Extract: the model proposes facts with quotes; code verifies every quote.

A field is kept only if its evidence_quote appears (after normalisation) in the cleaned
text of a page we actually fetched. Otherwise it becomes unknown. Missing is not negative.
"""
from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Literal

from pydantic import BaseModel

from .clean import normalize, tag_for_prompt
from .config import ROOT
from .llm import LLMProvider, LLMResult
from .models import Evidence, ExtractedFields, Role

PROMPT = ROOT / "prompts" / "extract.md"
MIN_QUOTE_WORDS = 3


# ---------- schema the model fills (all keys required, nullable) ----------

class _Q(BaseModel):
    evidence_quote: str | None
    source_url: str | None


class QStr(_Q):
    value: str | None


class QBool(_Q):
    value: bool | None


class QInt(_Q):
    value: int | None


class QList(_Q):
    value: list[str] | None


class RoleOut(BaseModel):
    title: str
    posted_date: str | None
    url: str | None


class QRoles(_Q):
    value: list[RoleOut] | None


class QBuyer(_Q):
    value: Literal["b2b", "b2c", "unclear"] | None


class QSensitivity(_Q):
    value: Literal["pii", "health", "payments", "none_stated"] | None


class ExtractOutput(BaseModel):
    product_summary: QStr
    buyer_type: QBuyer
    is_saas: QBool
    is_agency_or_services: QBool
    is_compliance_vendor: QBool
    certifications_named: QList
    compliance_tool_named: QStr
    enterprise_tier_present: QBool
    enterprise_customers_named: QList
    regions_served: QList
    open_roles_total: QInt
    compliance_security_roles: QRoles
    funding_stage_stated: QStr
    data_sensitivity: QSensitivity


# Named items must literally appear in the quote, so the model can't add extras.
_ITEMS_MUST_BE_IN_QUOTE = {"certifications_named", "enterprise_customers_named", "compliance_tool_named"}


def _squash(s: str) -> str:
    return normalize(s).replace(" ", "").replace("-", "")


def _canon_url(u: str) -> str:
    return u.rstrip("/").lower()


def verify_field(name: str, proposed: _Q, pages: dict[str, str]) -> tuple[Evidence, str | None]:
    """Return (evidence, rejection_reason). Evidence is unknown when rejected."""
    value = getattr(proposed, "value", None)
    quote = proposed.evidence_quote
    if value is None or value == [] or value == "":
        return Evidence(), None
    if value == "none_stated" and not quote:   # the model saying "nothing stated" is just unknown
        return Evidence(), None
    if not quote or len(quote.split()) < MIN_QUOTE_WORDS:
        return Evidence(), "quote_missing_or_too_short"
    nq = normalize(quote)
    by_url = {_canon_url(u): u for u in pages}
    cited = by_url.get(_canon_url(proposed.source_url or ""))
    # Prefer the cited page; accept another fetched page if the quote is there instead.
    candidates = ([cited] if cited else []) + [u for u in pages if u != cited]
    source = next((u for u in candidates if nq in normalize(pages[u])), None)
    if source is None and isinstance(value, list) and " · " in quote:
        # list pages: the model may join separate items; accept only if EVERY piece is verbatim on one page.
        # The stored quote uses " … " so readers can see it isn't one contiguous span.
        pieces = [p.strip() for p in quote.split(" · ") if p.strip()]
        source = next((u for u in candidates if all(normalize(p) in normalize(pages[u]) for p in pieces)), None)
        if source is not None:
            quote = " … ".join(pieces)
    if source is None:
        return Evidence(), "quote_not_found"
    if name in _ITEMS_MUST_BE_IN_QUOTE:
        sq = _squash(quote)
        if isinstance(value, list):
            value = [v for v in value if _squash(v) in sq]
            if not value:
                return Evidence(), "items_not_in_quote"
        elif _squash(value) not in sq:
            return Evidence(), "items_not_in_quote"
    if name == "compliance_security_roles":
        sq = _squash(quote)
        seen: set[str] = set()
        roles = []
        for r in value:   # titles must be quoted; one entry per title (same role in two locations = one role)
            key = _squash(r.title)
            if key in sq and key not in seen:
                seen.add(key)
                roles.append(Role(**r.model_dump()))
        value = roles
        if not value:
            return Evidence(), "items_not_in_quote"
    return Evidence(value=value, evidence_quote=quote, source_url=source, verified=True), None


def verify_all(out: ExtractOutput, pages: dict[str, str]) -> tuple[ExtractedFields, dict[str, str]]:
    fields: dict[str, Evidence] = {}
    rejected: dict[str, str] = {}
    for name in ExtractOutput.model_fields:
        ev, why = verify_field(name, getattr(out, name), pages)
        fields[name] = ev
        if why:
            rejected[name] = why
    return ExtractedFields.model_validate({k: v.model_dump() for k, v in fields.items()}), rejected


def merge(old: ExtractedFields, new: ExtractedFields) -> ExtractedFields:
    """Verified facts are never replaced by unknown. Newer verified facts win."""
    data = {}
    for name in ExtractedFields.model_fields:
        o, n = getattr(old, name), getattr(new, name)
        data[name] = (n if n.known else o).model_dump()
    return ExtractedFields.model_validate(data)


# ---------- support check: does the quote actually back the value? ----------

SUPPORT_PROMPT = ROOT / "prompts" / "support.md"


class Judgment(BaseModel):
    field: str
    supported: bool
    reason: str


class SupportOutput(BaseModel):
    judgments: list[Judgment]


def check_support(llm: LLMProvider, fields: ExtractedFields,
                  record_id: str | None = None) -> tuple[ExtractedFields, dict[str, str], LLMResult | None]:
    """Second-pass audit. Can only downgrade a verified field to unknown, never upgrade.

    Fails closed: a verified field the model doesn't judge as supported becomes unknown.
    """
    import json

    facts = []
    for name in ExtractedFields.model_fields:
        ev = getattr(fields, name)
        if ev.known:
            val = ev.value
            if isinstance(val, list):
                val = [v.model_dump() if hasattr(v, "model_dump") else v for v in val]
            facts.append({"field": name, "value": val, "quote": ev.evidence_quote})
    if not facts:
        return fields, {}, None
    user = "Facts to audit (JSON):\n" + json.dumps(facts, indent=1, ensure_ascii=False)
    res = llm.complete_json("support", SUPPORT_PROMPT.read_text(encoding="utf-8"), user, SupportOutput, record_id)
    verdicts = {j.field: j for j in res.parsed.judgments}
    data, rejected = fields.model_dump(), {}
    for f in facts:
        j = verdicts.get(f["field"])
        if j is None or not j.supported:
            data[f["field"]] = Evidence().model_dump()
            rejected[f["field"]] = ("quote_does_not_support_value: " + j.reason) if j else "support_not_judged"
    return ExtractedFields.model_validate(data), rejected, res


def build_prompt(domain: str, pages: dict[str, str]) -> tuple[str, str]:
    system = PROMPT.read_text(encoding="utf-8")
    body = "\n\n".join(tag_for_prompt(u, t) for u, t in pages.items())
    user = Template("Company domain: $domain\n\n$pages").substitute(domain=domain, pages=body)
    return system, user


def extract(llm: LLMProvider, domain: str, pages: dict[str, str],
            record_id: str | None = None) -> tuple[ExtractedFields, dict[str, str], LLMResult]:
    system, user = build_prompt(domain, pages)
    res = llm.complete_json("extract", system, user, ExtractOutput, record_id)
    fields, rejected = verify_all(res.parsed, pages)
    return fields, rejected, res
