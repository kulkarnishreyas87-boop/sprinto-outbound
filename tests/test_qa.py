import pytest
from fakes import good_body

from sprinto.config import Claim, Claims
from sprinto.llm import LLMError, LLMResult
from sprinto.models import Contact, Draft, Evidence, ExtractedFields, Pain
from sprinto.pains import PainOut, PainsOutput, check_pains, generate_pains
from sprinto.qa import JudgeOutput, Score, judge, qa_check

CLAIMS = Claims(claims=[Claim(id="c1", text="Sprinto gets teams audit-ready for SOC 2 in weeks, with 1,200 customers.",
                              approved=True),
                        Claim(id="c2", text="Unapproved.", approved=False)])
FIELDS = ExtractedFields(
    is_saas=Evidence(value=True, evidence_quote="a B2B SaaS platform", source_url="u", verified=True),
    open_roles_total=Evidence(value=32, evidence_quote="32 open roles listed on the Ashby job board", source_url="u",
                              verified=True),
    certifications_named=Evidence(value=["SOC 2 Type II"], evidence_quote="we hold SOC 2 Type II", source_url="u",
                                  verified=True),
    enterprise_customers_named=Evidence(value=["Globex"], evidence_quote="trusted by Globex", source_url="u",
                                        verified=True))
CONTACT = Contact(name="Jane Doe", title="CTO", email="jane@acme.io",
                  alternatives=[{"name": "Mark Smith", "title": "VP Sales", "excluded": "excluded_function"}])
PAINS = [Pain(hypothesis="a likely pain", linked_fields=["is_saas"]),
         Pain(hypothesis="another probable pain", linked_fields=["is_saas"])]


def draft(body=None, subject="quick idea for acme", **kw):
    base = dict(variant=1, subject=subject, body=body or good_body("Jane"), hook_field="is_saas", claim_id="c1",
                pain_index=1, facts_used=["is_saas"])
    base.update(kw)
    return Draft(**base)


def failures(d, cfg):
    return qa_check(d, fields=FIELDS, claims=CLAIMS, contact=CONTACT, pains=PAINS, cfg=cfg).failures


def test_good_draft_passes(cfg):
    assert failures(draft(), cfg) == []


def with_line(line):
    return good_body("Jane").replace("Would a short call", line + " Would a short call")


@pytest.mark.parametrize("kw,expect", [
    ({"body": good_body("Jane", 40)}, "length"),
    ({"body": good_body("Jane", 160)}, "length"),
    ({"body": good_body("Jane").replace("?", ".")}, "question_marks"),
    ({"body": with_line("Any thoughts?")}, "question_marks"),
    ({"subject": "two questions?"}, "question_marks"),
    ({"body": good_body("John")}, "greeting"),
    ({"body": with_line("Mark mentioned you.")}, "wrong_name"),
    ({"hook_field": "funding_stage_stated"}, "hook"),
    ({"facts_used": ["is_saas", "regions_served"]}, "facts"),
    ({"pain_index": 3}, "pain"),
    ({"claim_id": "c2"}, "claim"),
    ({"body": with_line("Your 40 open roles stood out.")}, "numbers: 40"),
    ({"body": with_line("Probably 3x faster.")}, "numbers: 3"),
    ({"body": with_line("Adding ISO 27001 next is common.")}, "unsupported_framework: ISO 27001"),
    ({"body": with_line("Unlike Vanta, we help.")}, "competitor: mentions Vanta"),
    ({"body": with_line("I hope this finds you well.")}, "banned_phrase"),
    ({"subject": "Re: compliance"}, "subject: starts with Re:"),
    ({"subject": "a very long subject line that keeps going on"}, "subject: 9 words"),
])
def test_each_rule_catches_its_failure(cfg, kw, expect):
    f = failures(draft(**kw), cfg)
    assert any(expect in x for x in f), f


@pytest.mark.parametrize("line", ["I'd guess his team is busy.", "She probably owns audits.",
                                  "That lands on her desk."])
def test_gendered_pronouns_are_rejected(cfg, line):
    assert any(x.startswith("pronoun:") for x in failures(draft(body=with_line(line)), cfg))


def test_pronoun_check_has_no_false_positives(cfg):
    line = "Whether the team there sees their hiring as help, these teams share it."
    assert failures(draft(body=with_line(line)), cfg) == []


def test_pains_reject_gendered_pronouns():
    _, problems = check_pains(PainsOutput(pains=[
        PainOut(hypothesis="The CTO is likely seeing his team stretched.", linked_fields=["is_saas"]),
        PainOut(hypothesis="Their engineers probably lose time to audits.", linked_fields=["is_saas"])]), {"is_saas"})
    assert problems == ["pain 1 uses a gendered pronoun; use the role or their name"]


@pytest.mark.parametrize("line", [
    "You have 32 open roles.",                       # number from evidence
    "Teams reach SOC 2 in weeks, with 1,200 customers.",   # numbers + framework from the approved claim
    "Your SOC 2 Type II is a strong base.",          # framework from evidence
    "Globex trusts you already.",                    # customer from evidence
])
def test_allowed_specifics_pass(cfg, line):
    assert failures(draft(body=with_line(line), facts_used=["is_saas", "open_roles_total"]), cfg) == []


