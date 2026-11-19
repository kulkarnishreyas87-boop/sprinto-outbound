"""Live web app (open, no login): same-site token, redaction, run loop, caps, review. Pipeline, settings and Resend are faked."""
import re

import pytest
from fakes import good_body
from fastapi.testclient import TestClient

from sprinto.config import Settings
from sprinto.models import Contact, DecisionRecord, Draft, Pain, Status

ME = "me@example.com"


@pytest.fixture
def web(tmp_path, monkeypatch, cfg, make_fetcher):
    import sprinto.web as web
    from sprinto.store import Store
    from test_contacts_gates import FakeHunter, person, resp
    from test_slice import CLAIMS, ROUTES, FakeLLM

    db = tmp_path / "web.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(Store.__init__, "__defaults__", (db,))
    monkeypatch.setattr("sprinto.pipeline.RUNS_DIR", tmp_path / "runs")
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(web, "load_settings", lambda: Settings(my_email=ME, resend_api_key="x"))
    monkeypatch.setattr(web, "load_claims", lambda: CLAIMS)
    sent = []
    monkeypatch.setattr("sprinto.send.ResendTransport.send", lambda self, p: sent.append(p) or "msg_web")

    def parts(store):   # built on the request's own store, like the real _pipeline_parts
        import httpx
        from conftest import make_transport
        from sprinto.fetch import Fetcher
        routes = dict(ROUTES)
        routes.update({"https://b.io/robots.txt": (200, ""), "https://b.io/": (403, "denied")})
        fetcher = Fetcher(cfg.scrape, store, client=httpx.Client(transport=make_transport(routes)),
                          sleep=lambda s: None, renderer=None)
        return (cfg, CLAIMS, Settings(my_email=ME), FakeLLM(draft_hook="is_saas"), fetcher,
                FakeHunter(resp(person("Bob", "Tech", "CTO", email="bob@acme.io"))))

    monkeypatch.setattr(web, "_pipeline_parts", parts)
    client = TestClient(web.app)
    client.sent = sent
    client.db = db
    return client


def login(client):
    """No login any more: just load a page, which sets the same-site token cookie, and read the token."""
    return re.search(r'name="csrf" value="([0-9a-f]+)"', client.get("/run").text).group(1)


def seed_record(db, status=Status.drafted):
    from sprinto.store import Store
    s = Store(db)
    run_id = s.create_run(1, ["acme.io"])
    rec = DecisionRecord(record_id="r-acme", run_id=run_id, config_version=1, domain="acme.io", status=status,
                         tier="A", moment="first_certification",
                         contact=Contact(name="Jane Doe", title="CTO", email="jane@acme.io",
                                         why_chosen="title 'CTO' matches 'cto'",
                                         alternatives=[{"name": "Mia Sales", "title": "VP Sales",
                                                        "excluded": "excluded_function"}]),
                         pains=[Pain(hypothesis="Jane's team is likely drowning in audits.", linked_fields=["x"])],
                         drafts=[Draft(variant=1, subject="hello there", body=good_body("Jane"),
                                       hook_field="is_saas", claim_id="c1")], chosen_draft=1)
    s.save_record(rec)
    s.close()
    return run_id


# ---------- public view ----------

def test_report_hides_personal_data_for_everyone(web):
    run_id = seed_record(web.db)
    home = web.get("/")
    assert home.status_code == 200 and run_id in home.text and "hidden on this open site" in home.text
    login(web)                       # having a token changes nothing: there is no fuller view
    page = web.get(f"/runs/{run_id}").text
    assert "acme.io" in page and "Open view" in page
    for secret in ("Jane Doe", "Mia Sales", "drowning in audits", "Hi Jane", "jane@acme.io"):
        assert secret not in page, secret
    assert "(name hidden)" in page and "CTO" in page


# ---------- open site, same-site guard ----------

def test_no_login_needed_to_open_run_and_review(web):
    seed_record(web.db)
    assert web.get("/run").status_code == 200 and web.get("/review").status_code == 200
    assert web.get("/login").status_code == 404
    assert "Operator login" not in web.get("/").text and "New run" in web.get("/").text


def test_post_without_token_is_refused(web):
    seed_record(web.db)
    web.get("/run")                 # cookie set, but the token is not sent
    assert web.post("/runs", data={"domains": "a.io"}).status_code == 403
    assert web.post("/api/runs/x/next").status_code == 403
    assert web.post("/review/r-acme", data={"action": "approve"}).status_code == 403 and web.sent == []


