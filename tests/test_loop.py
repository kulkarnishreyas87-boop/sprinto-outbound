"""S4 evidence loop, page discovery, job boards, render fallback (all offline)."""
import json

import httpx
import pytest
from conftest import make_transport

from sprinto.config import Claim, Claims
from sprinto.extract import ExtractOutput, Judgment, SupportOutput, verify_field
from sprinto.fetch import Fetcher, is_bot_challenge
from sprinto.jobboards import Board, board_to_fields, detect_board, parse_postings
from sprinto.llm import LLMResult
from sprinto.models import Status
from sprinto.pipeline import run_slice

CLAIMS = Claims(claims=[Claim(id="c1", text="Approved proof point.", approved=True)])
ROBOTS = (200, "User-agent: *\nAllow: /\n")

HOME = """<html><body><main>
<h1>Acme Ledger helps finance teams close the books every month</h1>
<p>Acme Ledger is a B2B SaaS platform for mid-market finance teams in the US.</p>
<p>Book a demo with our team today to see how the close gets faster.</p>
<a href="/careers">Careers</a> <a href="/pricing">Pricing</a> <a href="/security">Security</a>
</main></body></html>"""
CAREERS = """<html><body><main><h1>Join the Acme Ledger team and build the future of finance</h1>
<p>We are a remote-first company hiring across engineering, product and go to market.</p>
<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeledger"></script>
</main></body></html>"""
PRICING = """<html><body><main><h1>Simple pricing for every finance team at any stage</h1>
<p>Our Enterprise plan includes SSO, SAML, audit logs and a dedicated security review.</p>
</main></body></html>"""
GH_JOBS = {"jobs": [
    {"title": "Senior Security Engineer", "updated_at": "2026-11-02T10:00:00-05:00",
     "location": {"name": "Remote"}, "absolute_url": "https://boards.greenhouse.io/acmeledger/jobs/1",
     "departments": [{"name": "Engineering"}], "content": "&lt;p&gt;Own our security program.&lt;/p&gt;"},
    {"title": "Backend Engineer", "updated_at": "2026-10-01T10:00:00Z", "location": {"name": "NYC"},
     "absolute_url": "https://boards.greenhouse.io/acmeledger/jobs/2", "departments": [],
     "content": "&lt;p&gt;Build APIs. You will help us achieve SOC 2 Type II this year.&lt;/p&gt;"},
    {"title": "Account Executive", "updated_at": "2026-09-01", "location": {"name": "NYC"},
     "absolute_url": "https://boards.greenhouse.io/acmeledger/jobs/3", "content": "Sell things."},
]}
GH_API = "https://boards-api.greenhouse.io/v1/boards/acmeledger/jobs?content=true"


def routes(**extra):
    r = {"https://acme.io/robots.txt": ROBOTS, "https://acme.io/": (200, HOME),
         "https://acme.io/careers": (200, CAREERS), "https://acme.io/pricing": (200, PRICING),
         GH_API: (200, json.dumps(GH_JOBS))}
    r.update(extra)
    return r


def q(value=None, quote=None, url=None):
    return {"value": value, "evidence_quote": quote, "source_url": url}


class PageLLM:
    """Extraction answers depend on which page is in the prompt."""

    def __init__(self, by_page=None, unsupported=()):
        self.by_page = by_page or {}
        self.calls = []
        self.unsupported = set(unsupported)

    def complete_json(self, stage, system, user, schema, record_id=None):
        from sprinto.draft import DraftOutput
        self.calls.append(stage)
        if stage == "extract":
            out = {n: q() for n in ExtractOutput.model_fields}
            for url, fields in self.by_page.items():
                if f'<page url="{url}">' in user:
                    out.update(fields)
            return LLMResult(ExtractOutput.model_validate(out), "fake", 10, 10, 0.0001)
        if stage == "support":
            facts = json.loads(user.split("\n", 1)[1])
            return LLMResult(SupportOutput(judgments=[Judgment(field=f["field"], supported=f["field"] not in
                                                               self.unsupported, reason="r") for f in facts]),
                             "fake", 1, 1, 0.0)
        from fakes import judge_result, pains_result, variants_result
        if stage == "pains":
            return pains_result()
        if stage == "draft":
            return variants_result(user)
        if stage == "judge":
            return judge_result(user)
        if stage == "qualify":
            from sprinto.qualify import QualifyOutput
            return LLMResult(QualifyOutput(moment="first_certification", tier="A", rationale="r",
                                           signals_used=[]), "f", 1, 1, 0)
        raise AssertionError(stage)


