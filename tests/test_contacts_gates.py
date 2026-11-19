import httpx
import pytest
from conftest import HOME_HTML

from sprinto.config import Claim, Claims
from sprinto.contacts import HunterClient, HunterError, choose_contact, title_has
from sprinto.gates import check_hard_gates
from sprinto.models import Evidence, ExtractedFields, Status
from sprinto.pipeline import run_slice


def person(first, last, position, conf=95, email=None):
    return {"value": email or f"{first.lower()}@acme.io", "first_name": first, "last_name": last,
            "position": position, "confidence": conf}


def resp(*emails):
    return {"data": {"emails": list(emails)}, "meta": {"results": len(emails)}}


# ---------- title matching ----------

@pytest.mark.parametrize("title,kw,expected", [
    ("Director of Sales", "cto", False),            # 'cto' inside 'director'
    ("Co-founder & CTO", "cto", True),
    ("Chief Technology Officer", "cto", True),
    ("VP of Engineering", "vp engineering", True),
    ("Head of Security", "head of security", True),
    ("Chief Information Security Officer", "ciso", True),
    ("Governance, Risk & Compliance Lead", "grc", True),
    ("Chrome Platform Engineer", "hr", False),      # 'hr' inside 'chrome'
    ("HR Business Partner", "hr", True),
])
def test_title_has(title, kw, expected):
    assert title_has(title, kw) is expected


# ---------- ranking ----------

def test_first_certification_prefers_cto_over_ceo(cfg):
    c, _ = choose_contact(resp(person("Ann", "Ceo", "CEO"), person("Bob", "Tech", "Co-founder & CTO")),
                          moment="first_certification", has_open_security_role=False, cfg=cfg.contacts)
    assert c.name == "Bob Tech" and "cto" in c.why_chosen
    assert [a["name"] for a in c.alternatives] == ["Ann Ceo"]


def test_expansion_prefers_ciso(cfg):
    c, _ = choose_contact(resp(person("Bob", "T", "CTO"), person("Cy", "S", "Chief Information Security Officer")),
                          moment="expansion", has_open_security_role=False, cfg=cfg.contacts)
    assert c.name == "Cy S"


def test_open_role_prefers_hiring_manager(cfg):
    c, _ = choose_contact(resp(person("Ann", "C", "CEO"), person("Vic", "E", "VP Engineering")),
                          moment="first_certification", has_open_security_role=True, cfg=cfg.contacts)
    assert c.name == "Vic E" and "hiring-manager-first" in c.why_chosen


def test_high_confidence_preferred_over_better_title(cfg):
    c, _ = choose_contact(resp(person("Bob", "T", "CTO", conf=60), person("Ann", "C", "CEO", conf=92)),
                          moment="first_certification", has_open_security_role=False, cfg=cfg.contacts)
    assert c.name == "Ann C"


def test_low_confidence_used_only_as_last_resort(cfg):
    c, _ = choose_contact(resp(person("Bob", "T", "CTO", conf=60)),
                          moment="first_certification", has_open_security_role=False, cfg=cfg.contacts)
    assert c.name == "Bob T" and "below preferred minimum" in c.why_chosen


def test_only_sales_and_marketing_means_no_suitable_contact(cfg):
    c, reason = choose_contact(resp(person("S", "A", "VP Sales"), person("M", "K", "Head of Marketing"),
                                    person("R", "C", "Talent Acquisition Manager")),
                               moment="first_certification", has_open_security_role=False, cfg=cfg.contacts)
    assert c is None and reason == "no_suitable_contact"


def test_manager_title_alone_is_not_enough(cfg):
    c, reason = choose_contact(resp(person("O", "M", "Office Manager"), person("P", "M", "Product Manager")),
                               moment="first_certification", has_open_security_role=False, cfg=cfg.contacts)
    assert c is None and reason == "no_suitable_contact"


def test_empty_hunter_response(cfg):
    assert choose_contact({"data": {"emails": []}}, moment="expansion", has_open_security_role=False,
                          cfg=cfg.contacts) == (None, "no_suitable_contact")


# ---------- Hunter client ----------

def hunter_client(cfg, store, handler):
    return HunterClient("test-key", store, cfg.contacts, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_hunter_called_once_then_cached(cfg, store):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=resp(person("Bob", "T", "CTO")))

    h = hunter_client(cfg, store, handler)
    r1, called1 = h.domain_search("acme.io")
    r2, called2 = h.domain_search("acme.io")
    assert (called1, called2) == (True, False) and r1 == r2 and len(seen) == 1
    req = seen[0]
    assert "api_key" not in str(req.url) and req.headers["X-API-KEY"] == "test-key"
    assert req.url.params["department"] == "executive,it,management"
    assert req.url.params["seniority"] == "senior,executive"
    assert req.url.params["limit"] == "10"


@pytest.mark.parametrize("code,reason", [(401, "hunter_auth"), (429, "hunter_quota_exhausted"),
                                         (403, "hunter_rate_limited"), (500, "hunter_http_500")])
def test_hunter_errors_have_reason_codes_and_are_not_cached(cfg, store, code, reason):
    h = hunter_client(cfg, store, lambda req: httpx.Response(code, json={"errors": [{"id": "x"}]}))
    with pytest.raises(HunterError) as e:
        h.domain_search("acme.io")
    assert e.value.reason_code == reason and store.get_hunter("acme.io") is None


def test_hunter_requires_key(cfg, store):
    with pytest.raises(HunterError) as e:
        HunterClient("", store, cfg.contacts).domain_search("acme.io")
    assert e.value.reason_code == "hunter_missing_key"


