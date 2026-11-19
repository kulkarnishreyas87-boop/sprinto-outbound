"""LLM wrapper, drafting checks and the thin-slice pipeline, all with fakes."""
from types import SimpleNamespace

import pytest
from conftest import HOME_HTML
from pydantic import ValidationError

from sprinto.config import Claim, Claims
from sprinto.draft import DraftOutput, draft_variants
from sprinto.extract import ExtractOutput
from sprinto.llm import AnthropicProvider, LLMError, LLMResult
from sprinto.models import Evidence, ExtractedFields, Status
from sprinto.pipeline import run_slice

CLAIMS = Claims(claims=[Claim(id="c1", text="Approved proof point.", approved=True),
                        Claim(id="c2", text="Not approved.", approved=False)])


def _q(value=None, quote=None, url=None):
    return {"value": value, "evidence_quote": quote, "source_url": url}


def extract_output(url="https://acme.io/"):
    fields = {name: _q() for name in ExtractOutput.model_fields}
    fields["is_saas"] = _q(True, "Acme is a B2B SaaS platform", url)
    fields["certifications_named"] = _q(["SOC 2 Type II", "HIPAA"], "working towards SOC 2 Type II", url)
    fields["compliance_tool_named"] = _q("Vanta", "Powered by Vanta trust center", url)  # not on page
    fields["enterprise_tier_present"] = _q(True, "Our Enterprise plan includes SSO, SAML, audit logs", url)
    return ExtractOutput.model_validate(fields)


class FakeLLM:
    def __init__(self, draft_hook="certifications_named", draft_claim="c1", unsupported=(),
                 moment="first_certification", tier="A"):
        self.calls = []
        self.draft_hook, self.draft_claim = draft_hook, draft_claim
        self.unsupported = set(unsupported)
        self.moment, self.tier = moment, tier

    def complete_json(self, stage, system, user, schema, record_id=None):
        import json

        from sprinto.extract import Judgment, SupportOutput
        from sprinto.qualify import QualifyOutput
        self.calls.append((stage, user))
        if stage == "qualify":
            return LLMResult(QualifyOutput(moment=self.moment, tier=self.tier, rationale="model says so",
                                           signals_used=["certifications_named", "made_up_field"]),
                             "fake", 0, 0, 0.0)
        if stage == "extract":
            return LLMResult(extract_output(), "fake", 100, 50, 0.001)
        if stage == "support":
            facts = json.loads(user.split("\n", 1)[1])
            return LLMResult(SupportOutput(judgments=[
                Judgment(field=f["field"], supported=f["field"] not in self.unsupported, reason="r")
                for f in facts]), "fake", 0, 0, 0.0)
        from fakes import judge_result, pains_result, variants_result
        if stage == "pains":
            return pains_result()
        if stage == "draft":
            r = variants_result(user, hook=self.draft_hook, claim=self.draft_claim)
            return LLMResult(r.parsed, "fake", 200, 100, 0.002)
        if stage == "judge":
            return judge_result(user)
        raise AssertionError(stage)


ROUTES = {"https://acme.io/robots.txt": (200, "User-agent: *\nAllow: /"), "https://acme.io/": (200, HOME_HTML)}


