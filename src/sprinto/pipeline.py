"""Stage orchestration: S1 -> S2/S2b -> S3 -> S4 loop -> S6 -> S8. Send (S11) is a separate human step."""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .config import ROOT, Claims, Config
from .contacts import HunterClient, HunterError, choose_contact
from .extract import check_support, extract, merge
from .gates import check_hard_gates, detect_hosted_trust_tool
from .draft import draft_variants
from .pains import generate_pains
from .qa import judge, qa_check
from .fetch import Fetcher, normalize_domain
from .jobboards import detect_board
from .llm import LLMError, LLMProvider, LLMResult
from .loop import LoopState, fetch_target, next_target, unresolved
from .qualify import qualify
from .models import Contact, DecisionRecord, ExtractedFields, PageFetch, Rejection, StageFailure, Status
from .store import Store

RUNS_DIR = ROOT / "runs"


@contextmanager
def timed(rec: DecisionRecord, stage: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        rec.timings[stage] = rec.timings.get(stage, 0) + int((time.perf_counter() - t0) * 1000)


def add_cost(rec: DecisionRecord, res: LLMResult) -> None:
    rec.cost.tokens_in += res.tokens_in
    rec.cost.tokens_out += res.tokens_out
    rec.cost.usd_estimate = round(rec.cost.usd_estimate + res.usd, 6)


def _plain(value):
    if isinstance(value, list):
        return [v.model_dump() if hasattr(v, "model_dump") else v for v in value]
    return value


def export_record(rec: DecisionRecord, runs_dir: Path = RUNS_DIR) -> Path:
    d = runs_dir / rec.run_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rec.domain}.json"
    path.write_text(rec.model_dump_json(indent=2), encoding="utf-8")
    return path


def _detect_hosted_trust(fields: ExtractedFields, raw: str, url: str, cfg: Config) -> ExtractedFields:
    """Returns the same object when nothing new was found, a merged copy otherwise."""
    if fields.compliance_tool_named.known:
        return fields
    ev = detect_hosted_trust_tool(raw, url, cfg.gates)
    if ev is None:
        return fields
    return merge(fields, ExtractedFields(compliance_tool_named=ev))


def _extract_and_check(rec: DecisionRecord, llm: LLMProvider, domain: str, pages: dict[str, str],
                       stage: str) -> ExtractedFields:
    """Extract (quotes verified by code), then the support check. Rejections go on the record."""
    fields, rejected, res = extract(llm, domain, pages, rec.record_id)
    add_cost(rec, res)
    for name, why in rejected.items():
        p = getattr(res.parsed, name)
        rec.rejections.append(Rejection(stage=stage, field=name, reason=why, proposed_value=_plain(p.value),
                                        evidence_quote=p.evidence_quote, source_url=p.source_url))
    before = fields
    fields, unsupported, res = check_support(llm, fields, rec.record_id)
    if res is not None:
        add_cost(rec, res)
    for name, why in unsupported.items():
        ev = getattr(before, name)
        rec.rejections.append(Rejection(stage=f"{stage}b", field=name, reason=why, proposed_value=_plain(ev.value),
                                        evidence_quote=ev.evidence_quote, source_url=ev.source_url))
    return fields


def read_domains(path: Path) -> list[str]:
    """S0: normalise each line to its root domain, skip blanks/comments, dedupe (order kept)."""
    seen, out = set(), []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():   # -sig: Windows tools add a BOM
        d = normalize_domain(line.split("#")[0])
        if d and "." in d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def run_batch(domains: list[str], *, cfg: Config, claims: Claims, store: Store, fetcher: Fetcher,
              llm: LLMProvider, hunter: HunterClient | None, force: bool = False, dry_run: bool = False,
              runs_dir: Path = RUNS_DIR, today: date | None = None, on_record=None
              ) -> tuple[str, list[DecisionRecord], list[str]]:
    """Run every domain end to end with no mid-run decisions. Returns (run_id, records, skipped)."""
    skipped = [d for d in domains if not force and store.researched_recently(d, cfg.input.recheck_after_days)]
    todo = [d for d in domains if d not in skipped]
    run_id = store.create_run(cfg.version, todo, dry_run=dry_run)
    records = []
    for d in todo:
        try:
            rec = run_slice(d, cfg=cfg, claims=claims, store=store, fetcher=fetcher, llm=llm, run_id=run_id,
                            hunter=hunter, runs_dir=runs_dir, today=today)
        except Exception as e:  # run_slice already records its own failures; this is a last line of defence
            rec = DecisionRecord(record_id=uuid.uuid4().hex[:12], run_id=run_id, config_version=cfg.version,
                                 domain=d)
            fail(rec, "pipeline", "unexpected_error", f"{type(e).__name__}: {e}")
            store.save_record(rec)
        records.append(rec)
        if on_record:
            on_record(rec)
    store.finish_run(run_id)
    return run_id, records, skipped


def fail(rec: DecisionRecord, stage: str, code: str, detail: str = "",
         status: Status = Status.failed) -> DecisionRecord:
    rec.failures.append(StageFailure(stage=stage, reason_code=code, detail=detail[:300]))
    return rec.finish(status, code)