def test_framework_from_other_claim_not_allowed(cfg):
    claims = Claims(claims=[Claim(id="c1", text="Plain proof point.", approved=True),
                            Claim(id="c3", text="Also covers HIPAA.", approved=True)])
    f = qa_check(draft(body=with_line("HIPAA is next.")), fields=FIELDS, claims=claims, contact=CONTACT,
                 pains=PAINS, cfg=cfg).failures
    assert any("unsupported_framework: HIPAA" in x for x in f)


# ---------- judge ----------

class JudgeLLM:
    def __init__(self, pick, scores):
        self.pick, self.scores, self.calls = pick, scores, 0

    def complete_json(self, *a, **k):
        self.calls += 1
        return LLMResult(JudgeOutput(scores=[Score(variant=v, score=s, note="") for v, s in self.scores.items()],
                                     chosen_variant=self.pick, rationale="because"), "f", 1, 1, 0.0)


def test_judge_skipped_for_single_survivor():
    llm = JudgeLLM(9, {})
    j, res = judge(llm, [draft(variant=2)], fields=FIELDS, contact=CONTACT, pains=PAINS)
    assert j.chosen_variant == 2 and "only one" in j.skipped and res is None and llm.calls == 0


def test_judge_pick_used():
    j, _ = judge(JudgeLLM(3, {1: 2, 3: 5}), [draft(variant=1), draft(variant=3)], fields=FIELDS, contact=CONTACT,
                 pains=PAINS)
    assert j.chosen_variant == 3 and j.scores == {"1": 2, "3": 5}


def test_judge_cannot_pick_a_failed_variant():
    j, _ = judge(JudgeLLM(2, {1: 4, 2: 5, 3: 3}), [draft(variant=1), draft(variant=3)], fields=FIELDS,
                 contact=CONTACT, pains=PAINS)
    assert j.chosen_variant == 1 and "invalid" in j.rationale and "2" not in j.scores


# ---------- pains ----------

def test_check_pains_rules():
    verified = {"is_saas"}
    ok, problems = check_pains(PainsOutput(pains=[
        PainOut(hypothesis="Reviews likely slow deals.", linked_fields=["is_saas", "made_up"]),
        PainOut(hypothesis="I'd guess audits pull engineers away.", linked_fields=["is_saas"])]), verified)
    assert problems == [] and ok[0].linked_fields == ["is_saas"]
    _, problems = check_pains(PainsOutput(pains=[
        PainOut(hypothesis="Reviews slow deals.", linked_fields=["is_saas"]),
        PainOut(hypothesis="Audits probably hurt.", linked_fields=["nope"]),
        PainOut(hypothesis="Third likely pain.", linked_fields=["is_saas"])]), verified)
    assert any("exactly 2" in p for p in problems)
    assert any("pain 1 is stated as fact" in p for p in problems)
    assert any("pain 2 is not linked" in p for p in problems)


class PainsLLM:
    def __init__(self, outputs):
        self.outputs, self.users = list(outputs), []

    def complete_json(self, stage, system, user, schema, record_id=None):
        self.users.append(user)
        return LLMResult(self.outputs.pop(0), "f", 1, 1, 0.0)


BAD = PainsOutput(pains=[PainOut(hypothesis="Deals are slow.", linked_fields=["is_saas"])])
GOOD = PainsOutput(pains=[PainOut(hypothesis="Deals likely stall.", linked_fields=["is_saas"]),
                          PainOut(hypothesis="Audits probably hurt.", linked_fields=["is_saas"])])


def test_pains_regenerate_once_with_feedback():
    llm = PainsLLM([BAD, GOOD])
    pains, results = generate_pains(llm, domain="a.io", fields=FIELDS, moment="expansion", contact_name="Jane",
                                    contact_title="CTO")
    assert len(pains) == 2 and len(results) == 2 and "previous answer was rejected" in llm.users[1]


def test_pains_fail_after_second_bad_answer():
    with pytest.raises(LLMError) as e:
        generate_pains(PainsLLM([BAD, BAD]), domain="a.io", fields=FIELDS, moment="expansion",
                       contact_name="Jane", contact_title="CTO")
    assert e.value.reason_code == "pains_invalid"


def test_examples_must_be_approved_and_notes_stripped(tmp_path):
    from sprinto.draft import approved_examples
    (tmp_path / "a.md").write_text("approved: true\n# note for the growth lead\nSubject: hi\n\nHi Sam,\nBody", "utf-8")
    (tmp_path / "b.md").write_text("approved: false\nSubject: no\n", "utf-8")
    assert approved_examples(tmp_path) == ["Subject: hi\n\nHi Sam,\nBody"]


def test_repo_examples_declare_approval_on_first_line():
    from sprinto.draft import EXAMPLES_DIR
    files = list(EXAMPLES_DIR.glob("*.md"))
    assert len(files) >= 3
    for f in files:
        first = f.read_text(encoding="utf-8").splitlines()[0].strip().lower()
        assert first in ("approved: true", "approved: false"), f.name
