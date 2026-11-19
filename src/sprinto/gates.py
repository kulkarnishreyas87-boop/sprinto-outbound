"""S3 Hard gates (code only). Only VERIFIED facts can trigger a drop: unknown never does.

Pulled forward from milestone 4 so the slice never spends Hunter quota on a company
that should be dropped. Milestone 4 adds re-checking inside the evidence loop.
"""
from __future__ import annotations

from .clean import normalize
from .config import GatesCfg
from .models import Evidence, ExtractedFields


def detect_hosted_trust_tool(raw_html: str, url: str, cfg: GatesCfg) -> Evidence | None:
    """Code-only: a trust centre hosted by a competitor shows up as its asset URLs in the raw HTML.

    The quote is the matched HTML snippet, verified by this function (kind="raw_html").
    """
    if not raw_html:
        return None
    low = raw_html.lower()
    for tool, markers in cfg.hosted_trust_markers.items():
        for m in markers:
            i = low.find(m.lower())
            if i >= 0:
                snippet = " ".join(raw_html[max(0, i - 30): i + len(m) + 30].split())
                return Evidence(value=tool, evidence_quote=snippet, source_url=url, verified=True, kind="raw_html")
    return None


def check_hard_gates(fields: ExtractedFields, cfg: GatesCfg) -> list[str]:
    hits: list[str] = []
    d = cfg.drop_if
    if d.b2c and fields.buyer_type.known and fields.buyer_type.value == "b2c":
        hits.append("dropped:b2c")
    if d.agency_or_services and fields.is_agency_or_services.known and fields.is_agency_or_services.value is True:
        hits.append("dropped:agency_or_services")
    if d.compliance_vendor and fields.is_compliance_vendor.known and fields.is_compliance_vendor.value is True:
        hits.append("dropped:compliance_vendor")
    if d.competitor_tool and fields.compliance_tool_named.known:
        named = normalize(fields.compliance_tool_named.value or "")
        if any(normalize(t) in named for t in cfg.competitor_tools):
            hits.append("dropped:competitor_tool")
    return hits
