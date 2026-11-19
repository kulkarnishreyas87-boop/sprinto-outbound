"""Live web app: auth, CSRF, redaction, run loop, caps, review. Pipeline, settings and Resend are faked."""
import re

import pytest
from fakes import good_body
from fastapi.testclient import TestClient

from sprinto.config import Settings
from sprinto.models import Contact, DecisionRecord, Draft, Pain, Status

PASSWORD = "correct horse battery staple"
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
    monkeypatch.setenv("OPERATOR_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
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
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
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

def test_public_report_hides_personal_data(web):
    run_id = seed_record(web.db)
    home = web.get("/")
    assert home.status_code == 200 and run_id in home.text and "hides people" in home.text
    page = web.get(f"/runs/{run_id}").text
    assert "acme.io" in page and "Public view" in page
    for secret in ("Jane Doe", "Mia Sales", "drowning in audits", "Hi Jane", "jane@acme.io"):
        assert secret not in page, secret
    assert "(name hidden)" in page and "CTO" in page


def test_operator_report_shows_everything(web):
    run_id = seed_record(web.db)
    login(web)
    page = web.get(f"/runs/{run_id}").text
    assert "Jane Doe" in page and "drowning in audits" in page and "Hi Jane" in page


# ---------- auth and CSRF ----------

@pytest.mark.parametrize("method,path", [("get", "/run"), ("get", "/review"), ("post", "/runs"),
                                         ("post", "/api/runs/x/next"), ("post", "/review/r-acme")])
def test_operator_routes_need_login(web, method, path):
    if method == "post":
        r = web.post(path, data={"domains": "a.io", "action": "approve"})
    else:
        r = web.get(path)
    assert r.status_code == 401


def test_wrong_password_rejected(web):
    r = web.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 200 and "Wrong password" in r.text and "sprinto_session" not in r.cookies


def test_post_without_csrf_is_refused(web):
    login(web)
    assert web.post("/runs", data={"domains": "a.io"}).status_code == 403
    assert web.post("/api/runs/x/next").status_code == 403


def test_forged_cookie_is_not_a_session(web):
    web.cookies.set("sprinto_session", "9999999999.abcd.deadbeef")
    assert web.get("/run").status_code == 401


def test_operator_disabled_without_secrets(web, monkeypatch):
    monkeypatch.delenv("OPERATOR_PASSWORD")
    assert web.post("/login", data={"password": PASSWORD}).status_code == 503
    assert web.get("/run").status_code == 503
    assert web.get("/").status_code == 200 and "Operator login" not in web.get("/").text


def test_short_session_secret_disables_operator(web, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "short")
    assert web.get("/run").status_code == 503


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
    assert "Run totals" in report and "Bob Tech" in report       # operator view


def test_run_caps_domains_and_allows_one_active_batch(web):
    csrf = login(web)
    many = "\n".join(f"c{i}.io" for i in range(16))
    r = web.post("/runs", data={"domains": many, "csrf": csrf})
    assert "between 1 and 15" in r.text
    first = web.post("/runs", data={"domains": "acme.io", "csrf": csrf}, follow_redirects=False)
    second = web.post("/runs", data={"domains": "b.io", "csrf": csrf}, follow_redirects=False)
    assert second.headers["location"] == first.headers["location"]          # sent back to the active run


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


def test_review_without_csrf_refused(web):
    seed_record(web.db)
    login(web)
    assert web.post("/review/r-acme", data={"action": "approve"}).status_code == 403 and web.sent == []


def test_hunter_usage_shown_on_run_page(web):
    login(web)
    assert "0 of 20 searches used" in web.get("/run").text


def test_healthz(web):
    assert web.get("/healthz").json() == {"ok": True}
