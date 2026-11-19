import json
import shutil
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml
from conftest import HOME_HTML

from sprinto.config import CONFIG_DIR, Settings, active_version, load_config
from sprinto.feedback import build_report, make_proposal, next_version, parse_since
from sprinto.goldset import (PromotionError, compare, evaluate, freeze, latest_result, load_fixtures, metrics,
                             promote, run_gold, save_result)
from sprinto.models import Contact, DecisionRecord, Draft, QAResult, Status

WEEK = datetime.now(timezone.utc) - timedelta(days=7)


def rec(domain, title="CTO", status=Status.drafted, **kw):
    base = dict(record_id=f"r-{domain}", run_id="run1", config_version=1, domain=domain, status=status, tier="A",
                moment="first_certification", contact=Contact(name="Jane Doe", title=title, email="j@x.io"),
                drafts=[Draft(variant=1, subject="hi", body="Hi Jane, body text here?", hook_field="is_saas")],
                chosen_draft=1)
    base.update(kw)
    return DecisionRecord(**base)


def log(store, r, kind, decision, **kw):
    store.save_record(r)
    store.log_feedback(r, kind, decision, **kw)


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(CONFIG_DIR / "v1.yaml", d / "v1.yaml")
    shutil.copy(CONFIG_DIR / "claims.yaml", d / "claims.yaml")
    (d / "ACTIVE").write_text("v1\n")
    return d


def test_parse_since():
    gap = datetime.now(timezone.utc) - parse_since("7d")
    assert abs(gap - timedelta(days=7)) < timedelta(seconds=5)
    assert abs(datetime.now(timezone.utc) - parse_since("24h") - timedelta(hours=24)) < timedelta(seconds=5)
    with pytest.raises(ValueError):
        parse_since("last week")


def populate(store):
    log(store, rec("a.io"), "review", "approve", edit_ratio=0.0, rewrite=False)
    log(store, rec("b.io"), "review", "edit", reason_code="wrong_tone", edit_ratio=0.1, rewrite=False)
    log(store, rec("c.io"), "review", "edit", reason_code="weak_hook", edit_ratio=0.6, rewrite=True)
    for i in range(3):
        log(store, rec(f"p{i}.io", title="Chief Revenue Officer"), "review", "reject", reason_code="wrong_person")
    for i in range(3):
        log(store, rec(f"w{i}.io"), "review", "reject", reason_code="wrong_company", note="agency")
    log(store, rec("f.io"), "review", "reject", reason_code="factual_error", note="said SOC 2, they don't have it")
    q = rec("q.io", status=Status.needs_review)
    log(store, q, "verdict", "qualify")
    log(store, rec("d.io", status=Status.needs_review), "verdict", "drop")
    log(store, rec("a.io"), "outcome", "outcome", data={"replied": "positive", "meeting": True, "bounced": False})


def test_pattern_report_metrics(store):
    populate(store)
    r = build_report(store, WEEK)
    assert r.reviewed_drafts == 10 and r.rewrites == 1
    assert (r.qual_agree, r.qual_judged) == (8, 12)   # 7 drafts kept + 1 drop agree; 3 wrong_company + 1 qualify don't
    assert r.wrong_person == 3 and r.wrong_person_titles["chief revenue officer"] == 3
    assert len(r.factual_errors) == 1 and len(r.wrong_company) == 3
    assert r.reasons_by_stage["S6 contact"]["wrong_person"] == 3
    assert r.outcomes["replied:positive"] == 1 and r.outcomes["meeting"] == 1
    assert {c["domain"] for c in r.example_candidates} == {"a.io", "b.io"}       # approve + light edit only
    text = r.render()
    assert "Rewrite rate: 10% (1/10)" in text and "Contact accuracy: 70% (7/10)" in text
    assert "BUG: f.io" in text


def test_proposal_writes_next_version_keeps_comments_and_validates(store, cfg_dir, tmp_path):
    populate(store)
    cfg = load_config(1, cfg_dir)
    p = make_proposal(build_report(store, WEEK), store, cfg, config_dir=cfg_dir, proposals_dir=tmp_path / "prop")
    assert p.version == 2 and (cfg_dir / "v2.yaml").exists()
    v2 = load_config(2, cfg_dir)
    assert "chief revenue officer" in v2.contacts.exclude_title_keywords
    text = (cfg_dir / "v2.yaml").read_text()
    assert "# Sprinto outbound pipeline: versioned config." in text and "PROPOSED" in text
    assert "+  exclude_title_keywords:" in p.diff and "chief revenue officer" in p.diff
    assert active_version(cfg_dir) == 1                                  # never activated by propose
    assert any("wrong_company" in f for f in p.flags) and any("BUG factual_error: f.io" in f for f in p.flags)
    ex = sorted((tmp_path / "prop" / "v2" / "examples").glob("*.md"))
    assert len(ex) == 2 and all(f.read_text().startswith("approved: false") for f in ex)
    assert not list((CONFIG_DIR / "examples").glob("*-r-*.md"))          # real examples untouched


