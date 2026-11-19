from datetime import date

import pytest

from sprinto.models import Evidence, ExtractedFields, Role, Status
from sprinto.qualify import QualifyOutput, compute_signals, decide

TODAY = date(2026, 11, 19)


def ev(value, quote="a verified quote of enough words"):
    return Evidence(value=value, evidence_quote=quote, source_url="u", verified=True)


def roles(*dates):
    return ev([Role(title=f"Security Engineer {i}", posted_date=d) for i, d in enumerate(dates)])


def run(cfg, fields, moment="first_certification", tier="A", used=()):
    sig = compute_signals(fields, cfg, TODAY)
    return decide(QualifyOutput(moment=moment, tier=tier, rationale="r", signals_used=list(used)), fields, sig)


ENTERPRISE = dict(enterprise_tier_present=ev(True))


def test_recent_security_role_and_stage_proxy_is_tier_a(cfg):
    d = run(cfg, ExtractedFields(compliance_security_roles=roles("2026-11-01"), **ENTERPRISE))
    assert d.tier == "A" and "within 60 days" in d.signals.strong[0]


def test_old_or_undated_role_still_strong_but_flagged(cfg):
    d = run(cfg, ExtractedFields(compliance_security_roles=roles("2026-06-01"), **ENTERPRISE))
    assert d.tier == "A" and "older than 60 days" in d.signals.strong[0]


def test_soc2_in_progress_is_strong_for_first_certification(cfg):
    f = ExtractedFields(certifications_named=ev(["SOC 2"], "we are working towards SOC 2 this year"), **ENTERPRISE)
    d = run(cfg, f)
    assert d.tier == "A" and d.signals.cert_in_progress and not d.signals.cert_held


def test_fit_only_caps_at_b(cfg):
    d = run(cfg, ExtractedFields(enterprise_customers_named=ev(["Globex"]), **ENTERPRISE), tier="A")
    assert d.tier == "B" and any("downgraded A->B" in n for n in d.notes)


def test_never_upgrades(cfg):
    d = run(cfg, ExtractedFields(compliance_security_roles=roles("2026-11-01"), **ENTERPRISE), tier="B")
    assert d.tier == "B"


def test_stage_unknown_caps_at_c(cfg):
    # strong signal, but no named round, <5 roles and no enterprise tier
    d = run(cfg, ExtractedFields(compliance_security_roles=roles("2026-11-01")))
    assert d.tier == "C" and any("stage unknown" in n for n in d.notes)


def test_five_open_roles_is_a_stage_proxy(cfg):
    d = run(cfg, ExtractedFields(compliance_security_roles=roles("2026-11-01"), open_roles_total=ev(12)))
    assert d.tier == "A" and "12 open roles" in "; ".join(d.signals.stage_proxies)


def test_named_round_is_a_stage_proxy_never_inferred(cfg):
    f = ExtractedFields(compliance_security_roles=roles("2026-11-01"), funding_stage_stated=ev("Series B"))
    assert run(cfg, f).tier == "A"


def test_expansion_requires_held_certification(cfg):
    d = run(cfg, ExtractedFields(compliance_security_roles=roles("2026-11-01"), **ENTERPRISE), moment="expansion")
    assert d.tier == "C" and any("needs a verified certification" in n for n in d.notes)


def test_expansion_with_held_soc2(cfg):
    f = ExtractedFields(certifications_named=ev(["SOC 2 Type II"], "Linear undergoes regular SOC 2 Type II audits"),
                        regions_served=ev(["EU", "US"]), **ENTERPRISE)
    d = run(cfg, f, moment="expansion", tier="A")
    assert d.signals.cert_held and d.tier == "B"          # fit (held cert, regions) but no trigger


def test_first_certification_contradicted_by_held_cert(cfg):
    f = ExtractedFields(certifications_named=ev(["SOC 2 Type II"], "we are SOC 2 Type II certified today"),
                        **ENTERPRISE)
    d = run(cfg, f, moment="first_certification")
    assert d.tier == "C" and any("contradicts" in n for n in d.notes)


def test_model_none_with_verified_trigger_is_disputed_not_dropped(cfg):
    d = run(cfg, ExtractedFields(compliance_security_roles=roles("2026-11-01"), **ENTERPRISE), moment="none",
            tier="C")
    assert d.moment == "disputed" and d.tier is None and any("verified strong signal" in n for n in d.notes)