def run_slice(domain: str, *, cfg: Config, claims: Claims, store: Store, fetcher: Fetcher,
              llm: LLMProvider, run_id: str, contact_name: str | None = None,
              contact_title: str | None = None, hunter: HunterClient | None = None,
              runs_dir: Path = RUNS_DIR, today: date | None = None) -> DecisionRecord:
    domain = normalize_domain(domain)
    rec = DecisionRecord(record_id=uuid.uuid4().hex[:12], run_id=run_id,
                         config_version=cfg.version, domain=domain)
    try:
        # S1
        with timed(rec, "s1_scrape"):
            home = fetcher.homepage(domain)
        rec.pages_fetched.append(home.page)
        if home.page.status != "ok":
            return fail(rec, "s1", home.page.status)
        with timed(rec, "s1_scrape"):
            discovered = fetcher.discover_pages(domain, home.raw, home.final_url or home.page.url)
        rec.pages_discovered = discovered
        st = LoopState(pages={home.page.url: home.cleaned}, discovered=discovered, board=detect_board(home.raw))

        # S2 + S2b on the homepage
        with timed(rec, "s2_extract"):
            fields = _extract_and_check(rec, llm, domain, {home.page.url: home.cleaned}, "s2")
        fields = _detect_hosted_trust(fields, home.raw, home.page.url, cfg)
        rec.fields = fields

        # S3 hard gates: before any extra fetch or Hunter call
        rec.gates_hit = check_hard_gates(fields, cfg.gates)
        if rec.gates_hit:
            return rec.finish(Status.dropped, rec.gates_hit[0])

        # S4 evidence loop: one page at a time, hard cap, gates re-checked after every merge
        cap = cfg.loop.max_extra_fetches
        while (target := next_target(fields, st, cfg)) is not None:
            if rec.loop_iterations >= cap:
                left = unresolved(fields, st, cfg)
                rec.failures.append(StageFailure(stage="s4", reason_code="insufficient_evidence",
                                                 detail=f"cap {cap} reached; unresolved: {', '.join(left)}"))
                return rec.finish(Status.needs_review, "insufficient_evidence")
            with timed(rec, "s4_loop"):
                step = fetch_target(target, st, fetcher=fetcher, domain=domain, cfg=cfg)
            rec.pages_fetched.append(PageFetch(url=step.url, page_type=step.page_type, method=step.method,
                                               words=step.words, status=step.status, note=step.note))
            rec.loop_iterations += int(step.counted)
            # code-only detector on raw HTML: runs even when the page has no visible text
            detected = _detect_hosted_trust(fields, step.raw, step.url, cfg)
            if detected is not fields:
                fields = rec.fields = detected
                rec.gates_hit = check_hard_gates(fields, cfg.gates)
                if rec.gates_hit:
                    return rec.finish(Status.dropped, rec.gates_hit[0])
            if step.status != "ok":
                continue
            if step.new_fields is not None:          # job board: facts set by code from the API
                new = step.new_fields
            else:
                try:
                    with timed(rec, "s4_loop"):
                        new = _extract_and_check(rec, llm, domain, {step.url: st.pages[step.url]}, "s4")
                except LLMError as e:    # one page's extraction failing doesn't end the company
                    rec.failures.append(StageFailure(stage="s4", reason_code=e.reason_code,
                                                     detail=f"{step.page_type}: {e.detail}"[:300]))
                    continue
            fields = merge(fields, new)
            rec.fields = fields
            rec.gates_hit = check_hard_gates(fields, cfg.gates)
            if rec.gates_hit:
                return rec.finish(Status.dropped, rec.gates_hit[0])

        # S5 qualify: model proposes, code caps the tier (never raises it)
        with timed(rec, "s5_qualify"):
            decision, res = qualify(llm, domain, fields, cfg, today=today, record_id=rec.record_id)
        add_cost(rec, res)
        rec.moment, rec.tier = decision.moment, decision.tier
        rec.signals_used = decision.signals_used
        rec.tier_rationale = decision.rationale + " | code: " + " || ".join(decision.notes)
        if decision.moment == "disputed":
            rec.moment = None
            rec.failures.append(StageFailure(stage="s5", reason_code="moment_disputed",
                                             detail=" || ".join(decision.notes)[:300]))
            return rec.finish(Status.needs_review, "moment_disputed")
        if decision.moment == "none":
            return rec.finish(Status.dropped, "not_qualified:no_moment")
        if decision.tier == "C":
            # plausible but unproven: review queue, no Hunter call, no draft (v1)
            rec.failures.append(StageFailure(stage="s5", reason_code="tier_c", detail="; ".join(decision.notes)[:300]))
            return rec.finish(Status.needs_review, "tier_c")

        return _contact_and_draft(rec, domain=domain, fields=fields, cfg=cfg, claims=claims, llm=llm,
                                  hunter=hunter, contact_name=contact_name, contact_title=contact_title)
    except LLMError as e:
        return fail(rec, "llm", e.reason_code, e.detail)
    except Exception as e:  # one company's failure never stops the batch
        return fail(rec, "pipeline", "unexpected_error", f"{type(e).__name__}: {e}")
    finally:
        store.save_record(rec)
        export_record(rec, runs_dir)