def test_thin_slice_drafts_with_verified_hook(make_fetcher, store, cfg, tmp_path):
    llm = FakeLLM()
    rec = run_slice("https://www.acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES),
                    llm=llm, run_id="r1", contact_name="Jane Doe", contact_title="CTO", runs_dir=tmp_path)
    assert rec.status == Status.drafted, rec.failures
    assert rec.domain == "acme.io"
    assert rec.fields.is_saas.known
    assert rec.fields.certifications_named.value == ["SOC 2 Type II"]       # HIPAA dropped
    assert not rec.fields.compliance_tool_named.known                        # invented quote rejected
    rej = next(r for r in rec.rejections if r.field == "compliance_tool_named")
    assert rej.stage == "s2" and rej.reason == "quote_not_found"
    assert rej.proposed_value == "Vanta" and rej.evidence_quote == "Powered by Vanta trust center"
    assert rec.failures == []   # rejected facts are not stage failures
    assert len(rec.drafts) == 3 and rec.drafts[0].hook_field == "certifications_named"
    assert all(r.passed for r in rec.qa_results) and len(rec.pains) == 2
    assert rec.chosen_draft == 3 and rec.judge.scores == {"1": 3, "2": 3, "3": 4}
    assert rec.cost.tokens_in == 100 + 200 + 1 + 1 and rec.cost.usd_estimate == pytest.approx(0.003)
    assert {"s1_scrape", "s2_extract", "s7_pains", "s8_draft", "s9_qa"} <= set(rec.timings)
    assert (tmp_path / "r1" / "acme.io.json").exists()
    assert store.get_record(rec.record_id).status == Status.drafted
    # draft prompt received only approved claims and only verified facts
    assert [c[0] for c in llm.calls] == ["extract", "support", "qualify", "pains", "draft", "judge"]
    draft_user = llm.calls[4][1]
    assert rec.moment == "first_certification" and rec.tier == "A"
    assert rec.signals_used == ["certifications_named"]          # made-up field ignored by code
    assert "certification_in_progress" in rec.tier_rationale
    assert "Not approved." not in draft_user and "Vanta" not in draft_user


def test_unsupported_certification_removed_before_draft(make_fetcher, store, cfg, tmp_path):
    rec = run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES),
                    llm=FakeLLM(draft_hook="is_saas", unsupported={"certifications_named"}), run_id="r1",
                    contact_name="Jane", contact_title=None, runs_dir=tmp_path)
    assert rec.status == Status.drafted
    assert not rec.fields.certifications_named.known and rec.fields.is_saas.known
    rej = next(r for r in rec.rejections if r.stage == "s2b")
    assert rej.field == "certifications_named" and rej.reason.startswith("quote_does_not_support_value")
    assert rej.proposed_value == ["SOC 2 Type II"]


def test_hook_on_field_removed_by_support_check_is_rejected(make_fetcher, store, cfg, tmp_path):
    rec = run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES),
                    llm=FakeLLM(unsupported={"certifications_named"}), run_id="r1",
                    contact_name="Jane", contact_title=None, runs_dir=tmp_path)
    assert (rec.status, rec.reason_code) == (Status.needs_review, "qa_failed")
    assert all("hook: 'certifications_named' is not a verified fact" in r.failures[0] for r in rec.qa_results)


def test_support_check_fails_closed_on_missing_judgment():
    from sprinto.extract import SupportOutput, check_support

    class Silent:
        def complete_json(self, *a, **k):
            return LLMResult(SupportOutput(judgments=[]), "fake", 0, 0, 0.0)

    fields = ExtractedFields(is_saas=Evidence(value=True, evidence_quote="q q q", source_url="u", verified=True))
    out, rejected, _ = check_support(Silent(), fields)
    assert not out.is_saas.known and rejected == {"is_saas": "support_not_judged"}


def test_support_check_cannot_upgrade_unknown_field():
    from sprinto.extract import Judgment, SupportOutput, check_support

    class Eager:
        def complete_json(self, *a, **k):
            return LLMResult(SupportOutput(judgments=[Judgment(field="is_compliance_vendor", supported=True,
                                                               reason="x")]), "fake", 0, 0, 0.0)

    out, _, res = check_support(Eager(), ExtractedFields())
    assert res is None and not out.is_compliance_vendor.known   # no known facts -> no call, nothing invented


def test_blocked_site_fails_with_reason_and_no_llm_call(make_fetcher, store, cfg, tmp_path):
    llm = FakeLLM()
    routes = {"https://acme.io/robots.txt": (200, ""), "https://acme.io/": (403, "denied")}
    rec = run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(routes), llm=llm,
                    run_id="r1", contact_name="Jane", contact_title=None, runs_dir=tmp_path)
    assert rec.status == Status.failed and rec.reason_code == "blocked" and llm.calls == []