HOME_FACTS = {"https://acme.io/": {
    "is_saas": q(True, "a B2B SaaS platform for mid-market finance teams", "https://acme.io/"),
    "buyer_type": q("b2b", "a B2B SaaS platform for mid-market finance teams", "https://acme.io/")}}
PRICING_FACTS = {"https://acme.io/pricing": {
    "enterprise_tier_present": q(True, "Enterprise plan includes SSO, SAML, audit logs", "https://acme.io/pricing")}}


def fetcher(cfg, store, rts, renderer=None):
    client = httpx.Client(transport=make_transport(rts), follow_redirects=True)
    return Fetcher(cfg.scrape, store, client=client, sleep=lambda s: None, renderer=renderer)


def go(cfg, store, tmp_path, rts, llm, **kw):
    return run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=fetcher(cfg, store, rts),
                     llm=llm, run_id="r1", contact_name="Jane", runs_dir=tmp_path, **kw)


# ---------- the loop ----------

def test_loop_fetches_careers_then_job_board_then_pricing(cfg, store, tmp_path):
    llm = PageLLM({**HOME_FACTS, **PRICING_FACTS})
    rec = go(cfg, store, tmp_path, routes(), llm)
    assert rec.status == Status.drafted, (rec.failures, rec.reason_code)
    fetched = [(p.page_type, p.status) for p in rec.pages_fetched]
    # required order: certifications (trust/security), enterprise tier (pricing), roles (careers -> board)
    assert ("careers", "ok") in fetched and ("jobboard", "ok") in fetched and ("pricing", "ok") in fetched
    assert rec.loop_iterations <= cfg.loop.max_extra_fetches
    roles = rec.fields.compliance_security_roles
    assert roles.known and [r.title for r in roles.value] == ["Senior Security Engineer", "Backend Engineer"]
    assert roles.source_url == GH_API
    assert rec.fields.open_roles_total.value == 3
    assert rec.fields.enterprise_tier_present.known
    assert rec.pages_discovered["careers"] == "https://acme.io/careers"


def test_loop_checks_competitor_first_then_strongest_signal(cfg, store, tmp_path):
    rec = go(cfg, store, tmp_path, routes(), PageLLM({**HOME_FACTS, **PRICING_FACTS}))
    order = [p.page_type for p in rec.pages_fetched[1:]]
    # disqualifier check (trust/security page) first, then open security roles
    assert order[:3] == ["security", "careers", "jobboard"]


VANTA_TRUST = ('<html><head><script data-endpoint-url="https://static.vanta.com/api/version/1"></script></head>'
               '<body><div id="root">Trust Center</div></body></html>')


def test_vanta_trust_subdomain_linked_from_security_page_is_dropped(cfg, store, tmp_path):
    sec = ("<html><body><main><p>We take security seriously and run regular independent audits of our controls.</p>"
           '<a href="https://trust.acme.io/">Visit our Trust Center</a></main></body></html>')
    rts = routes(**{"https://acme.io/security": (200, sec), "https://trust.acme.io/robots.txt": (404, ""),
                    "https://trust.acme.io/": (200, VANTA_TRUST)})
    llm = PageLLM(HOME_FACTS)
    rec = go(cfg, store, tmp_path, rts, llm)
    assert (rec.status, rec.reason_code) == (Status.dropped, "dropped:competitor_tool")
    assert [p.page_type for p in rec.pages_fetched[1:]] == ["security", "trust"]
    tool = rec.fields.compliance_tool_named
    assert tool.value == "Vanta" and tool.kind == "raw_html" and "static.vanta.com" in tool.evidence_quote
    assert tool.source_url == "https://trust.acme.io/"
    assert rec.pages_discovered["trust"] == "https://trust.acme.io/"
    assert "qualify" not in llm.calls and not rec.drafts          # dropped before S5, no Hunter