def test_token_without_its_cookie_is_refused(web):
    csrf = login(web)
    web.cookies.clear()
    assert web.post("/runs", data={"domains": "a.io", "csrf": csrf}).status_code == 403


def test_cross_site_origin_is_refused(web):
    csrf = login(web)
    r = web.post("/runs", data={"domains": "a.io", "csrf": csrf}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = web.post("/runs", data={"domains": "a.io", "csrf": csrf}, headers={"Origin": "http://testserver"},
                  follow_redirects=False)
    assert ok.status_code == 303


def test_running_off_on_vercel_without_keys(web, monkeypatch):
    import sprinto.web as w
    csrf = login(web)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("DATABASE_URL", "unused")                    # only can_run is under test here
    for k in ("GEMINI_API_KEY", "HUNTER_API_KEY", "RESEND_API_KEY", "MY_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    assert w.can_run() is False
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        w.check_post(object(), csrf)
    assert e.value.status_code == 503


# ---------- runs ----------

def test_run_loop_one_company_per_request(web):
    csrf = login(web)
    r = web.post("/runs", data={"domains": "https://www.acme.io/pricing\nb.io\nacme.io", "csrf": csrf},
                 follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/live")
    run_id = r.headers["location"].split("/")[2]
    live = web.get(f"/runs/{run_id}/live").text
    assert "acme.io" in live and "b.io" in live
    h = {"X-CSRF-Token": csrf}
    first = web.post(f"/api/runs/{run_id}/next", headers=h).json()
    if first["status"] == "failed":
        from sprinto.store import Store
        s = Store(web.db)
        recs = s.records_for_run(run_id)
        s.close()
        pytest.fail(str([f.model_dump() for r in recs for f in r.failures]))
    assert first["domain"] == "acme.io" and first["status"] == "drafted" and first["tier"] == "A"
    second = web.post(f"/api/runs/{run_id}/next", headers=h).json()
    assert second["domain"] == "b.io" and second["status"] == "failed" and second["reason"] == "blocked"
    assert web.post(f"/api/runs/{run_id}/next", headers=h).json() == {"done": True, "busy": False}
    report = web.get(f"/runs/{run_id}").text
    assert "Run totals" in report and "Bob Tech" not in report and "bob@acme.io" not in report


def test_run_caps_domains_and_allows_one_active_batch(web):
    csrf = login(web)
    many = "\n".join(f"c{i}.io" for i in range(16))
    r = web.post("/runs", data={"domains": many, "csrf": csrf})
    assert "between 1 and 15" in r.text
    first = web.post("/runs", data={"domains": "acme.io", "csrf": csrf}, follow_redirects=False)
    second = web.post("/runs", data={"domains": "b.io", "csrf": csrf}, follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]          # sent back to the active run


def test_daily_company_cap_is_shared_and_counts_admitted_companies(web, monkeypatch):
    import sprinto.web as w
    monkeypatch.setattr(w, "DAILY_COMPANY_CAP", 3)
    csrf = login(web)
    r = web.post("/runs", data={"domains": "a1.io,a2.io", "csrf": csrf}, follow_redirects=False)
    run_id = r.headers["location"].split("/")[2]
    from sprinto.store import Store
    s = Store(web.db)
    for d in ("a1.io", "a2.io"):           # finish the batch so it is no longer the active one
        s.claim_next_item(run_id); s.finish_item(run_id, d, None)
    s.close()
    assert "2 / 3 companies today" in web.get("/run").text
    over = web.post("/runs", data={"domains": "b1.io,b2.io", "csrf": csrf})
    assert over.status_code == 429 and "1 left right now" in over.text
    ok = web.post("/runs", data={"domains": "b1.io", "csrf": csrf}, follow_redirects=False)
    assert ok.status_code == 303


def test_recently_researched_domain_is_skipped_unless_forced(web):
    seed_record(web.db, status=Status.dropped)
    csrf = login(web)
    r = web.post("/runs", data={"domains": "acme.io", "csrf": csrf}, follow_redirects=False)
    run_id = r.headers["location"].split("/")[2]
    assert "skipped" in web.get(f"/runs/{run_id}").text


# ---------- review ----------

def test_review_approve_sends_only_to_my_email(web):
    seed_record(web.db)
    csrf = login(web)
    page = web.get("/review").text
    assert "acme.io" in page and "Approve" in page
    r = web.post("/review/r-acme", data={"action": "approve", "csrf": csrf}, follow_redirects=True)
    assert "Approved and sent acme.io" in r.text
    assert len(web.sent) == 1 and web.sent[0]["to"] == [ME]
    assert "jane@acme.io" not in str(web.sent[0]["to"])
    assert "Nothing waiting for review" in web.get("/review").text


def test_review_edit_needs_reason_code(web):
    seed_record(web.db)
    csrf = login(web)
    r = web.post("/review/r-acme", data={"action": "edit", "csrf": csrf, "subject": "s",
                                          "body": good_body("Jane")}, follow_redirects=True)
    assert "Not done for acme.io" in r.text and "reason code required" in r.text and web.sent == []


def test_review_reject_and_outcome(web):
    seed_record(web.db)
    csrf = login(web)
    r = web.post("/review/r-acme", data={"action": "reject", "reason_code": "wrong_person", "csrf": csrf},
                 follow_redirects=True)
    assert "Rejected acme.io: wrong_person" in r.text


def test_review_masks_names_and_edit_fills_them_back(web):
    seed_record(web.db)
    csrf = login(web)
    page = web.get("/review").text
    for secret in ("Jane", "Doe", "jane@acme.io", "Mia Sales", "drowning in audits"):
        assert secret not in page, secret
    assert "[first name]" in page
    body = re.search(r'<textarea id="body-r-acme" name="body" rows="11">(.*?)</textarea>', page, re.S).group(1)
    import html
    r = web.post("/review/r-acme", data={"action": "edit", "csrf": csrf, "subject": "hello there",
                                          "body": html.unescape(body), "reason_code": "wrong_tone"},
                 follow_redirects=True)
    assert "Edited and sent acme.io" in r.text, r.text[:2000]
    assert web.sent[0]["to"] == [ME] and "Jane" in web.sent[0]["text"] and "[first name]" not in web.sent[0]["text"]


def test_mask_and_unmask_roundtrip():
    from sprinto.web import mask_people, unmask_people
    rec = DecisionRecord(record_id="r", run_id="x", config_version=1, domain="acme.io", status=Status.drafted,
                         contact=Contact(name="Jane Doe", title="CTO", email="jane@acme.io"))
    text = "Hi Jane, Doe here; write to jane@acme.io. Janet is someone else."
    m = mask_people(text, rec)
    assert m == "Hi [first name], [last name] here; write to [email]. Janet is someone else."
    assert unmask_people(m, rec) == text


def test_hunter_usage_shown_on_run_page(web):
    login(web)
    assert "Hunter 0 / 20 this month" in web.get("/run").text


def test_summary_api_is_counts_only(web):
    run_id = seed_record(web.db)
    s = web.get("/api/summary").json()
    assert s["companies"] == 1 and s["drafted"] == 1 and s["runs"][0]["run_id"] == run_id
    assert s["runs"][0]["counts"] == {"drafted": 1} and s["hunter_cap"] == 20
    raw = web.get("/api/summary").text
    for secret in ("Jane", "Mia", "jane@acme.io", "drowning", "Hi Jane"):
        assert secret not in raw, secret


def test_dashboard_renders_tiles_bar_and_runs(web):
    run_id = seed_record(web.db)
    page = web.get("/").text
    assert "Companies researched" in page and "Outcome mix" in page and run_id in page
    assert 'class="seg c-good"' in page and "Drafted or sent: 1 of 1 (100%)" in page
    assert "New run" in page and "Log in" not in page


def test_report_has_filter_chips_and_status_pills(web):
    run_id = seed_record(web.db)
    page = web.get(f"/runs/{run_id}").text
    assert 'data-f="drafted"' in page and "pill st-drafted" in page and 'id="q"' in page


def test_vercel_without_database_shows_setup_page_not_500(web, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "sekret-value-should-never-render")
    r = web.get("/")
    assert r.status_code == 503 and "Setup needed" in r.text and "DATABASE_URL" in r.text
    assert "GEMINI_API_KEY" not in r.text.split("Missing settings")[1].split("</ul>")[0]   # set => not listed
    assert "sekret-value" not in r.text
    assert web.get("/healthz").json() == {"ok": True}


def test_healthz(web):
    assert web.get("/healthz").json() == {"ok": True}
