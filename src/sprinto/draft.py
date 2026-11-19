"""S8 Draft: 2-3 variants in one call, built only from verified facts, the chosen pains and
approved claims. Each variant declares what it relies on so S9 code QA can check it.
"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template

from pydantic import BaseModel

from .config import ROOT, Claims
from .llm import LLMError, LLMProvider, LLMResult
from .models import Draft, ExtractedFields, Pain

PROMPT = ROOT / "prompts" / "draft.md"
EXAMPLES_DIR = ROOT / "config" / "examples"


class DraftOutput(BaseModel):
    """Single-draft shape (kept for provider tests)."""
    subject: str
    body: str
    hook_field: str
    claim_id: str


class VariantOut(BaseModel):
    subject: str
    body: str
    hook_field: str
    pain_index: int
    claim_id: str
    facts_used: list[str]


class VariantsOutput(BaseModel):
    variants: list[VariantOut]


def verified_facts(fields: ExtractedFields) -> dict[str, dict]:
    out = {}
    for name in ExtractedFields.model_fields:
        ev = getattr(fields, name)
        if ev.known:
            val = ev.value
            if isinstance(val, list):
                val = [v.model_dump() if hasattr(v, "model_dump") else v for v in val]
            out[name] = {"value": val, "quote": ev.evidence_quote, "source_url": ev.source_url}
    return out


def approved_examples(directory: Path = EXAMPLES_DIR) -> list[str]:
    """Example files must start with a line `approved: true` to be used."""
    out = []
    for f in sorted(directory.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        first, _, rest = text.partition("\n")
        if first.strip().lower() == "approved: true":
            # "#" lines are notes for the Growth Lead, never shown to the model
            out.append("\n".join(l for l in rest.splitlines() if not l.lstrip().startswith("#")).strip())
    return out


def draft_variants(llm: LLMProvider, *, domain: str, fields: ExtractedFields, claims: Claims,
                   contact_name: str, contact_title: str | None, moment: str | None, pains: list[Pain],
                   n: int = 3, feedback: str = "", start_variant: int = 1,
                   record_id: str | None = None) -> tuple[list[Draft], LLMResult]:
    approved = claims.approved()
    if not approved:
        raise LLMError("no_approved_claims", "config/claims.yaml has no approved claims")
    facts = verified_facts(fields)
    if not facts:
        raise LLMError("no_verified_facts", "nothing verified to build a hook on")
    examples = approved_examples()
    pains_txt = "\n".join(f"{i}. {p.hypothesis} (linked to: {', '.join(p.linked_fields)})"
                          for i, p in enumerate(pains, 1)) or "(none given: choose one hypothesis for this role)"
    user = Template(
        "Company: $domain\nRecipient: $name ($title)\nBuying moment: $moment\nWrite $n distinct variants.\n\n"
        "Verified facts (JSON):\n$facts\n\nPains (use exactly one per variant, by number):\n$pains\n\n"
        "Approved claims (JSON):\n$claims\n\nExample emails the Growth Lead approved:\n$examples$feedback"
    ).substitute(
        domain=domain, name=contact_name, title=contact_title or "title unknown", moment=moment or "unknown", n=n,
        facts=json.dumps(facts, indent=1, ensure_ascii=False), pains=pains_txt,
        claims=json.dumps([{"id": c.id, "text": c.text} for c in approved], ensure_ascii=False),
        examples="\n---\n".join(examples) if examples else "(none approved yet)",
        feedback=("\n\nPrevious drafts failed QA. Fix these problems:\n" + feedback) if feedback else "",
    )
    res = llm.complete_json("draft", PROMPT.read_text(encoding="utf-8"), user, VariantsOutput, record_id)
    drafts = [Draft(variant=start_variant + i, subject=v.subject.strip(), body=v.body.strip(),
                    hook_field=v.hook_field, claim_id=v.claim_id, pain_index=v.pain_index,
                    facts_used=list(v.facts_used))
              for i, v in enumerate(res.parsed.variants[:n])]
    if not drafts:
        raise LLMError("no_variants", "model returned no variants")
    return drafts, res