def test_cached_empty_trust_page_still_reaches_detector_offline(cfg, store, tmp_path):
    """Gold runs are offline: an empty-text page from the cache must not fall through to the network."""
    sec = ("<html><body><main><p>We take security seriously and run regular independent audits of our controls.</p>"
           '<a href="https://trust.acme.io/">Visit our Trust Center</a></main></body></html>')
    for url, page_type, raw in [("https://acme.io/", "home", HOME), ("https://acme.io/security", "security", sec),
                                ("https://trust.acme.io/", "trust", VANTA_TRUST)]:
        store.put_page(url, "acme.io", page_type, "httpx", 200, raw, "")
    offline = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404)))
    f = Fetcher(cfg.scrape, store, client=offline, sleep=lambda s: None, renderer=None)
    rec = run_slice("acme.io", cfg=cfg, claims=CLAIMS, store=store, fetcher=f, llm=PageLLM(HOME_FACTS),
                    run_id="r1", contact_name="Jane", runs_dir=tmp_path)
    assert rec.reason_code == "dropped:competitor_tool"
    trust = next(p for p in rec.pages_fetched if p.page_type == "trust")
    assert (trust.method, trust.status) == ("cache", "empty_after_render")


def test_vanta_marker_on_homepage_drops_immediately(cfg, store, tmp_path):
    home = HOME.replace("</main>", '<script src="https://assets.vanta.com/static/badge.js"></script></main>')
    rec = go(cfg, store, tmp_path, routes(**{"https://acme.io/": (200, home)}), PageLLM(HOME_FACTS))
    assert rec.reason_code == "dropped:competitor_tool" and rec.loop_iterations == 0


def test_safebase_drata_link_is_not_a_competitor_hit(cfg):
    from sprinto.gates import detect_hosted_trust_tool
    html = '<a href="https://drata.com/trust-center">SafeBase</a><script>NEXT_PUBLIC_AUTH0_DRATA_US</script>'
    assert detect_hosted_trust_tool(html, "u", cfg.gates) is None


def test_markers_must_name_a_listed_competitor(cfg):
    from pydantic import ValidationError
    from sprinto.config import GatesCfg
    with pytest.raises(ValidationError):
        GatesCfg(competitor_tools=["Vanta"], hosted_trust_markers={"SafeBase": ["safebase.io"]},
                 drop_if=cfg.gates.drop_if)


def test_job_board_evidence_is_marked_api(cfg, store, tmp_path):
    rec = go(cfg, store, tmp_path, routes(), PageLLM({**HOME_FACTS, **PRICING_FACTS}))
    assert rec.fields.compliance_security_roles.kind == "api" and rec.fields.is_saas.kind == "text"


def test_cap_reached_goes_to_review_with_unresolved_fields(cfg, store, tmp_path):
    cfg = cfg.model_copy(update={"loop": cfg.loop.model_copy(update={"max_extra_fetches": 1})})
    rec = go(cfg, store, tmp_path, routes(), PageLLM(HOME_FACTS))
    assert rec.status == Status.needs_review and rec.reason_code == "insufficient_evidence"
    assert rec.loop_iterations == 1
    f = next(x for x in rec.failures if x.reason_code == "insufficient_evidence")
    assert "unresolved:" in f.detail
    assert not rec.drafts


def test_cap_zero_means_homepage_only(cfg, store, tmp_path):
    cfg = cfg.model_copy(update={"loop": cfg.loop.model_copy(update={"max_extra_fetches": 0})})
    rec = go(cfg, store, tmp_path, routes(), PageLLM(HOME_FACTS))
    assert rec.reason_code == "insufficient_evidence" and len(rec.pages_fetched) == 1


def test_fields_resolved_when_pages_do_not_exist(cfg, store, tmp_path):
    """No trust/security page and no SOC 2 anywhere: certification stays unknown, not a review."""
    home_no_links = HOME.replace('<a href="/careers">Careers</a> <a href="/pricing">Pricing</a> '
                                 '<a href="/security">Security</a>', "")
    rec = go(cfg, store, tmp_path, routes(**{"https://acme.io/": (200, home_no_links)}), PageLLM(HOME_FACTS))
    assert rec.loop_iterations == 0 and not rec.fields.certifications_named.known
    assert rec.reason_code != "insufficient_evidence"
    # nothing left to look at, but no fit signals or stage proxy either -> tier C review, not a draft
    assert (rec.status, rec.reason_code, rec.tier) == (Status.needs_review, "tier_c", "C")