def _contact_and_draft(rec: DecisionRecord, *, domain: str, fields: ExtractedFields, cfg: Config, claims: Claims,
                       llm: LLMProvider, hunter: HunterClient | None, contact_name: str | None = None,
                       contact_title: str | None = None) -> DecisionRecord:
    """S6 -> S7 -> S8 -> S9 for a tier A/B record. Raises LLMError; callers record failures."""
    # S6 contact (tier A/B only): manual override, or Hunter (cache first) + code ranking
    if contact_name:
        rec.contact = Contact(name=contact_name, title=contact_title, email="(entered by hand)",
                              why_chosen="entered by hand with --name; Hunter not called")
    else:
        if hunter is None:
            return fail(rec, "s6", "hunter_not_configured")
        if not claims.approved():
            # nothing could be drafted anyway: don't spend scarce Hunter quota
            return fail(rec, "s6", "no_approved_claims", "approve a claim in config/claims.yaml first")
        with timed(rec, "s6_contact"):
            try:
                response, called = hunter.domain_search(domain)
            except HunterError as e:
                return fail(rec, "s6", e.reason_code, e.detail)
        rec.cost.hunter_calls += int(called)
        open_role = fields.compliance_security_roles.known
        contact, reason = choose_contact(response, moment=rec.moment, has_open_security_role=open_role,
                                         cfg=cfg.contacts)
        if contact is None:
            rec.contact = None
            rec.failures.append(StageFailure(stage="s6", reason_code=reason,
                                             detail=f"{len((response.get('data') or {}).get('emails') or [])} Hunter results"))
            return rec.finish(Status.needs_review, reason)
        rec.contact = contact

    # S7 pains: exactly two hypotheses for this role in this moment
    with timed(rec, "s7_pains"):
        pains, results = generate_pains(llm, domain=domain, fields=fields, moment=rec.moment,
                                        contact_name=rec.contact.name, contact_title=rec.contact.title,
                                        record_id=rec.record_id)
    for r in results:
        add_cost(rec, r)
    rec.pains = pains

    # S8 draft variants -> S9 code QA; one regeneration with the QA failures as feedback
    feedback, next_variant = "", 1
    survivors: list = []
    for attempt in range(1 + cfg.qa.regenerate_attempts):
        with timed(rec, "s8_draft"):
            drafts, res = draft_variants(llm, domain=domain, fields=fields, claims=claims,
                                         contact_name=rec.contact.name, contact_title=rec.contact.title,
                                         moment=rec.moment, pains=pains, n=cfg.qa.variants,
                                         feedback=feedback, start_variant=next_variant,
                                         record_id=rec.record_id)
        add_cost(rec, res)
        rec.drafts += drafts
        next_variant += len(drafts)
        with timed(rec, "s9_qa"):
            results = [qa_check(d, fields=fields, claims=claims, contact=rec.contact, pains=pains, cfg=cfg)
                       for d in drafts]
        rec.qa_results += results
        survivors = [d for d, r in zip(drafts, results) if r.passed]
        if survivors:
            break
        feedback = "\n".join(f"- variant {r.variant}: {'; '.join(r.failures)}" for r in results)
    if not survivors:
        rec.failures.append(StageFailure(stage="s9", reason_code="qa_failed",
                                         detail=f"{len(rec.drafts)} variants, none passed code QA"))
        return rec.finish(Status.needs_review, "qa_failed")

    # S9 judge: picks the best surviving variant (code validates the pick)
    with timed(rec, "s9_qa"):
        rec.judge, res = judge(llm, survivors, fields=fields, contact=rec.contact, pains=pains,
                               record_id=rec.record_id)
    if res is not None:
        add_cost(rec, res)
    rec.chosen_draft = rec.judge.chosen_variant
    return rec.finish(Status.drafted)


def resume_after_qualify(rec: DecisionRecord, *, cfg: Config, claims: Claims, store: Store, llm: LLMProvider,
                         hunter: HunterClient | None, runs_dir: Path = RUNS_DIR) -> DecisionRecord:
    """The Growth Lead qualified a tier-C company: continue from S6 with the stored evidence.

    The system tier stays visible in the rationale; the human override is recorded, not hidden.
    """
    rec.review.history.append(rec.review.model_dump(mode="json", exclude={"history"}))
    rec.review = type(rec.review)(history=rec.review.history)       # back into the queue once drafted
    rec.tier_rationale += " || human override: qualified by Growth Lead (system tier C)"
    rec.tier = "B"
    rec.status, rec.reason_code = Status.pending, None
    try:
        return _contact_and_draft(rec, domain=rec.domain, fields=rec.fields, cfg=cfg, claims=claims, llm=llm,
                                  hunter=hunter)
    except LLMError as e:
        return fail(rec, "llm", e.reason_code, e.detail)
    except Exception as e:
        return fail(rec, "pipeline", "unexpected_error", f"{type(e).__name__}: {e}")
    finally:
        store.save_record(rec)
        export_record(rec, runs_dir)


run_company = run_slice