def test_hook_on_unverified_field_rejected(make_fetcher, store, cfg, tmp_path):
    rec = run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES),
                    llm=FakeLLM(draft_hook="compliance_tool_named"), run_id="r1",
                    contact_name="Jane", contact_title=None, runs_dir=tmp_path)
    # 3 variants + 3 regenerated, all fail -> review queue, never a draft
    assert (rec.status, rec.reason_code) == (Status.needs_review, "qa_failed")
    assert len(rec.drafts) == 6 and [d.variant for d in rec.drafts] == [1, 2, 3, 4, 5, 6]
    assert rec.chosen_draft is None


def test_regeneration_gets_qa_feedback(make_fetcher, store, cfg, tmp_path):
    llm = FakeLLM(draft_claim="c2")
    rec = run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES),
                    llm=llm, run_id="r1", contact_name="Jane", contact_title=None, runs_dir=tmp_path)
    assert rec.reason_code == "qa_failed"
    drafts = [u for s, u in llm.calls if s == "draft"]
    assert len(drafts) == 2 and "Previous drafts failed QA" in drafts[1] and "'c2' is not an approved claim" in drafts[1]


def test_no_approved_claims_refuses_to_draft():
    fields = ExtractedFields(is_saas=Evidence(value=True, evidence_quote="q q q", source_url="u", verified=True))
    with pytest.raises(LLMError) as e:
        draft_variants(FakeLLM(), domain="acme.io", fields=fields, claims=Claims(claims=[]),
                       contact_name="Jane", contact_title=None, moment="first_certification", pains=[])
    assert e.value.reason_code == "no_approved_claims"


def test_unexpected_error_is_recorded_not_raised(make_fetcher, store, cfg, tmp_path):
    class Boom(FakeLLM):
        def complete_json(self, *a, **k):
            raise KeyError("boom")
    rec = run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES), llm=Boom(),
                    run_id="r1", contact_name="Jane", contact_title=None, runs_dir=tmp_path)
    assert rec.status == Status.failed and rec.reason_code == "unexpected_error"


# ---------- AnthropicProvider with a fake SDK client ----------

def _resp(parsed, stop="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.kwargs = []
        self.messages = self

    def parse(self, **kwargs):
        self.kwargs.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _invalid():
    try:
        DraftOutput.model_validate({})
    except ValidationError as e:
        return e


def test_provider_uses_config_model_and_effort(cfg, store):
    ok = DraftOutput(subject="s", body="b", hook_field="h", claim_id="c")
    client = FakeClient([_resp(ok)])
    res = AnthropicProvider(cfg, store, "", client=client).complete_json("extract", "sys", "user", DraftOutput)
    assert res.parsed == ok
    assert client.kwargs[0]["model"] == cfg.models.extract.model
    assert client.kwargs[0]["output_config"] == {"effort": cfg.models.extract.effort}
    p = cfg.models.prices[cfg.models.extract.model]
    assert res.usd == pytest.approx((1000 * p.input + 200 * p.output) / 1e6)


def test_provider_retries_once_then_fails(cfg, store):
    client = FakeClient([_invalid(), _invalid(), _resp(None)])
    with pytest.raises(LLMError) as e:
        AnthropicProvider(cfg, store, "", client=client).complete_json("extract", "s", "u", DraftOutput)
    assert e.value.reason_code == "llm_invalid_output" and len(client.kwargs) == 2


def test_provider_retry_succeeds(cfg, store):
    ok = DraftOutput(subject="s", body="b", hook_field="h", claim_id="c")
    client = FakeClient([_resp(None, "max_tokens"), _resp(ok)])
    res = AnthropicProvider(cfg, store, "", client=client).complete_json("draft", "s", "u", DraftOutput)
    assert res.parsed == ok and res.tokens_in == 2000


def test_provider_refusal_is_a_stage_failure(cfg, store):
    client = FakeClient([_resp(None, "refusal")])
    with pytest.raises(LLMError) as e:
        AnthropicProvider(cfg, store, "", client=client).complete_json("draft", "s", "u", DraftOutput)
    assert e.value.reason_code == "llm_refusal"


def test_provider_requires_key_without_client(cfg, store):
    with pytest.raises(LLMError) as e:
        AnthropicProvider(cfg, store, "")
    assert e.value.reason_code == "missing_api_key"