def test_gate_rechecked_after_loop_page(cfg, store, tmp_path):
    trust = {"https://acme.io/security": {"compliance_tool_named": q(
        "Vanta", "Our trust center is powered by Vanta", "https://acme.io/security")}}
    sec = "<html><body><main><p>Our trust center is powered by Vanta for continuous monitoring.</p></main></body></html>"
    rec = go(cfg, store, tmp_path, routes(**{"https://acme.io/security": (200, sec)}), PageLLM({**HOME_FACTS, **trust}))
    assert rec.status == Status.dropped and rec.reason_code == "dropped:competitor_tool"
    assert rec.pages_fetched[-1].page_type == "security" and not rec.drafts


def test_robots_disallowed_page_not_counted(cfg, store, tmp_path):
    rts = routes(**{"https://acme.io/robots.txt": (200, "User-agent: *\nDisallow: /security\n")})
    rec = go(cfg, store, tmp_path, rts, PageLLM({**HOME_FACTS, **PRICING_FACTS}))
    sec = next(p for p in rec.pages_fetched if p.page_type == "security")
    assert sec.status == "robots_disallowed"
    counted = sum(1 for p in rec.pages_fetched[1:] if p.status != "robots_disallowed")
    assert rec.loop_iterations == counted


def test_failed_loop_page_continues(cfg, store, tmp_path):
    rec = go(cfg, store, tmp_path, routes(**{"https://acme.io/pricing": (500, "")}), PageLLM(HOME_FACTS))
    assert any(p.page_type == "pricing" and p.status == "error" for p in rec.pages_fetched)
    assert rec.status in (Status.drafted, Status.needs_review)


def test_loop_rejections_are_recorded_with_stage(cfg, store, tmp_path):
    bad = {"https://acme.io/pricing": {"enterprise_tier_present": q(True, "we have a great enterprise plan",
                                                                     "https://acme.io/pricing")}}
    rec = go(cfg, store, tmp_path, routes(), PageLLM({**HOME_FACTS, **bad}))
    assert any(r.stage == "s4" and r.field == "enterprise_tier_present" and r.reason == "quote_not_found"
               for r in rec.rejections)


# ---------- job boards ----------

@pytest.mark.parametrize("html,kind,token", [
    ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>', "greenhouse", "acme"),
    ('<a href="https://job-boards.greenhouse.io/acme-co">', "greenhouse", "acme-co"),
    ('<a href="https://jobs.lever.co/acme/">', "lever", "acme"),
    ('<a href="https://jobs.eu.lever.co/acme">', "lever", "acme"),
    ('<a href="https://jobs.ashbyhq.com/Acme">', "ashby", "Acme"),
    ('<a href="https://apply.workable.com/acme/">', "workable", "acme"),
])
def test_detect_board(html, kind, token):
    b = detect_board(html)
    assert (b.kind, b.token) == (kind, token)


def test_detect_board_none_and_eu():
    assert detect_board("<a href='/careers'>Careers</a>") is None
    assert detect_board('<a href="https://jobs.eu.lever.co/acme">').api_url.startswith("https://api.eu.lever.co/")


def test_board_fields_quotes_verify_against_board_text(cfg):
    b = Board("greenhouse", "acmeledger")
    postings = parse_postings(b, GH_JOBS)
    assert postings[0].posted == "2026-11-02" and postings[0].department == "Engineering"
    assert "Own our security program." in postings[0].description          # entities decoded
    text, fields = board_to_fields(b, postings, cfg.jobboards)
    pages = {b.api_url: text}
    from sprinto.extract import QInt, QRoles, RoleOut
    roles = fields.compliance_security_roles
    assert verify_field("compliance_security_roles",
                        QRoles(value=[RoleOut(title=r.title, posted_date=r.posted_date, url=r.url) for r in roles.value],
                               evidence_quote=roles.evidence_quote, source_url=b.api_url), pages)[0].known
    assert verify_field("open_roles_total", QInt(value=3, evidence_quote=fields.open_roles_total.evidence_quote,
                                                 source_url=b.api_url), pages)[0].known
    assert 'description mentions: "' in text and "SOC 2 Type II" in text
    assert "Account Executive" in text and len(roles.value) == 2


