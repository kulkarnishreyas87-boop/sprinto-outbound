"""Live web app (Vercel): open to everyone, no login, with guardrails.

Everything the pipeline decides is still decided by the same code as the CLI: this module only
wires HTTP to `pipeline.run_slice`, `report.render_*` and `review.*`.

Guardrails (there is no password, so these are what protect the API credits and people's data):
- people's names and emails are hidden on every page: reports use the public render and the review
  form shows drafts with names masked as placeholders, which are filled back in before sending;
- every POST needs a same-site token (double-submit cookie) and a matching Origin when one is sent,
  so other websites cannot start runs or approve drafts on a visitor's behalf;
- caps: 15 domains per batch, one active batch, 30 companies per rolling 24 hours, Hunter's monthly
  cap in config; approved emails still only ever go to MY_EMAIL (enforced in send.py).
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import load_claims, load_config, load_settings
from .models import DecisionRecord, Status
from .store import Store

MAX_DOMAINS = 15
DAILY_COMPANY_CAP = 30
CSRF_COOKIE = "sprinto_csrf"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Sprinto Outbound", docs_url=None, redoc_url=None, openapi_url=None)


# ---------- helpers ----------

class NotConfigured(Exception):
    pass


REQUIRED_ENV = ["DATABASE_URL", "GEMINI_API_KEY", "HUNTER_API_KEY", "RESEND_API_KEY", "MY_EMAIL"]


def get_store() -> Store:
    # On Vercel the bundle is read-only: without Postgres there is nowhere to keep data.
    if os.environ.get("VERCEL") and not os.environ.get("DATABASE_URL"):
        raise NotConfigured()
    return Store()


@app.exception_handler(NotConfigured)
def not_configured(request: Request, exc: NotConfigured):
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]     # names only, never values
    return TEMPLATES.TemplateResponse(request, "setup.html", {
        "request": request, "missing": missing, "can_run": False, "csrf": ""}, status_code=503)


@lru_cache(maxsize=1)
def cfg():
    return load_config()


def can_run() -> bool:
    """Running needs the API keys. Off Vercel (CLI checkout, tests) settings come from .env / fakes."""
    if not os.environ.get("VERCEL"):
        return True
    return all(os.environ.get(k) for k in REQUIRED_ENV)


def _csrf_cookie(request: Request) -> str:
    tok = request.cookies.get(CSRF_COOKIE, "")
    return tok if re.fullmatch(r"[0-9a-f]{32}", tok) else ""


def check_post(request: Request, csrf: str | None = None) -> None:
    """Same-site check for every POST: a cross-site page can neither read our cookie nor forge Origin."""
    if not can_run():
        raise HTTPException(503, "Running is switched off until the API keys are added.")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", request.url.netloc):
        raise HTTPException(403, "Cross-site request refused.")
    cookie = _csrf_cookie(request)
    sent = csrf or request.headers.get("x-csrf-token", "")
    if not cookie or not hmac.compare_digest(sent, cookie):
        raise HTTPException(403, "Page expired: reload it and try again.")


def render(request: Request, name: str, status_code: int = 200, **ctx):
    tok = _csrf_cookie(request) or secrets.token_hex(16)
    ctx.update(request=request, can_run=can_run(), csrf=tok)
    resp = TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)
    if tok != request.cookies.get(CSRF_COOKIE):
        resp.set_cookie(CSRF_COOKIE, tok, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=7 * 24 * 3600)
    return resp


# ---------- people's names stay hidden ----------

def _name_swaps(rec: DecisionRecord) -> list[tuple[str, str]]:
    """(real text, placeholder) pairs, longest first, for the chosen contact and the alternatives."""
    swaps: list[tuple[str, str]] = []
    c = rec.contact
    if c:
        if c.email:
            swaps.append((c.email, "[email]"))
        parts = (c.name or "").split()
        if len(parts) > 1:
            swaps += [(c.name, "[full name]"), (parts[-1], "[last name]")]
        if parts:
            swaps.append((parts[0], "[first name]"))
        for a in c.alternatives:
            if a.get("name"):
                swaps.append((str(a["name"]), "[someone]"))
    return sorted({s for s in swaps if len(s[0]) >= 2}, key=lambda s: -len(s[0]))


def mask_people(text: str, rec: DecisionRecord) -> str:
    for real, ph in _name_swaps(rec):
        text = re.sub(rf"(?<!\w){re.escape(real)}(?!\w)", ph, text)
    return text


def unmask_people(text: str, rec: DecisionRecord) -> str:
    for real, ph in _name_swaps(rec):
        if ph != "[someone]":
            text = text.replace(ph, real)
    return text


def _pipeline_parts(store: Store):
    from .contacts import HunterClient
    from .fetch import Fetcher
    from .llm import make_provider

    c, s = cfg(), load_settings()
    return c, load_claims(), s, make_provider(c, store, s), Fetcher(c.scrape, store), \
        HunterClient(s.hunter_api_key, store, c.contacts)


# ---------- public pages ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


def _summary(store: Store) -> dict:
    """Dashboard numbers. Counts and costs only: safe to serve publicly."""
    runs, total = [], Counter()
    usd = secs = 0.0
    for r in store.recent_runs(30):
        recs = store.records_for_run(r["run_id"])
        counts = Counter(x.status.value for x in recs)
        total.update(counts)
        run_usd = sum(x.cost.usd_estimate for x in recs)
        usd += run_usd
        secs += sum(sum(x.timings.values()) for x in recs) / 1000
        items = store.run_items(r["run_id"])
        runs.append({"run_id": r["run_id"], "version": r["config_version"], "started": str(r["started_at"])[:16].replace("T", " "),
                     "n": len(recs), "counts": dict(counts), "usd": round(run_usd, 4),
                     "pending": sum(1 for i in items if i["state"] in ("pending", "running"))})
    companies = sum(total.values())
    c = cfg()
    return {
        "runs": runs,
        "totals": dict(total),
        "companies": companies,
        "drafted": total.get("drafted", 0) + total.get("sent", 0),
        "sent": total.get("sent", 0),
        "needs_review": total.get("needs_review", 0),
        "dropped": total.get("dropped", 0),
        "failed": total.get("failed", 0),
        "usd_total": round(usd, 4),
        "usd_per_company": round(usd / companies, 4) if companies else 0.0,
        "secs_per_company": round(secs / companies, 1) if companies else 0.0,
        "hunter_used": store.hunter_calls_since(30),
        "hunter_cap": c.contacts.hunter_monthly_cap,
        "database": "Postgres" if store.is_postgres else "SQLite",
        "can_run": can_run(),
        "today_used": store.companies_started_since(24),
        "today_cap": DAILY_COMPANY_CAP,
        "config": c.version,
        "active_run": store.active_run_id(),
    }


@app.get("/api/summary")
def api_summary():
    store = get_store()
    try:
        return JSONResponse(_summary(store), headers={"Cache-Control": "no-store"})
    finally:
        store.close()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    store = get_store()
    try:
        return render(request, "index.html", s=_summary(store))
    finally:
        store.close()


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_report(request: Request, run_id: str):
    from .report import render_record, render_totals

    store = get_store()
    try:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "No such run.")
        public = True       # open site: people's names, pains and email bodies never render here
        order = {Status.sent: 0, Status.drafted: 1, Status.needs_review: 2, Status.dropped: 3, Status.failed: 4}
        recs = sorted(store.records_for_run(run_id), key=lambda r: (order.get(r.status, 9), r.domain))
        claims = load_claims()
        blocks = [{"domain": r.domain, "status": r.status.value, "reason": r.reason_code, "tier": r.tier,
                   "text": render_record(r, claims, full=not public, public=public)} for r in recs]
        return render(request, "report.html", run_id=run_id, version=run["config_version"], blocks=blocks,
                      totals=render_totals(recs), public=public, items=store.run_items(run_id))
    finally:
        store.close()


# ---------- runs ----------

@app.get("/run", response_class=HTMLResponse)
def run_page(request: Request):
    store = get_store()
    try:
        return _run_form(request, store, error=None)
    finally:
        store.close()


def _run_form(request: Request, store: Store, error: str | None, status_code: int = 200):
    c = cfg()
    return render(request, "run.html", status_code=status_code, active=store.active_run_id(),
                  max_domains=MAX_DOMAINS, hunter_used=store.hunter_calls_since(30),
                  hunter_cap=c.contacts.hunter_monthly_cap, approved_claims=len(load_claims().approved()),
                  today_used=store.companies_started_since(24), today_cap=DAILY_COMPANY_CAP, error=error)


@app.post("/runs")
def start_run(request: Request, domains: str = Form(...), force: str = Form(""), csrf: str = Form("")):
    check_post(request, csrf)
    from .pipeline import read_domains_text

    store = get_store()
    try:
        active = store.active_run_id()
        if active:
            return RedirectResponse(f"/runs/{active}/live", status_code=303)
        ds = read_domains_text(domains)
        c = cfg()
        if not ds or len(ds) > MAX_DOMAINS:
            return _run_form(request, store, f"Give between 1 and {MAX_DOMAINS} valid domains (you gave {len(ds)}).")
        skipped = [] if force else [d for d in ds if store.researched_recently(d, c.input.recheck_after_days)]
        todo = [d for d in ds if d not in skipped]
        left = max(0, DAILY_COMPANY_CAP - store.companies_started_since(24))
        if len(todo) > left:
            return _run_form(request, store, status_code=429, error=(
                f"Daily limit: {DAILY_COMPANY_CAP} companies per 24 hours across all visitors. "
                f"{left} left right now, and this batch needs {len(todo)}."))
        run_id = store.create_run(c.version, todo)
        store.add_run_items(run_id, todo, skipped)
        if not todo:
            store.finish_run(run_id)
        return RedirectResponse(f"/runs/{run_id}/live", status_code=303)
    finally:
        store.close()


@app.get("/runs/{run_id}/live", response_class=HTMLResponse)
def run_live(request: Request, run_id: str):
    store = get_store()
    try:
        if store.get_run(run_id) is None:
            raise HTTPException(404, "No such run.")
        return render(request, "live.html", run_id=run_id, items=store.run_items(run_id))
    finally:
        store.close()


@app.post("/api/runs/{run_id}/next")
def run_next(request: Request, run_id: str):
    """Process exactly one pending company (one function invocation per company)."""
    check_post(request)
    from .pipeline import run_slice

    store = get_store()
    try:
        if store.get_run(run_id) is None:
            raise HTTPException(404, "No such run.")
        store.reset_stale_items(run_id)
        domain = store.claim_next_item(run_id)
        if domain is None:
            busy = any(i["state"] == "running" for i in store.run_items(run_id))
            if not busy:
                store.finish_run(run_id)
            return JSONResponse({"done": not busy, "busy": busy})
        try:
            c, claims, s, llm, fetcher, hunter = _pipeline_parts(store)
            rec = run_slice(domain, cfg=c, claims=claims, store=store, fetcher=fetcher, llm=llm, run_id=run_id,
                            hunter=hunter)
            store.finish_item(run_id, domain, rec.record_id)
            return JSONResponse({"done": False, "domain": domain, "status": rec.status.value,
                                 "reason": rec.reason_code, "tier": rec.tier})
        except Exception as e:  # never leave an item stuck in "running"
            store.finish_item(run_id, domain, None, note=f"error: {type(e).__name__}")
            return JSONResponse({"done": False, "domain": domain, "status": "failed",
                                 "reason": f"setup_error:{type(e).__name__}", "tier": None})
    finally:
        store.close()


# ---------- review ----------

@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, msg: str = ""):
    from .report import render_record
    from .review import order_queue

    store = get_store()
    try:
        claims = load_claims()
        queue = order_queue(store.review_queue())
        items = []
        for r in queue:
            d = next((x for x in r.drafts if x.variant == r.chosen_draft), None)
            items.append({"rec": r, "text": render_record(r, claims, public=True),
                          "subject": mask_people(d.subject, r) if d else "",
                          "body": mask_people(d.body, r) if d else ""})
        sent_rows = store.conn.execute(
            "SELECT record_json FROM decision_records WHERE status='sent' ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        sent = [DecisionRecord.model_validate_json(x["record_json"]) for x in sent_rows]
        return render(request, "review.html", items=items, sent=sent, msg=msg,
                      reasons=cfg().feedback.reason_codes)
    finally:
        store.close()


@app.post("/review/{record_id}")
def review_action(request: Request, record_id: str, action: str = Form(...), csrf: str = Form(""),
                  reason_code: str = Form(""), note: str = Form(""), subject: str = Form(""),
                  body: str = Form(""), hook_or_pain_changed: str = Form(""), replied: str = Form(""),
                  meeting: str = Form(""), bounced: str = Form("")):
    check_post(request, csrf)
    from .review import ReviewError, approve, edit, record_outcome, reject, verdict

    store = get_store()
    try:
        rec = store.get_record(record_id)
        if rec is None:
            raise HTTPException(404, "No such record.")
        s, c = load_settings(), cfg()
        try:
            if action == "approve":
                approve(rec, store, s)
                msg = f"Approved and sent {rec.domain} to MY_EMAIL (id {rec.send.message_id})."
            elif action == "edit":
                # the form showed placeholders instead of names: put the real ones back before QA and send
                edit(rec, store, s, c, new_body=unmask_people(body, rec), new_subject=unmask_people(subject, rec), reason_code=reason_code,
                     hook_or_pain_changed=bool(hook_or_pain_changed), note=note)
                msg = f"Edited and sent {rec.domain} (rewrite: {rec.review.rewrite})."
            elif action == "reject":
                reject(rec, store, c, reason_code=reason_code, note=note)
                msg = f"Rejected {rec.domain}: {reason_code}."
            elif action in ("qualify", "drop", "needs_more_info"):
                verdict(rec, store, c, decision=action, note=note, reason_code=reason_code or None)
                msg = f"Verdict for {rec.domain}: {action}."
                if action == "qualify":
                    from .pipeline import resume_after_qualify
                    from .review import can_resume
                    if can_resume(rec):
                        _, claims, _, llm, _, hunter = _pipeline_parts(store)
                        rec = resume_after_qualify(rec, cfg=c, claims=claims, store=store, llm=llm, hunter=hunter)
                        msg += f" Continued: {rec.status.value} ({rec.reason_code or 'draft ready'})."
                    else:
                        msg += f" It stopped at '{rec.reason_code}', so re-run it with a fresh run."
            elif action == "outcome":
                record_outcome(rec, store, replied=replied or None, meeting=meeting or None, bounced=bounced or None)
                msg = f"Outcome recorded for {rec.domain}."
            else:
                raise HTTPException(400, "Unknown action.")
        except ReviewError as e:
            msg = f"Not done for {rec.domain}: {e}"
        except HTTPException:
            raise
        except Exception as e:     # e.g. the send failed: the decision is saved, status stays drafted
            msg = f"Not sent for {rec.domain}: {type(e).__name__}. The decision is saved; try Approve again."
        from urllib.parse import quote
        return RedirectResponse(f"/review?msg={quote(msg)}", status_code=303)
    finally:
        store.close()
