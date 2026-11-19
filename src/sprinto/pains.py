"""S7 Pains: exactly two hypotheses for this role in this moment, each linked to verified fields.

Code checks: exactly two, each linked to >=1 verified field, each phrased as a hypothesis.
One regeneration on failure, then the stage fails with a reason code.
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel

from .config import ROOT
from .draft import verified_facts
from .llm import LLMError, LLMProvider, LLMResult
from .models import ExtractedFields, Pain

PROMPT = ROOT / "prompts" / "pains.md"
HEDGES = re.compile(r"\b(likely|probably|may|might|could|perhaps|i'?d guess|i suspect|i imagine|possibly|"
                    r"tends? to|often|wonder)\b", re.I)


class PainOut(BaseModel):
    hypothesis: str
    linked_fields: list[str]


class PainsOutput(BaseModel):
    pains: list[PainOut]


def check_pains(out: PainsOutput, verified: set[str]) -> tuple[list[Pain], list[str]]:
    problems: list[str] = []
    if len(out.pains) != 2:
        problems.append(f"expected exactly 2 pains, got {len(out.pains)}")
    pains = []
    for i, p in enumerate(out.pains[:2], 1):
        links = [f for f in p.linked_fields if f in verified]
        if not links:
            problems.append(f"pain {i} is not linked to any verified field ({p.linked_fields})")
        if re.search(r"\b(he|she|him|her|his|hers)\b", p.hypothesis, re.I):
            problems.append(f"pain {i} uses a gendered pronoun; use the role or their name")
        if not HEDGES.search(p.hypothesis):
            problems.append(f"pain {i} is stated as fact, not a hypothesis")
        pains.append(Pain(hypothesis=p.hypothesis.strip(), linked_fields=links))
    return pains, problems


def generate_pains(llm: LLMProvider, *, domain: str, fields: ExtractedFields, moment: str,
                   contact_name: str, contact_title: str | None,
                   record_id: str | None = None) -> tuple[list[Pain], list[LLMResult]]:
    facts = verified_facts(fields)
    base = (f"Company: {domain}\nBuying moment: {moment}\nPerson: {contact_name}, {contact_title or 'title unknown'}\n\n"
            f"Verified facts (JSON):\n{json.dumps(facts, indent=1, ensure_ascii=False)}")
    results, feedback = [], ""
    for attempt in range(2):
        res = llm.complete_json("pains", PROMPT.read_text(encoding="utf-8"), base + feedback, PainsOutput, record_id)
        results.append(res)
        pains, problems = check_pains(res.parsed, set(facts))
        if not problems:
            return pains, results
        feedback = "\n\nYour previous answer was rejected: " + "; ".join(problems) + ". Fix these."
    raise LLMError("pains_invalid", "; ".join(problems))
