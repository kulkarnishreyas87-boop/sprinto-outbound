import pytest
from fakes import good_body

from sprinto.config import Settings
from sprinto.models import Contact, DecisionRecord, Draft, Evidence, ExtractedFields, Status
from sprinto.review import (ReviewError, approve, can_resume, edit, edit_in_editor, edit_ratio, editor_command,
                            order_queue, record_outcome, reject, send_approved, verdict)

ME = Settings(my_email="me@example.com", resend_api_key="x")


class Transport:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, payload):
        if self.fail:
            raise RuntimeError("resend down")
        self.sent.append(payload)
        return f"msg_{len(self.sent)}"


def drafted(domain="acme.io", tier="A", n_facts=1, **kw):
    fields = ExtractedFields(is_saas=Evidence(value=True, evidence_quote="q q q", source_url="u", verified=True))
    if n_facts > 1:
        fields.open_roles_total = Evidence(value=9, evidence_quote="q q q", source_url="u", verified=True)
    base = dict(record_id=f"r-{domain}", run_id="run1", config_version=1, domain=domain, status=Status.drafted,
                tier=tier, moment="first_certification", fields=fields,
                contact=Contact(name="Jane Doe", title="CTO", email="jane@acme.io"),
                drafts=[Draft(variant=1, subject="hello there", body=good_body("Jane"), hook_field="is_saas",
                              claim_id="c1")], chosen_draft=1)
    base.update(kw)
    return DecisionRecord(**base)


def feedback_rows(store):
    return [dict(r) for r in store.conn.execute("SELECT * FROM feedback ORDER BY id")]


# ---------- queue ----------

def test_queue_order_tier_then_evidence():
    recs = [drafted("c.io", tier="B"), drafted("a.io", tier="A"), drafted("b.io", tier="A", n_facts=2),
            drafted("z.io", tier=None, status=Status.needs_review)]
    assert [r.domain for r in order_queue(recs)] == ["b.io", "a.io", "c.io", "z.io"]


def test_store_queue_latest_per_domain_and_undecided_only(store):
    old = drafted("acme.io", record_id="old")
    store.save_record(old)
    new = drafted("acme.io", record_id="new", status=Status.needs_review)
    new.updated_at = old.updated_at.replace(year=old.updated_at.year + 1)
    store.save_record(new)
    decided = drafted("b.io")
    decided.review.decision = "approve"
    store.save_record(decided)
    store.save_record(drafted("c.io", status=Status.dropped))
    assert [r.record_id for r in store.review_queue()] == ["new"]


# ---------- edit ratio ----------

def test_edit_ratio():
    assert edit_ratio("a b c d", "a b c d") == 0.0
    assert edit_ratio("a b c d", "w x y z") == 1.0
    assert 0 < edit_ratio("one two three four five six", "one two three four five seven") < 0.33


# ---------- approve / edit / reject ----------

def test_approve_sends_to_my_email_and_logs(store):
    t = Transport()
    rec = approve(drafted(), store, ME, transport=t)
    assert rec.status == Status.sent and rec.send.message_id == "msg_1" and t.sent[0]["to"] == [ME.my_email]
    assert rec.review.decision == "approve" and rec.review.edit_ratio == 0.0
    fb = feedback_rows(store)
    assert (fb[0]["kind"], fb[0]["decision"], fb[0]["rewrite"]) == ("review", "approve", 0)
    assert store.get_record(rec.record_id).status == Status.sent


def test_failed_send_keeps_approval_then_retry(store):
    rec = drafted()
    with pytest.raises(RuntimeError):
        approve(rec, store, ME, transport=Transport(fail=True))
    saved = store.get_record(rec.record_id)
    assert saved.review.decision == "approve" and saved.status == Status.drafted
    send_approved(saved, store, ME, transport=Transport())
    assert store.get_record(rec.record_id).status == Status.sent


def test_approve_without_send(store):
    rec = approve(drafted(), store, ME, send=False)
    assert rec.status == Status.drafted and rec.review.decision == "approve"