# ---------- gates ----------

def ev(value):
    return Evidence(value=value, evidence_quote="some verified quote", source_url="u", verified=True)


@pytest.mark.parametrize("fields,expected", [
    (ExtractedFields(is_compliance_vendor=ev(True)), ["dropped:compliance_vendor"]),
    (ExtractedFields(buyer_type=ev("b2c")), ["dropped:b2c"]),
    (ExtractedFields(is_agency_or_services=ev(True)), ["dropped:agency_or_services"]),
    (ExtractedFields(compliance_tool_named=ev("Powered by Drata")), ["dropped:competitor_tool"]),
    (ExtractedFields(compliance_tool_named=ev("Sprinto")), []),
    (ExtractedFields(is_compliance_vendor=ev(False), buyer_type=ev("b2b")), []),
    (ExtractedFields(), []),                                  # unknown never drops
])
def test_hard_gates(cfg, fields, expected):
    assert check_hard_gates(fields, cfg.gates) == expected


def test_unverified_value_never_drops(cfg):
    f = ExtractedFields(is_compliance_vendor=Evidence(value=True, evidence_quote="x", verified=False))
    assert check_hard_gates(f, cfg.gates) == []


# ---------- slice with Hunter ----------

ROUTES = {"https://acme.io/robots.txt": (200, "User-agent: *\nAllow: /"), "https://acme.io/": (200, HOME_HTML)}
CLAIMS = Claims(claims=[Claim(id="c1", text="Approved proof point.", approved=True)])


class FakeHunter:
    def __init__(self, response):
        self.response, self.calls = response, 0

    def domain_search(self, domain):
        self.calls += 1
        return self.response, True


def _slice(make_fetcher, store, cfg, tmp_path, hunter, llm=None):
    from test_slice import FakeLLM
    return run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES),
                     llm=llm or FakeLLM(), run_id="r1", hunter=hunter, runs_dir=tmp_path)


def test_slice_finds_contact_via_hunter(make_fetcher, store, cfg, tmp_path):
    h = FakeHunter(resp(person("Mia", "Sales", "VP Sales"), person("Bob", "Tech", "CTO", email="bob@acme.io")))
    rec = _slice(make_fetcher, store, cfg, tmp_path, h)
    assert rec.status == Status.drafted, rec.failures
    assert rec.contact.name == "Bob Tech" and rec.contact.email == "bob@acme.io"
    assert rec.moment == "first_certification" and "first_certification ranking" in rec.contact.why_chosen
    assert rec.cost.hunter_calls == 1 and "s6_contact" in rec.timings


@pytest.mark.parametrize("llm_kw,status,reason", [
    ({"tier": "C"}, Status.needs_review, "tier_c"),
    # this fixture verifies "working towards SOC 2" (a strong signal), so a model "none" is disputed
    ({"moment": "none", "tier": "C"}, Status.needs_review, "moment_disputed"),
    ({"moment": "expansion", "tier": "A"}, Status.needs_review, "tier_c"),   # SOC 2 only "in progress"
])
def test_hunter_only_for_tier_a_or_b(make_fetcher, store, cfg, tmp_path, llm_kw, status, reason):
    from test_slice import FakeLLM
    h = FakeHunter(resp(person("Bob", "T", "CTO")))
    rec = _slice(make_fetcher, store, cfg, tmp_path, h, llm=FakeLLM(**llm_kw))
    assert (rec.status, rec.reason_code) == (status, reason)
    assert h.calls == 0 and rec.cost.hunter_calls == 0 and not rec.drafts


def test_no_approved_claims_stops_before_hunter(make_fetcher, store, cfg, tmp_path):
    from test_slice import FakeLLM
    h = FakeHunter(resp(person("Bob", "T", "CTO")))
    rec = run_slice("acme.io", cfg=cfg, claims=Claims(claims=[]), store=store, fetcher=make_fetcher(ROUTES),
                    llm=FakeLLM(), run_id="r1", hunter=h, runs_dir=tmp_path)
    assert rec.reason_code == "no_approved_claims" and h.calls == 0 and rec.cost.hunter_calls == 0


def test_slice_needs_review_when_only_sales(make_fetcher, store, cfg, tmp_path):
    rec = _slice(make_fetcher, store, cfg, tmp_path, FakeHunter(resp(person("Mia", "S", "VP Sales"))))
    assert rec.status == Status.needs_review and rec.reason_code == "no_suitable_contact" and not rec.drafts


def test_compliance_vendor_dropped_before_hunter(make_fetcher, store, cfg, tmp_path):
    from sprinto.extract import ExtractOutput
    from sprinto.llm import LLMResult
    from test_slice import FakeLLM, extract_output

    class VendorLLM(FakeLLM):
        def complete_json(self, stage, system, user, schema, record_id=None):
            if stage == "extract":
                out = extract_output().model_dump()
                out["is_compliance_vendor"] = {"value": True, "evidence_quote": "Acme is a B2B SaaS platform",
                                               "source_url": "https://acme.io/"}
                return LLMResult(ExtractOutput.model_validate(out), "fake", 1, 1, 0.0)
            return super().complete_json(stage, system, user, schema, record_id)

    h = FakeHunter(resp(person("Bob", "T", "CTO")))
    rec = _slice(make_fetcher, store, cfg, tmp_path, h, llm=VendorLLM())
    assert rec.status == Status.dropped and rec.reason_code == "dropped:compliance_vendor"
    assert h.calls == 0 and not rec.drafts and rec.cost.hunter_calls == 0