def test_no_proposal_below_threshold(store, cfg_dir, tmp_path):
    for i in range(2):
        log(store, rec(f"p{i}.io", title="CRO"), "review", "reject", reason_code="wrong_person")
    cfg = load_config(1, cfg_dir)
    p = make_proposal(build_report(store, WEEK), store, cfg, config_dir=cfg_dir, proposals_dir=tmp_path / "prop")
    assert p.version is None and not (cfg_dir / "v2.yaml").exists() and p.flags == []


def test_dry_proposal_writes_nothing(store, cfg_dir, tmp_path):
    populate(store)
    p = make_proposal(build_report(store, WEEK), store, load_config(1, cfg_dir), config_dir=cfg_dir,
                      proposals_dir=tmp_path / "prop", write=False)
    assert p.diff and not (cfg_dir / "v2.yaml").exists() and not (tmp_path / "prop").exists()
    assert next_version(cfg_dir) == 2


# ---------- gold set ----------

def case(**exp):
    return {"id": "c", "domain": "acme.io", "expected": exp}


def test_evaluate_and_metrics():
    r = rec("acme.io", qa_results=[QAResult(variant=1, passed=True)])
    ok = evaluate(case(status="sent", tier="A", moment="first_certification", contact_title="cto"), r)
    assert ok.ok and ok.contact_ok and ok.qa_passed
    bad = evaluate(case(status="dropped", reason_code="dropped:b2c"), r)
    assert not bad.ok and "status drafted != dropped" in bad.problems[0]
    missing = evaluate(case(status="sent"), None)
    assert not missing.ok and "missing fixtures" in missing.problems[0]
    m = metrics([ok, bad, missing])
    assert m == {"qualification_agreement": 0.333, "contact_accuracy": 1.0, "qa_pass_rate": 1.0}


def test_allowed_tiers_and_statuses():
    r = rec("acme.io", tier="B")
    assert evaluate({"id": "x", "domain": "acme.io", "expected": {}, "allowed_tiers": ["A", "B"],
                     "allowed_statuses": ["sent", "needs_review"]}, r).ok
    assert not evaluate({"id": "x", "domain": "acme.io", "expected": {}, "allowed_tiers": ["A"]}, r).ok


def test_compare():
    base = {"qualification_agreement": 0.8, "contact_accuracy": 1.0, "qa_pass_rate": None}
    assert compare({"qualification_agreement": 0.8, "contact_accuracy": 1.0, "qa_pass_rate": 0.5}, base)[0]
    ok, notes = compare({"qualification_agreement": 0.6, "contact_accuracy": 1.0, "qa_pass_rate": 1.0}, base)
    assert not ok and any("FAIL" in n for n in notes)
    assert compare({"qualification_agreement": 1.0}, None)[0]


def test_freeze_and_load_round_trip(store, tmp_path):
    store.put_page("https://acme.io/", "acme.io", "home", "httpx", 200, HOME_HTML, "x")
    store.put_hunter("acme.io", {"data": {"emails": []}})
    out = freeze("acme.io", store, tmp_path / "fx")
    assert (out / "pages.json").exists() and (out / "hunter.json").exists()
    from sprinto.store import Store
    s2 = Store(tmp_path / "s2.db")
    assert load_fixtures("acme.io", s2, tmp_path / "fx") and s2.get_hunter("acme.io") == {"data": {"emails": []}}
    with pytest.raises(ValueError):
        freeze("nothing.io", store, tmp_path / "fx")
    s2.close()


def test_run_gold_offline_with_fixtures(store, tmp_path):
    from test_contacts_gates import person, resp
    from test_slice import CLAIMS, FakeLLM
    fx = tmp_path / "fx" / "acme.io"
    fx.mkdir(parents=True)
    (fx / "pages.json").write_text(json.dumps({"https://acme.io/": {"page_type": "home", "method": "httpx",
                                                                     "status_code": 200, "raw": HOME_HTML}}))
    (fx / "hunter.json").write_text(json.dumps(resp(person("Bob", "Tech", "CTO"))))
    cases = [{"id": "tier_a", "domain": "acme.io",
              "expected": {"status": "sent", "tier": "A", "moment": "first_certification", "contact_title": "CTO"}},
             {"id": "no_fixture", "domain": "ghost.io", "expected": {"status": "dropped"}}]
    results, m = run_gold(1, settings=Settings(), claims=CLAIMS, cases=cases, llm=FakeLLM(draft_hook="is_saas"),
                          fixtures_dir=tmp_path / "fx", today=date(2026, 11, 19))
    assert results[0].ok, results[0].problems
    assert results[0].actual["contact_title"] == "CTO" and not results[1].ok
    assert m == {"qualification_agreement": 0.5, "contact_accuracy": 1.0, "qa_pass_rate": 1.0}


