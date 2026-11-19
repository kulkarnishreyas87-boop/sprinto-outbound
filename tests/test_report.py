from datetime import datetime, timezone

from sprinto.config import Claim, Claims
from sprinto.models import (Contact, Cost, DecisionRecord, Draft, Evidence, ExtractedFields, PageFetch,
                            Rejection, SendInfo, StageFailure, Status)
from sprinto.report import render_record, render_run, render_totals

CLAIMS = Claims(claims=[Claim(id="c1", text="Sprinto automates evidence collection.", approved=True)])


def ev(value, quote="a long enough verified quote", url="https://acme.io/"):
    return Evidence(value=value, evidence_quote=quote, source_url=url, verified=True)


def rec(domain="acme.io", status=Status.drafted, **kw):
    base = dict(record_id=f"id-{domain}", run_id="r1", config_version=1, domain=domain, status=status,
                pages_fetched=[PageFetch(url=f"https://{domain}/", words=420)],
                cost=Cost(tokens_in=1000, tokens_out=500, usd_estimate=0.02, hunter_calls=1),
                timings={"s1_scrape": 2000, "s2_extract": 3000})
    base.update(kw)
    return DecisionRecord(**base)


def drafted():
    return rec(
        fields=ExtractedFields(is_saas=ev(True),
                               compliance_security_roles=ev([{"title": "Security Engineer"}],
                                                            quote="We are hiring a Security Engineer to own SOC 2")),
        rejections=[Rejection(stage="s2b", field="certifications_named", reason="quote_does_not_support_value: sells it",
                              proposed_value=["SOC 2"], evidence_quote="Need a SOC 2 yesterday?")],
        contact=Contact(name="Bob Tech", title="CTO", email="bob@acme.io", hunter_confidence=94,
                        why_chosen="title 'CTO' matches 'cto'",
                        alternatives=[{"name": "Mia S", "title": "VP Sales", "excluded": "excluded_function"}]),
        drafts=[Draft(variant=1, subject="your soc 2 hire", body="Hi Bob, " + "word " * 90 + "ok?",
                      hook_field="compliance_security_roles", claim_id="c1")],
        chosen_draft=1)


def test_drafted_record_explains_person_and_email():
    out = render_record(drafted(), CLAIMS)
    assert "DRAFTED" in out and "waiting for review" in out
    assert 'is_saas = True  ← "a long enough verified quote" (https://acme.io/)' in out
    assert "[s2b] certifications_named: quote_does_not_support_value" in out and 'quote="Need a SOC 2' in out
    assert "Contact: Bob Tech, CTO (Hunter confidence 94)" in out and "why: title 'CTO'" in out
    assert "Mia S (VP Sales): excluded_function" in out
    assert "hook: compliance_security_roles = Security Engineer" in out and "own SOC 2" in out
    assert 'proof point: c1 "Sprinto automates evidence collection."' in out
    assert "93 words, 1 question mark(s)" in out   # "Hi Bob," + 90 + "ok?"
    assert "bob@acme.io" not in out          # prospect email stays out of the summary
    assert "not assessed yet" in out


def test_full_mode_includes_body():
    assert "Hi Bob," in render_record(drafted(), CLAIMS, full=True)
    assert "Hi Bob," not in render_record(drafted(), CLAIMS)


def test_dropped_record_shows_gate_and_evidence():
    r = rec("vanta.com", Status.dropped, reason_code="dropped:compliance_vendor",
            gates_hit=["dropped:compliance_vendor"],
            fields=ExtractedFields(is_compliance_vendor=ev(True, quote="Compliance, risk, and proof platform")))
    out = render_record(r)
    assert "DROPPED (dropped:compliance_vendor)" in out
    assert 'because is_compliance_vendor = True  ← "Compliance, risk, and proof platform"' in out


def test_needs_review_and_failed_show_reasons():
    nr = rec("b.io", Status.needs_review, reason_code="no_suitable_contact",
             failures=[StageFailure(stage="s6", reason_code="no_suitable_contact", detail="3 Hunter results")])
    assert "Needs review: no_suitable_contact" in render_record(nr) and "3 Hunter results" in render_record(nr)
    f = rec("c.io", Status.failed, reason_code="blocked", failures=[StageFailure(stage="s1", reason_code="blocked")],
            pages_fetched=[PageFetch(url="https://c.io/", status="blocked")])
    out = render_record(f)
    assert "Failed at s1: blocked" in out and "home: https://c.io/ [httpx, 0 words, blocked]" in out


def test_sent_record_shows_message_id():
    r = drafted()
    r.status = Status.sent
    r.send = SendInfo(message_id="msg_1", sent_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc))
    r.review.decision = "approve"
    assert "message id msg_1; review: approve" in render_record(r)


def test_totals_per_company():
    recs = [drafted(), rec("b.io", Status.dropped, reason_code="dropped:b2c"),
            rec("c.io", Status.failed, reason_code="blocked")]
    out = render_totals(recs)
    assert "Companies: 3 | drafted 1, dropped 1, failed 1" in out
    assert "$0.0600 total, $0.0200 per company" in out and "5.0s per company" in out
    assert "Hunter calls: 3" in out and "dropped:b2c ×1" in out and "blocked ×1" in out


def test_run_orders_sent_first_failed_last():
    out = render_run("r1", 1, [rec("z.io", Status.failed, reason_code="x"), drafted()])
    assert out.startswith("Run r1 (config v1)") and out.index("acme.io") < out.index("z.io")


def test_old_records_without_new_fields_still_render():
    old = DecisionRecord.model_validate_json(
        '{"record_id":"o","run_id":"r","config_version":1,"domain":"old.io","status":"dropped"}')
    assert "old.io" in render_record(old)


def test_report_cli_latest_and_list(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import sprinto.store as store_mod
    from sprinto.cli import app
    from sprinto.store import Store

    db = tmp_path / "p.db"
    monkeypatch.setattr(store_mod, "DEFAULT_DB", db)
    monkeypatch.setattr(Store.__init__, "__defaults__", (db,))
    s = Store(db)
    run_id = s.create_run(1, ["acme.io"])
    r = drafted()
    r.run_id = run_id
    s.save_record(r)
    s.close()
    runner = CliRunner()
    res = runner.invoke(app, ["report"])
    assert res.exit_code == 0, res.output
    assert f"Run {run_id}" in res.output and "Bob Tech" in res.output
    assert run_id in runner.invoke(app, ["report", "--list"]).output
    assert runner.invoke(app, ["report", "nope"]).exit_code == 1
