"""Shared fake LLM answers for S7-S9 that pass code QA by default."""
import re

from sprinto.draft import VariantOut, VariantsOutput
from sprinto.llm import LLMResult
from sprinto.pains import PainOut, PainsOutput
from sprinto.qa import JudgeOutput, Score


def recipient_first(user: str) -> str:
    m = re.search(r"Recipient: (\S+)", user)
    return m.group(1) if m else "there"


def good_body(first: str, words: int = 90) -> str:
    filler = " ".join(["teams"] * (words - 12))
    return f"Hi {first},\n\nI noticed your growth and {filler}.\n\nWould a short call next week help?\n\nBest,\nSprinto team"


def pains_result(linked=("is_saas",)) -> LLMResult:
    return LLMResult(PainsOutput(pains=[
        PainOut(hypothesis="Security reviews are likely slowing enterprise deals.", linked_fields=list(linked)),
        PainOut(hypothesis="Engineers probably lose time to audit prep.", linked_fields=list(linked))]),
        "fake", 1, 1, 0.0)


def variants_result(user: str, hook="is_saas", claim="c1", facts=None, body=None, n=3) -> LLMResult:
    first = recipient_first(user)
    names = ["one", "two", "three", "four", "five", "six"]
    vs = [VariantOut(subject=f"idea {names[i - 1]}", body=body or good_body(first, 80 + i), hook_field=hook, pain_index=1,
                     claim_id=claim, facts_used=facts if facts is not None else [hook]) for i in range(1, n + 1)]
    return LLMResult(VariantsOutput(variants=vs), "fake", 1, 1, 0.0)


def judge_result(user: str, pick=None) -> LLMResult:
    ids = [int(x) for x in re.findall(r'"variant": (\d+)', user)]
    return LLMResult(JudgeOutput(scores=[Score(variant=i, score=3 + (i == ids[-1]), note="n") for i in ids],
                                 chosen_variant=pick if pick is not None else ids[-1], rationale="best hook"),
                     "fake", 1, 1, 0.0)