def test_edit_requires_reason_code(store, cfg):
    with pytest.raises(ReviewError):
        edit(drafted(), store, ME, cfg, new_body=good_body("Jane"), reason_code="", hook_or_pain_changed=False)
    with pytest.raises(ReviewError):
        edit(drafted(), store, ME, cfg, new_body=good_body("Jane"), reason_code="vibes", hook_or_pain_changed=False)


def test_small_edit_is_not_a_rewrite_and_sends_edited_text(store, cfg):
    t = Transport()
    body = good_body("Jane").replace("short call", "short chat")
    rec = edit(drafted(), store, ME, cfg, new_body=body, new_subject="new subject", reason_code="wrong_tone",
               hook_or_pain_changed=False, transport=t)
    assert rec.review.rewrite is False and 0 < rec.review.edit_ratio < 0.33
    assert t.sent[0]["text"] == body.strip() and t.sent[0]["subject"].endswith("new subject")
    fb = feedback_rows(store)[0]
    assert fb["decision"] == "edit" and fb["reason_code"] == "wrong_tone" and fb["contact_title"] == "CTO"


@pytest.mark.parametrize("body,changed", [("Hi Jane,\n\nCompletely different words here entirely.", False),
                                          (None, True)])
def test_rewrite_by_ratio_or_by_hook_change(store, cfg, body, changed):
    new = body or good_body("Jane").replace("short call", "short chat")
    rec = edit(drafted(), store, ME, cfg, new_body=new, reason_code="weak_hook", hook_or_pain_changed=changed,
               send=False)
    assert rec.review.rewrite is True


def test_reject(store, cfg):
    rec = reject(drafted(), store, cfg, reason_code="wrong_person", note="CEO is better here")
    assert rec.status == Status.dropped and rec.reason_code == "rejected:wrong_person"
    assert feedback_rows(store)[0]["reason_code"] == "wrong_person"
    with pytest.raises(ReviewError):
        reject(drafted(), store, cfg, reason_code="")


def test_cannot_approve_review_queue_item(store):
    with pytest.raises(ReviewError):
        approve(drafted(status=Status.needs_review), store, ME)


# ---------- verdicts ----------

def review_item(reason="tier_c", moment="first_certification"):
    return drafted("q.io", tier="C", status=Status.needs_review, reason_code=reason, moment=moment, drafts=[],
                   chosen_draft=None, contact=None)


def test_verdict_drop_and_needs_more_info(store, cfg):
    rec = verdict(review_item(), store, cfg, decision="drop", reason_code="wrong_company", note="agency")
    assert rec.status == Status.dropped and rec.reason_code == "review:drop (tier_c)"
    rec2 = verdict(review_item(), store, cfg, decision="needs_more_info")
    assert rec2.status == Status.needs_review and rec2.review.decision == "needs_more_info"
    fb = feedback_rows(store)
    assert fb[0]["system_status"] == "needs_review" and '"system_reason": "tier_c"' in fb[0]["data_json"]
    with pytest.raises(ReviewError):
        verdict(review_item(), store, cfg, decision="maybe")


@pytest.mark.parametrize("reason,moment,ok", [("tier_c", "expansion", True), ("tier_c", None, False),
                                              ("insufficient_evidence", "expansion", False),
                                              ("no_suitable_contact", "expansion", False)])
def test_can_resume(reason, moment, ok):
    assert can_resume(review_item(reason, moment)) is ok


def test_qualify_resumes_from_s6_and_returns_to_queue(store, cfg, tmp_path):
    from test_contacts_gates import FakeHunter, person, resp
    from test_slice import CLAIMS, FakeLLM

    from sprinto.pipeline import resume_after_qualify
    rec = verdict(review_item(), store, cfg, decision="qualify", note="I know them, they're growing")
    h = FakeHunter(resp(person("Bob", "Tech", "CTO")))
    rec = resume_after_qualify(rec, cfg=cfg, claims=CLAIMS, store=store, llm=FakeLLM(draft_hook="is_saas"),
                               hunter=h, runs_dir=tmp_path)
    assert rec.status == Status.drafted and rec.contact.name == "Bob Tech" and h.calls == 1
    assert rec.tier == "B" and "human override" in rec.tier_rationale
    assert rec.review.decision is None and rec.review.history[0]["decision"] == "qualify"
    assert [r.record_id for r in store.review_queue()] == [rec.record_id]