def test_disputed_moment_goes_to_review_without_hunter(make_fetcher, store, tmp_path):
    from test_contacts_gates import ROUTES, CLAIMS, FakeHunter, person, resp
    from test_slice import FakeLLM
    from sprinto.config import load_config
    from sprinto.pipeline import run_slice
    h = FakeHunter(resp(person("Bob", "T", "CTO")))
    # acme fixture verifies "working towards SOC 2" (a strong signal); the model still says "none"
    rec = run_slice("acme.io", cfg=load_config(1), claims=CLAIMS, store=store, fetcher=make_fetcher(ROUTES),
                    llm=FakeLLM(moment="none", tier="C"), run_id="r1", hunter=h, runs_dir=tmp_path,
                    today=TODAY)
    assert (rec.status, rec.reason_code) == (Status.needs_review, "moment_disputed")
    assert h.calls == 0 and not rec.drafts


def test_moment_none_has_no_tier(cfg):
    d = run(cfg, ExtractedFields(**ENTERPRISE), moment="none", tier="C")
    assert d.moment == "none" and d.tier is None


def test_signals_used_must_be_verified(cfg):
    d = run(cfg, ExtractedFields(**ENTERPRISE), used=["enterprise_tier_present", "funding_stage_stated", "vibes"])
    assert d.signals_used == ["enterprise_tier_present"]
    assert any("funding_stage_stated" in n and "vibes" in n for n in d.notes)


def test_unverified_facts_never_count(cfg):
    f = ExtractedFields(compliance_security_roles=Evidence(value=[Role(title="CISO")], verified=False),
                        enterprise_tier_present=Evidence(value=True, verified=False))
    sig = compute_signals(f, cfg, TODAY)
    assert sig.strong == [] and sig.fit == [] and sig.stage_proxies == []


def test_sensitive_data_is_fit(cfg):
    sig = compute_signals(ExtractedFields(data_sensitivity=ev("health")), cfg, TODAY)
    assert sig.fit == ["data_sensitivity: health"]
    assert compute_signals(ExtractedFields(data_sensitivity=ev("none_stated")), cfg, TODAY).fit == []


# ---------- S0 and batch ----------

def test_read_domains_normalises_and_dedupes(tmp_path):
    from sprinto.pipeline import read_domains
    p = tmp_path / "d.txt"
    p.write_text("# prospects\nhttps://www.Acme.io/pricing\nacme.io\n\nlinear.app  # note\nnot a domain\n"
                 "http://b.co\n", encoding="utf-8")
    assert read_domains(p) == ["acme.io", "linear.app", "b.co"]


def test_read_domains_handles_windows_bom(tmp_path):
    from sprinto.pipeline import read_domains
    p = tmp_path / "bom.txt"
    p.write_text("posthog.com\nvercel.com\n", encoding="utf-8-sig")     # what PowerShell's Out-File writes
    assert read_domains(p) == ["posthog.com", "vercel.com"]


def test_batch_continues_after_failure_and_skips_recent(make_fetcher, store, cfg, tmp_path):
    from conftest import HOME_HTML
    from test_slice import CLAIMS, FakeLLM

    from sprinto.models import DecisionRecord
    from sprinto.pipeline import run_batch

    old = DecisionRecord(record_id="old", run_id="r0", config_version=1, domain="done.io")
    old.finish(Status.dropped, "dropped:b2c")
    store.save_record(old)
    routes = {"https://acme.io/robots.txt": (200, ""), "https://acme.io/": (200, HOME_HTML),
              "https://bad.io/": (403, "denied")}
    run_id, recs, skipped = run_batch(["bad.io", "acme.io", "done.io"], cfg=cfg, claims=CLAIMS, store=store,
                                      fetcher=make_fetcher(routes), llm=FakeLLM(), hunter=None,
                                      runs_dir=tmp_path, today=TODAY)
    assert skipped == ["done.io"]
    assert [(r.domain, r.status) for r in recs] == [("bad.io", Status.failed), ("acme.io", Status.failed)]
    assert recs[0].reason_code == "blocked" and recs[1].reason_code == "hunter_not_configured"
    assert all(r.run_id == run_id for r in recs) and store.get_run(run_id)["finished_at"]
    _, recs2, skipped2 = run_batch(["done.io"], cfg=cfg, claims=CLAIMS, store=store, fetcher=make_fetcher(routes),
                                   llm=FakeLLM(), hunter=None, force=True, runs_dir=tmp_path, today=TODAY)
    assert skipped2 == [] and recs2[0].domain == "done.io"


def test_dry_run_hunter_never_calls_api(cfg, store):
    from sprinto.contacts import HunterClient, HunterError
    h = HunterClient("key", store, cfg.contacts, cache_only=True)
    with pytest.raises(HunterError) as e:
        h.domain_search("acme.io")
    assert e.value.reason_code == "hunter_skipped_dry_run"
    store.put_hunter("acme.io", {"data": {"emails": []}})
    assert h.domain_search("acme.io") == ({"data": {"emails": []}}, False)