def test_repo_gold_set_loads_six_cases():
    from sprinto.goldset import GOLD_FILE, load_cases
    cases = load_cases(GOLD_FILE)
    assert [c["id"] for c in cases] == ["tier_a_open_role", "soc2_expanding", "competitor_trust_centre",
                                        "b2c_or_non_saas", "thin_homepage_needs_careers", "hunter_only_sales"]
    assert len({c["domain"] for c in cases}) == 6        # one domain per case (runs are keyed by domain)


@pytest.mark.parametrize("moment,open_role", [("first_certification", False), ("expansion", False),
                                              ("expansion", True)])
def test_hand_written_vercel_fixture_has_no_suitable_contact(cfg, moment, open_role):
    from sprinto.contacts import choose_contact
    from sprinto.goldset import FIXTURES
    data = json.loads((FIXTURES / "vercel.com" / "hunter.json").read_text(encoding="utf-8"))
    assert all(e["value"].endswith("@example.invalid") for e in data["data"]["emails"])
    assert choose_contact(data, moment=moment, has_open_security_role=open_role, cfg=cfg.contacts) == \
        (None, "no_suitable_contact")


def test_freeze_includes_company_subdomains(store, tmp_path):
    store.put_page("https://acme.io/", "acme.io", "home", "httpx", 200, HOME_HTML, "x")
    store.put_page("https://trust.acme.io/", "trust.acme.io", "trust", "httpx", 200, "<html>vanta</html>", "")
    store.put_page("https://notacme.io/", "notacme.io", "home", "httpx", 200, "<html>other</html>", "")
    out = freeze("acme.io", store, tmp_path / "fx")
    urls = set(json.loads((out / "pages.json").read_text()))
    assert urls == {"https://acme.io/", "https://trust.acme.io/"}


def test_freeze_never_overwrites_hand_written_hunter(store, tmp_path):
    out = tmp_path / "fx" / "acme.io"
    out.mkdir(parents=True)
    (out / "hunter.json").write_text(json.dumps({"_fixture": "hand", "data": {"emails": []}}))
    store.put_page("https://acme.io/", "acme.io", "home", "httpx", 200, HOME_HTML, "x")
    store.put_hunter("acme.io", {"data": {"emails": [{"value": "real@acme.io"}]}})
    freeze("acme.io", store, tmp_path / "fx")
    assert json.loads((out / "hunter.json").read_text())["_fixture"] == "hand"
    assert (out / "pages.json").exists()


# ---------- promotion ----------

def test_promote_forward_needs_passing_gold_and_rollback_is_free(store, cfg_dir):
    text = (cfg_dir / "v1.yaml").read_text().replace("version: 1", "version: 2", 1)
    (cfg_dir / "v2.yaml").write_text(text)
    with pytest.raises(PromotionError, match="no gold-set result"):
        promote(2, store, cfg_dir)
    save_result(store, 2, [], {"qualification_agreement": 0.5}, False, 1)
    with pytest.raises(PromotionError, match="did not pass"):
        promote(2, store, cfg_dir)
    save_result(store, 2, [], {"qualification_agreement": 0.9}, True, 1)
    assert promote(2, store, cfg_dir) == "promoted: v1 -> v2" and active_version(cfg_dir) == 2
    assert promote(1, store, cfg_dir) == "rolled back: v2 -> v1" and active_version(cfg_dir) == 1
    assert latest_result(store, 2)["passed"] is True
    with pytest.raises(FileNotFoundError):
        promote(7, store, cfg_dir)


def test_pass_against_old_baseline_does_not_count(store, cfg_dir):
    for v in (2, 3):
        (cfg_dir / f"v{v}.yaml").write_text((cfg_dir / "v1.yaml").read_text().replace("version: 1", f"version: {v}", 1))
    save_result(store, 3, [], {}, True, 1)          # passed vs v1...
    (cfg_dir / "ACTIVE").write_text("v2\n")         # ...but v2 is active now
    with pytest.raises(PromotionError):
        promote(3, store, cfg_dir)


def test_cli_propose_dry(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import sprinto.store as store_mod
    from sprinto.cli import app
    from sprinto.store import Store
    db = tmp_path / "p.db"
    monkeypatch.setattr(Store.__init__, "__defaults__", (db,))
    s = Store(db)
    populate(s)
    s.close()
    res = CliRunner().invoke(app, ["propose", "--since", "7d", "--dry"])
    assert res.exit_code == 0, res.output
    assert "Pattern report" in res.output and "chief revenue officer" in res.output and "FLAG: BUG" in res.output
    assert not (CONFIG_DIR / "v2.yaml").exists()