# ---------- outcomes ----------

def test_outcome_only_for_sent_and_validated(store):
    with pytest.raises(ReviewError):
        record_outcome(drafted(), store, replied="positive")
    rec = approve(drafted(), store, ME, transport=Transport())
    with pytest.raises(ReviewError):
        record_outcome(rec, store, replied="great")
    with pytest.raises(ReviewError):
        record_outcome(rec, store, meeting="maybe")
    with pytest.raises(ReviewError):
        record_outcome(rec, store)
    rec = record_outcome(rec, store, replied="positive", meeting="yes", bounced="no")
    o = store.get_record(rec.record_id).outcome
    assert (o.replied, o.meeting, o.bounced, o.source) == ("positive", True, False, "manual")
    assert feedback_rows(store)[-1]["kind"] == "outcome"


# ---------- editor ----------

def test_editor_command_from_env(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "code --wait")
    assert editor_command() == ["code", "--wait"]


def test_edit_in_editor_round_trip():
    def runner(cmd, check):
        path = cmd[-1]
        text = open(path, encoding="utf-8").read().replace("old body", "new body").replace("old subj", "new subj")
        open(path, "w", encoding="utf-8").write(text)
    assert edit_in_editor("old subj", "Hi Jane,\nold body", runner=runner) == ("new subj", "Hi Jane,\nnew body")


# ---------- CLI ----------

@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    import sprinto.store as store_mod
    from sprinto.store import Store
    db = tmp_path / "p.db"
    monkeypatch.setattr(store_mod, "DEFAULT_DB", db)
    monkeypatch.setattr(Store.__init__, "__defaults__", (db,))
    monkeypatch.setattr("sprinto.pipeline.RUNS_DIR", tmp_path / "runs")
    s = Store(db)
    yield s
    s.close()


def test_cli_review_reject_and_verdict(cli_db):
    from typer.testing import CliRunner

    from sprinto.cli import app
    cli_db.save_record(drafted("a.io"))
    cli_db.save_record(review_item("insufficient_evidence", None))
    res = CliRunner().invoke(app, ["review", "--no-send"], input="r\n3\nhook was generic\nd\n\nnot a fit\n")
    assert res.exit_code == 0, res.output
    assert "Rejected: weak_hook" in res.output and "Verdict: drop" in res.output
    assert cli_db.get_record("r-a.io").reason_code == "rejected:weak_hook"
    assert cli_db.get_record("r-q.io").status == Status.dropped


def test_cli_review_edit(cli_db, monkeypatch):
    from typer.testing import CliRunner

    from sprinto.cli import app
    monkeypatch.setattr("sprinto.review.edit_in_editor",
                        lambda s, b: (s, b.replace("short call", "short chat")))
    cli_db.save_record(drafted("a.io"))
    res = CliRunner().invoke(app, ["review", "--no-send"], input="e\nwrong_tone\nn\n\n")
    assert res.exit_code == 0, res.output
    rec = cli_db.get_record("r-a.io")
    assert rec.review.decision == "edit" and rec.review.rewrite is False and rec.status == Status.drafted


def test_cli_outcome(cli_db):
    from typer.testing import CliRunner

    from sprinto.cli import app
    rec = drafted("a.io")
    approve(rec, cli_db, ME, transport=Transport())
    res = CliRunner().invoke(app, ["outcome", rec.record_id, "--replied", "neutral", "--meeting", "no"])
    assert res.exit_code == 0 and "replied=neutral meeting=False" in res.output
    assert CliRunner().invoke(app, ["outcome", "nope", "--replied", "neutral"]).exit_code == 1
