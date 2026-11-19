"""Live web app (Vercel): public read-only reports; password-protected run + review.

Everything the pipeline decides is still decided by the same code as the CLI: this module only
wires HTTP to `pipeline.run_slice`, `report.render_*` and `review.*`.

Security:
- operator routes need a signed session (OPERATOR_PASSWORD + SESSION_SECRET env vars; if either is
  missing, operator routes are disabled), and every POST needs a CSRF token tied to that session;
- the public view never shows people's names, pains, email bodies or review notes;
- runs are capped (15 domains, one active batch); Hunter has a monthly cap in config.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import load_claims, load_config, load_settings
from .models import Status
from .store import Store

MAX_DOMAINS = 15
SESSION_COOKIE = "sprinto_session"
SESSION_HOURS = 12
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Sprinto Outbound", docs_url=None, redoc_url=None, openapi_url=None)


# ---------- helpers ----------

class NotConfigured(Exception):
    pass


REQUIRED_ENV = ["DATABASE_URL", "GEMINI_API_KEY", "HUNTER_API_KEY", "RESEND_API_KEY", "MY_EMAIL",
                "OPERATOR_PASSWORD", "SESSION_SECRET"]


def get_store() -> Store:
    # On Vercel the bundle is read-only: without Postgres there is nowhere to keep data.
    if os.environ.get("VERCEL") and not os.environ.get("DATABASE_URL"):
        raise NotConfigured()
    return Store()


@app.exception_handler(NotConfigured)
def not_configured(request: Request, exc: NotConfigured):
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]     # names only, never values
    return TEMPLATES.TemplateResponse(request, "setup.html", {
        "request": request, "missing": missing, "operator": False, "operator_enabled": False, "csrf": ""},
        status_code=503)


@lru_cache(maxsize=1)
def cfg():
    return load_config()


def _secret() -> bytes:
    return os.environ.get("SESSION_SECRET", "").encode()


def operator_enabled() -> bool:
    return bool(os.environ.get("OPERATOR_PASSWORD")) and len(_secret()) >= 16


def _sign(value: str) -> str:
    return hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    payload = f"{int(time.time()) + SESSION_HOURS * 3600}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def session_valid(token: str | None) -> bool:
    if not token or not operator_enabled():
        return False
    try:
        exp, nonce, sig = token.split(".")
    except ValueError:
        return False
    return hmac.compare_digest(sig, _sign(f"{exp}.{nonce}")) and int(exp) > time.time()


def csrf_token(request: Request) -> str:
    return _sign("csrf:" + (request.cookies.get(SESSION_COOKIE) or ""))


def is_operator(request: Request) -> bool:
    return session_valid(request.cookies.get(SESSION_COOKIE))


def require_operator(request: Request, csrf: str | None = None, check_csrf: bool = False) -> None:
    if not operator_enabled():
        raise HTTPException(503, "Operator access is not configured on this deployment.")
    if not is_operator(request):
        raise HTTPException(401, "Log in first.")
    if check_csrf:
        sent = csrf or request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, csrf_token(request)):
            raise HTTPException(403, "Bad or missing CSRF token. Reload the page and try again.")


def render(request: Request, name: str, **ctx):
    ctx.update(request=request, operator=is_operator(request), operator_enabled=operator_enabled(),
               csrf=csrf_token(request) if is_operator(request) else "")
    return TEMPLATES.TemplateResponse(request, name, ctx)


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    store = get_store()
    try:
        runs = []
        for r in store.recent_runs(30):
            recs = store.records_for_run(r["run_id"])
            runs.append({"run_id": r["run_id"], "version": r["config_version"], "started": str(r["started_at"])[:16],
                         "n": len(recs), "counts": Counter(x.status.value for x in recs),
                         "usd": sum(x.cost.usd_estimate for x in recs)})
        status = {
            "database": "Postgres" if store.is_postgres else "SQLite (local)",
            "operator": operator_enabled(),
            "runs": len(runs),
            "companies": sum(r["n"] for r in runs),
            "drafted": sum(r["counts"].get("drafted", 0) + r["counts"].get("sent", 0) for r in runs),
            "config": cfg().version,
        }
        return render(request, "index.html", runs=runs, active=store.active_run_id(), status=status)
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
        public = not is_operator(request)
        order = {Status.sent: 0, Status.drafted: 1, Status.needs_review: 2, Status.dropped: 3, Status.failed: 4}
        recs = sorted(store.records_for_run(run_id), key=lambda r: (order.get(r.status, 9), r.domain))
        claims = load_claims()
        blocks = [{"domain": r.domain, "status": r.status.value, "reason": r.reason_code, "tier": r.tier,
                   "text": render_record(r, claims, full=not public, public=public)} for r in recs]
        return render(request, "report.html", run_id=run_id, version=run["config_version"], blocks=blocks,
                      totals=render_totals(recs), public=public, items=store.run_items(run_id))
    finally:
        store.close()


# ---------- auth ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if not operator_enabled():
        raise HTTPException(503, "Operator access is not configured on this deployment.")
    time.sleep(0.5)    # slows down password guessing
    if not hmac.compare_digest(password.encode(), os.environ["OPERATOR_PASSWORD"].encode()):
        return render(request, "login.html", error="Wrong password.")
    resp = RedirectResponse("/run", status_code=303)
    resp.set_cookie(SESSION_COOKIE, make_session(), max_age=SESSION_HOURS * 3600, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https")
    return resp


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    require_operator(request, csrf, check_csrf=True)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------- runs (operator) ----------

@app.get("/run", response_class=HTMLResponse)
def run_page(request: Request):
    require_operator(request)
    store = get_store()
    try:
        c = cfg()
        return render(request, "run.html", active=store.active_run_id(), max_domains=MAX_DOMAINS,
                      hunter_used=store.hunter_calls_since(30), hunter_cap=c.contacts.hunter_monthly_cap,
                      approved_claims=len(load_claims().approved()), error=None)
    finally:
        store.close()


@app.post("/runs")
def start_run(request: Request, domains: str = Form(...), force: str = Form(""), csrf: str = Form("")):
    require_operator(request, csrf, check_csrf=True)
    from .pipeline import read_domains_text

    store = get_store()
    try:
        active = store.active_run_id()
        if active:
            return RedirectResponse(f"/runs/{active}/live", status_code=303)
        ds = read_domains_text(domains)
        c = cfg()
        if not ds or len(ds) > MAX_DOMAINS:
            return render(request, "run.html", active=None, max_domains=MAX_DOMAINS,
                          hunter_used=store.hunter_calls_since(30), hunter_cap=c.contacts.hunter_monthly_cap,
                          approved_claims=len(load_claims().approved()),
                          error=f"Give between 1 and {MAX_DOMAINS} valid domains (you gave {len(ds)}).")
        skipped = [] if force else [d for d in ds if store.researched_recently(d, c.input.recheck_after_days)]
        todo = [d for d in ds if d not in skipped]
        run_id = store.create_run(c.version, todo)
        store.add_run_items(run_id, todo, skipped)
        if not todo:
            store.finish_run(run_id)
        return RedirectResponse(f"/runs/{run_id}/live", status_code=303)
    finally:
        store.close()


@app.get("/runs/{run_id}/live", response_class=HTMLResponse)
def run_live(request: Request, run_id: str):
    require_operator(request)
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
    require_operator(request, check_csrf=True)
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


# ---------- review (operator) ----------

@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, msg: str = ""):
    require_operator(request)
    from .report import render_record
    from .review import order_queue

    store = get_store()
    try:
        claims = load_claims()
        queue = order_queue(store.review_queue())
        items = []
        for r in queue:
            d = next((x for x in r.drafts if x.variant == r.chosen_draft), None)
            items.append({"rec": r, "text": render_record(r, claims, full=True),
                          "subject": d.subject if d else "", "body": d.body if d else ""})
        sent_rows = store.conn.execute(
            "SELECT record_json FROM decision_records WHERE status='sent' ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()
        from .models import DecisionRecord
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
    require_operator(request, csrf, check_csrf=True)
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
                edit(rec, store, s, c, new_body=body, new_subject=subject, reason_code=reason_code,
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