def test_parse_lever_ashby_workable():
    lever = parse_postings(Board("lever", "x"), [{"text": "GRC Analyst", "categories": {"team": "Security",
                           "location": "London"}, "createdAt": 1762000000000, "hostedUrl": "https://jobs.lever.co/x/1",
                           "descriptionPlain": "Own ISO 27001."}])
    assert lever[0].title == "GRC Analyst" and lever[0].posted and lever[0].department == "Security"
    ashby = parse_postings(Board("ashby", "x"), {"jobs": [
        {"title": "Compliance Lead", "publishedAt": "2026-11-01T00:00:00Z", "jobUrl": "u", "isListed": True},
        {"title": "Hidden", "isListed": False}]})
    assert [p.title for p in ashby] == ["Compliance Lead"] and ashby[0].posted == "2026-11-01"
    wk = parse_postings(Board("workable", "x"), {"jobs": [{"title": "Security Engineer", "city": "Pune",
                        "country": "India", "published_on": "2026-10-20", "url": "u",
                        "description": "<p>Lead our SOC 2 audit.</p>"}]})
    assert wk[0].location == "Pune, India" and wk[0].description == "Lead our SOC 2 audit."


def test_title_matched_roles_rank_before_description_mentions(cfg):
    b = Board("greenhouse", "v")
    jobs = {"jobs": [
        {"title": "Member of Technical Staff, Financial Data", "updated_at": "2026-10-22", "absolute_url": "u1",
         "content": "You will support our SOC 2 audits."},
        {"title": "GRC Analyst", "updated_at": "2026-10-30", "absolute_url": "u2", "content": "Own GRC."}]}
    _, fields = board_to_fields(b, parse_postings(b, jobs), cfg.jobboards)
    assert [r.title for r in fields.compliance_security_roles.value] == [
        "GRC Analyst", "Member of Technical Staff, Financial Data"]
    assert fields.compliance_security_roles.evidence_quote.startswith("Open role: GRC Analyst")


def test_board_with_no_security_roles_sets_only_total(cfg):
    b = Board("ashby", "x")
    text, fields = board_to_fields(b, parse_postings(b, {"jobs": [{"title": "Designer", "jobUrl": "u"}]}),
                                   cfg.jobboards)
    assert fields.open_roles_total.value == 1 and not fields.compliance_security_roles.known


def test_job_board_api_respects_robots(cfg, store, tmp_path):
    rts = routes(**{"https://boards-api.greenhouse.io/robots.txt": (200, "User-agent: *\nDisallow: /\n")})
    rec = go(cfg, store, tmp_path, rts, PageLLM(HOME_FACTS))
    jb = next(p for p in rec.pages_fetched if p.page_type == "jobboard")
    assert jb.status == "robots_disallowed" and not rec.fields.compliance_security_roles.known


# ---------- fetch: render fallback, bot challenges, discovery ----------

THIN = "<html><body><div id='root'></div><p>Loading the app for you now, please wait a moment.</p></body></html>"
RENDERED = "<html><body><main>" + "".join(
    f"<p>Paragraph {i} explains how Acme Ledger helps finance teams close their books faster.</p>"
    for i in range(30)) + "</main></body></html>"


def test_thin_page_uses_renderer(cfg, store):
    calls = []
    f = fetcher(cfg, store, {"https://acme.io/": (200, THIN)}, renderer=lambda u, ua, t: calls.append(u) or RENDERED)
    r = f.homepage("acme.io")
    assert calls == ["https://acme.io/"] and r.page.method == "playwright" and r.page.words > 200


def test_thin_page_without_renderer_is_noted(cfg, store):
    r = fetcher(cfg, store, {"https://acme.io/": (200, THIN)}).homepage("acme.io")
    assert r.page.method == "httpx" and "render fallback unavailable" in r.page.note


def test_bot_challenge_is_blocked_and_never_rendered(cfg, store):
    calls = []
    challenge = "<html><head><title>Just a moment...</title></head><body>cf_chl_opt</body></html>"
    f = fetcher(cfg, store, {"https://acme.io/": (200, challenge)}, renderer=lambda *a: calls.append(a) or RENDERED)
    r = f.homepage("acme.io")
    assert r.page.status == "blocked" and calls == []


def test_recaptcha_on_a_real_page_is_not_a_challenge(cfg, store):
    page = RENDERED.replace("<main>", "<main><div class='g-recaptcha'></div>")
    r = fetcher(cfg, store, {"https://acme.io/": (200, page)}).homepage("acme.io")
    assert r.page.status == "ok"
    assert not is_bot_challenge(page)


def test_discovery_prefers_shortest_and_uses_sitemap(cfg, store):
    home = ('<a href="/careers/senior-engineer-123">x</a><a href="/careers">y</a>'
            '<a href="https://careers.acme.io/jobs">z</a><a href="https://other.com/pricing">o</a>')
    sitemap = ("<urlset><url><loc>https://acme.io/trust</loc></url>"
               "<url><loc>https://acme.io/customers/case-study-1</loc></url></urlset>")
    f = fetcher(cfg, store, {"https://acme.io/sitemap.xml": (200, sitemap)})
    found = f.discover_pages("acme.io", home, "https://acme.io/")
    assert found["careers"] == "https://acme.io/careers"
    assert found["trust"] == "https://acme.io/trust" and "customers" in found
    assert "pricing" not in found                                          # other domain ignored


@pytest.mark.parametrize("url,page_type", [
    ("https://trust.acme.io/", "trust"),
    ("https://security.acme.io", "security"),
    ("https://careers.acme.io/", "careers"),
    ("https://jobs.acme.io/", "careers"),
])
def test_subdomain_label_names_the_page_type(cfg, store, url, page_type):
    found = fetcher(cfg, store, {}).discover_links("https://acme.io/", f'<a href="{url}">x</a>')
    assert found == {page_type: url}


@pytest.mark.parametrize("href,expected", [
    # seen live 2026-11-19: these were wrongly classified as trust pages
    ("/docs/data-pipelines/sources/trustpilot", {}),
    ("/docs/errors/trusted_sources_environment_mismatch", {}),
    ("/help/security", {}),
    ("/trust-center", {"trust": "https://acme.io/trust-center"}),
    ("/company/security/", {"security": "https://acme.io/company/security/"}),
    ("/securityweek-recap", {}),
])
def test_keywords_match_whole_path_segments(cfg, store, href, expected):
    found = fetcher(cfg, store, {}).discover_links("https://acme.io/", f'<a href="{href}">x</a>')
    assert {k: v for k, v in found.items() if k in ("trust", "security")} == expected


@pytest.mark.parametrize("url", ["https://trustpilot.com/review/acme.io", "https://trusted.acme.io/",
                                 "https://blog.acme.io/"])
def test_subdomain_label_must_match_exactly(cfg, store, url):
    assert fetcher(cfg, store, {}).discover_links("https://acme.io/", f'<a href="{url}">x</a>') == {}


def test_report_labels_raw_html_evidence():
    from sprinto.models import DecisionRecord, Evidence, ExtractedFields
    from sprinto.report import render_record
    r = DecisionRecord(record_id="x", run_id="r", config_version=1, domain="acme.io", status=Status.dropped,
                       reason_code="dropped:competitor_tool", gates_hit=["dropped:competitor_tool"],
                       fields=ExtractedFields(compliance_tool_named=Evidence(
                           value="Vanta", evidence_quote="src=static.vanta.com/api", source_url="https://trust.acme.io/",
                           verified=True, kind="raw_html")))
    assert "[found in page HTML, not visible text]" in render_record(r)


def test_sitemap_index_reads_only_first_child(cfg, store):
    idx = "<sitemapindex><sitemap><loc>https://acme.io/sitemap-pages.xml</loc></sitemap>" \
          "<sitemap><loc>https://acme.io/sitemap-blog.xml</loc></sitemap></sitemapindex>"
    child = "<urlset><url><loc>https://acme.io/pricing</loc></url></urlset>"
    f = fetcher(cfg, store, {"https://acme.io/sitemap.xml": (200, idx),
                             "https://acme.io/sitemap-pages.xml": (200, child)})
    assert f.sitemap_urls("acme.io") == ["https://acme.io/pricing"]
