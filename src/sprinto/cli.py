"""Command-line interface. Milestone 1: stubs plus config/env checks."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .config import active_version, load_claims, load_config, load_settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Sprinto outbound research and drafting pipeline")


def _todo(milestone: int) -> None:
    typer.secho(f"Not built yet (milestone {milestone}).", fg=typer.colors.YELLOW)
    raise typer.Exit(code=2)


@app.command()
def check(config: Optional[int] = typer.Option(None, help="Config version to validate (default: active)")) -> None:
    """Validate config, claims and which .env values are present (values are never printed)."""
    cfg = load_config(config)
    claims = load_claims()
    s = load_settings()
    typer.echo(f"config v{cfg.version} OK (active: v{active_version()})")
    typer.echo(f"claims: {len(claims.claims)} total, {len(claims.approved())} approved")
    typer.echo(f"LLM provider: {cfg.models.provider}")
    llm_key = ("GEMINI_API_KEY", s.gemini_api_key) if cfg.models.provider == "gemini" \
        else ("ANTHROPIC_API_KEY", s.anthropic_api_key)
    for name, val in [("MY_EMAIL", s.my_email), llm_key,
                      ("HUNTER_API_KEY", s.hunter_api_key), ("RESEND_API_KEY", s.resend_api_key)]:
        typer.echo(f"{name}: {'set' if val else 'MISSING'}")


@app.command("slice")
def slice_cmd(domain: str,
              name: Optional[str] = typer.Option(None, help="Override: skip Hunter and address this person"),
              title: Optional[str] = typer.Option(None, help="Title for --name"),
              refresh: bool = typer.Option(False, "--refresh", help="Re-fetch pages instead of using the cache")) -> None:
    """Thin slice: scrape homepage, extract with evidence, gates, Hunter contact, draft. Does NOT send."""
    from .contacts import HunterClient
    from .fetch import Fetcher
    from .llm import LLMError, make_provider
    from .pipeline import run_slice
    from .store import Store

    cfg, claims, s = load_config(), load_claims(), load_settings()
    store = Store()
    try:
        llm = make_provider(cfg, store, s)
    except LLMError as e:
        typer.secho(f"Cannot start: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    run_id = store.create_run(cfg.version, [domain])
    rec = run_slice(domain, cfg=cfg, claims=claims, store=store,
                    fetcher=Fetcher(cfg.scrape, store, use_cache=not refresh),
                    llm=llm, run_id=run_id, contact_name=name, contact_title=title,
                    hunter=HunterClient(s.hunter_api_key, store, cfg.contacts))
    store.finish_run(run_id)
    from .report import render_record

    typer.echo(f"run {run_id}\n")
    typer.echo(render_record(rec, claims, full=True))
    if rec.drafts:
        typer.echo(f"\nTo send to MY_EMAIL after reviewing: uv run python -m sprinto send {rec.record_id}")


@app.command()
def send(record_id: str, yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt")) -> None:
    """Approve (if not already) and send a drafted record to MY_EMAIL, the only possible recipient."""
    from .models import Status
    from .pipeline import export_record
    from .review import ReviewError, approve, chosen, send_approved
    from .store import Store

    store = Store()
    rec = store.get_record(record_id)
    if rec is None:
        typer.secho("No such record.", fg=typer.colors.RED)
        raise typer.Exit(1)
    if rec.status != Status.drafted or rec.chosen_draft is None:
        typer.secho(f"Record is {rec.status.value}; only drafted records can be sent.", fg=typer.colors.RED)
        raise typer.Exit(1)
    d = chosen(rec)
    typer.echo(f"Subject: {rec.review.edited_subject or d.subject}\n\n{rec.review.edited_text or d.body}\n")
    if not yes and not typer.confirm("Approve and send this to MY_EMAIL?"):
        raise typer.Exit(0)
    try:
        if rec.review.decision in ("approve", "edit"):     # approved earlier; a previous send failed
            send_approved(rec, store, load_settings())
        else:
            approve(rec, store, load_settings())
    except ReviewError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    except Exception as e:   # includes RecipientLockError
        typer.secho(f"Not sent: {type(e).__name__}: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    export_record(rec)
    typer.echo(f"Sent to MY_EMAIL. message id {rec.send.message_id}")


@app.command()
def run(domains_file: Path,
        force: bool = typer.Option(False, "--force", help="Re-research domains done in the last 30 days"),
        dry_run: bool = typer.Option(False, "--dry-run", help="No new Hunter calls (cache only); never sends"),
        refresh: bool = typer.Option(False, "--refresh", help="Re-fetch pages instead of using the cache")) -> None:
    """Run the whole pipeline on a file of domains. Never sends: approve drafts with `send`."""
    from .contacts import HunterClient
    from .fetch import Fetcher
    from .llm import LLMError, make_provider
    from .pipeline import read_domains, run_batch
    from .report import render_totals
    from .store import Store

    cfg, claims, s = load_config(), load_claims(), load_settings()
    domains = read_domains(domains_file)
    if not domains:
        typer.secho("No domains found.", fg=typer.colors.RED)
        raise typer.Exit(1)
    store = Store()
    try:
        llm = make_provider(cfg, store, s)
    except LLMError as e:
        typer.secho(f"Cannot start: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    if not claims.approved():
        typer.secho("Note: no approved claims, so no drafts will be written and Hunter won't be called.",
                    fg=typer.colors.YELLOW)

    def show(rec):
        extra = f" tier {rec.tier}" if rec.tier else ""
        typer.echo(f"  {rec.domain:<28} {rec.status.value:<13}{extra}  {rec.reason_code or ''}")

    typer.echo(f"Config v{cfg.version} | {len(domains)} domain(s){' | DRY RUN' if dry_run else ''}")
    run_id, records, skipped = run_batch(
        domains, cfg=cfg, claims=claims, store=store,
        fetcher=Fetcher(cfg.scrape, store, use_cache=not refresh),
        llm=llm, hunter=HunterClient(s.hunter_api_key, store, cfg.contacts, cache_only=dry_run),
        force=force, dry_run=dry_run, on_record=show)
    for d in skipped:
        typer.echo(f"  {d:<28} skipped        researched in the last {cfg.input.recheck_after_days} days (--force to redo)")
    typer.echo("")
    typer.echo(render_totals(records))
    typer.echo(f"\nRun {run_id}. Explain every decision: uv run python -m sprinto report {run_id}")


def _pick_reason(codes: list[str], allow_none: bool = False) -> str | None:
    for i, c in enumerate(codes, 1):
        typer.echo(f"  {i}. {c}")
    while True:
        raw = typer.prompt("Reason code" + (" (Enter to skip)" if allow_none else ""),
                           default="" if allow_none else None, show_default=False).strip()
        if allow_none and not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(codes):
            return codes[int(raw) - 1]
        if raw in codes:
            return raw
        typer.secho("Pick a number or a code from the list.", fg=typer.colors.YELLOW)


@app.command()
def review(no_send: bool = typer.Option(False, "--no-send", help="Record approvals but don't send yet"),
           limit: int = typer.Option(0, help="Stop after this many items (0 = all)")) -> None:
    """Work through drafts and review-queue items: tier first, strongest evidence first."""
    from .contacts import HunterClient
    from .llm import LLMError, make_provider
    from .models import Status
    from .pipeline import export_record, resume_after_qualify
    from .report import render_record
    from .review import (ReviewError, approve, can_resume, chosen, edit, edit_in_editor, edit_ratio,
                         order_queue, reject, verdict)
    from .store import Store

    cfg, claims, s = load_config(), load_claims(), load_settings()
    store = Store()
    queue = order_queue(store.review_queue())
    if limit:
        queue = queue[:limit]
    if not queue:
        typer.echo("Nothing to review.")
        return
    codes = cfg.feedback.reason_codes
    typer.echo(f"{len(queue)} item(s) to review.\n")
    for n, rec in enumerate(queue, 1):
        typer.echo(f"\n[{n}/{len(queue)}] " + render_record(rec, claims, full=True))
        try:
            if rec.status == Status.drafted:
                choice = typer.prompt("\n[a]pprove & send, [e]dit, [r]eject, [s]kip, [x] quit", default="s").lower()
                if choice == "x":
                    break
                if choice == "a":
                    approve(rec, store, s, send=not no_send)
                    typer.secho("Approved" + ("" if no_send else f" and sent, id {rec.send.message_id}"),
                                fg=typer.colors.GREEN)
                elif choice == "e":
                    d = chosen(rec)
                    subj, body = edit_in_editor(d.subject, d.body)
                    typer.echo(f"Edit ratio {edit_ratio(d.body, body):.2f}")
                    reason = _pick_reason(codes)
                    changed = typer.confirm("Did you change the hook or the pain?", default=False)
                    note = typer.prompt("Note (optional)", default="", show_default=False)
                    send_now = not no_send and typer.confirm("Send the edited version to MY_EMAIL now?", default=True)
                    edit(rec, store, s, cfg, new_body=body, new_subject=subj, reason_code=reason,
                         hook_or_pain_changed=changed, note=note, send=send_now)
                    typer.secho(f"Edited (rewrite: {rec.review.rewrite})" +
                                (f", sent, id {rec.send.message_id}" if rec.status == Status.sent else ""),
                                fg=typer.colors.GREEN)
                elif choice == "r":
                    reason = _pick_reason(codes)
                    note = typer.prompt("Note (optional)", default="", show_default=False)
                    reject(rec, store, cfg, reason_code=reason, note=note)
                    typer.secho(f"Rejected: {reason}", fg=typer.colors.YELLOW)
            elif rec.status == Status.needs_review:
                choice = typer.prompt("\n[q]ualify, [d]rop, [n]eeds more info, [s]kip, [x] quit", default="s").lower()
                if choice == "x":
                    break
                if choice in ("q", "d", "n"):
                    decision = {"q": "qualify", "d": "drop", "n": "needs_more_info"}[choice]
                    reason = _pick_reason(codes, allow_none=True) if decision == "drop" else None
                    note = typer.prompt("Note (optional)", default="", show_default=False)
                    verdict(rec, store, cfg, decision=decision, note=note, reason_code=reason)
                    typer.secho(f"Verdict: {decision}", fg=typer.colors.GREEN)
                    if decision == "qualify":
                        if can_resume(rec) and typer.confirm("Continue to contact lookup and drafting now?",
                                                             default=True):
                            llm = make_provider(cfg, store, s)
                            rec = resume_after_qualify(rec, cfg=cfg, claims=claims, store=store, llm=llm,
                                                       hunter=HunterClient(s.hunter_api_key, store, cfg.contacts))
                            typer.echo(f"-> {rec.status.value} ({rec.reason_code or 'draft ready'}); "
                                       "run `review` again to see it.")
                        elif not can_resume(rec):
                            typer.echo(f"Recorded. This item stopped at '{rec.reason_code}', so it can't continue "
                                       "automatically: fix the input and re-run with `run --force`.")
            export_record(rec)
        except (ReviewError, LLMError) as e:
            typer.secho(f"Not done: {e}", fg=typer.colors.RED)
        except Exception as e:   # e.g. send failed: the decision is saved, status stays drafted
            typer.secho(f"Not sent: {type(e).__name__}: {e}. The approval is saved; retry with "
                        f"`send {rec.record_id}`.", fg=typer.colors.RED)
    typer.echo("\nDone.")


@app.command()
def report(run_id: Optional[str] = typer.Argument(None, help="Run id (default: latest run)"),
           full: bool = typer.Option(False, "--full", help="Untruncated quotes and email bodies"),
           list_runs: bool = typer.Option(False, "--list", help="List recent runs instead")) -> None:
    """Explain every decision in a run: why each company qualified or not, why this person, why this email."""
    from .report import render_run
    from .store import Store

    store = Store()
    if list_runs:
        for r in store.recent_runs():
            typer.echo(f"{r['run_id']}  v{r['config_version']}  {r['started_at'][:19]}  {r['n']} companies")
        return
    run_id = run_id or store.latest_run_id()
    run = store.get_run(run_id) if run_id else None
    if run is None:
        typer.secho("No such run. Use `report --list`.", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        claims = load_claims()
    except Exception:
        claims = None
    typer.echo(render_run(run_id, run["config_version"], store.records_for_run(run_id), claims, full))


@app.command()
def outcome(record_id: str,
            replied: Optional[str] = typer.Option(None, help="positive|neutral|negative"),
            meeting: Optional[str] = typer.Option(None, help="yes|no"),
            bounced: Optional[str] = typer.Option(None, help="yes|no")) -> None:
    """Record a manual outcome for a sent email."""
    from .pipeline import export_record
    from .review import ReviewError, record_outcome
    from .store import Store

    store = Store()
    rec = store.get_record(record_id)
    if rec is None:
        typer.secho("No such record.", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        record_outcome(rec, store, replied=replied, meeting=meeting, bounced=bounced)
    except ReviewError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    export_record(rec)
    o = rec.outcome
    typer.echo(f"Outcome for {rec.domain}: replied={o.replied} meeting={o.meeting} bounced={o.bounced}")


@app.command()
def propose(since: str = typer.Option("7d", help="Window, e.g. 7d or 24h"),
            dry: bool = typer.Option(False, "--dry", help="Show the report and diff without writing files")) -> None:
    """Weekly pattern report, then PROPOSED changes as config/v{N+1}.yaml (never activated here)."""
    from .feedback import build_report, make_proposal, parse_since
    from .store import Store

    try:
        since_dt = parse_since(since)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(1)
    cfg, store = load_config(), Store()
    report = build_report(store, since_dt)
    typer.echo(report.render())
    p = make_proposal(report, store, cfg, write=not dry)
    typer.echo("\n━━ Proposals")
    if p.flags:
        for f in p.flags:
            typer.secho(f"FLAG: {f}", fg=typer.colors.YELLOW)
    if not p.changes:
        typer.echo(f"No config changes proposed (each needs >= {cfg.feedback.min_occurrences_for_proposal} "
                   "occurrences of the same reason).")
        return
    for c in p.changes:
        typer.echo(f"- {c}")
    if p.diff:
        typer.echo("\n" + p.diff)
    if not dry and p.config_text:
        typer.echo(f"Wrote config/v{p.version}.yaml (NOT active). Next: "
                   f"`test-gold --config {p.version}`, then `promote v{p.version}` if it passes.")


@app.command("test-gold")
def test_gold(config: Optional[int] = typer.Option(None, help="Config version to test (default: active)"),
              freeze: Optional[str] = typer.Option(None, help="Snapshot fixtures for these domains (comma-separated)"
                                                    " from the current page/Hunter cache, then stop")) -> None:
    """Gold-set regression test on frozen fixtures (no network, no Hunter calls, no sends)."""
    from .goldset import compare, freeze as freeze_domain, latest_result, load_cases, run_gold, save_result
    from .store import Store

    store = Store()
    if freeze:
        for d in [x.strip() for x in freeze.split(",") if x.strip()]:
            try:
                typer.echo(f"froze {d} -> {freeze_domain(d, store)}")
            except ValueError as e:
                typer.secho(str(e), fg=typer.colors.RED)
        return
    cases = load_cases()
    if not cases:
        typer.secho("data/gold_set.yaml has no labelled cases yet (replace the TODO domains).", fg=typer.colors.RED)
        raise typer.Exit(1)
    from .goldset import FIXTURES
    missing = [c["domain"] for c in cases if not (FIXTURES / c["domain"] / "pages.json").exists()]
    if missing:
        # never record a result (or a baseline) from an incomplete gold set
        typer.secho("Missing fixtures for: " + ", ".join(missing) + ". Research each domain once (`slice` or "
                    "`run`), then `test-gold --freeze <domains>`. Nothing was run or saved.", fg=typer.colors.RED)
        raise typer.Exit(1)
    s, claims = load_settings(), load_claims()
    if not claims.approved():
        typer.secho("Note: no approved claims, so cases expecting a draft will fail at S6.", fg=typer.colors.YELLOW)
    active = active_version()
    version = config if config is not None else active

    def run_one(v: int):
        typer.echo(f"Running gold set ({len(cases)} cases) with config v{v}…")
        results, m = run_gold(v, settings=s, claims=claims, cases=cases)
        for r in results:
            mark = "PASS" if r.ok else "FAIL"
            typer.echo(f"  {mark} {r.id:<28} {r.domain:<24} {r.actual.get('status', '-')}"
                       + (f"  ({'; '.join(r.problems)})" if r.problems else ""))
        typer.echo("  metrics: " + ", ".join(f"{k}={v_}" for k, v_ in m.items()))
        return results, m

    baseline = None
    if version != active:
        prev = latest_result(store, active)
        if prev is None:
            res_a, m_a = run_one(active)
            save_result(store, active, res_a, m_a, True, None)
            baseline = m_a
        else:
            baseline = prev["metrics"]
            typer.echo(f"Baseline: active v{active} result from {prev['at'][:19]}: {baseline}")
    results, m = run_one(version)
    passed, notes = compare(m, baseline)
    save_result(store, version, results, m, passed, active if version != active else None)
    for n in notes:
        typer.echo("  " + n)
    typer.secho(f"v{version}: {'PASS' if passed else 'FAIL'}" +
                (f" vs active v{active}" if version != active else " (baseline for the active version)"),
                fg=typer.colors.GREEN if passed else typer.colors.RED)
    if version != active and passed:
        typer.echo(f"You can now run: promote v{version}")


@app.command()
def promote(version: str) -> None:
    """Make a config version active. Human command only; forward needs a passing gold result."""
    from .goldset import PromotionError, promote as do_promote
    from .store import Store

    try:
        v = int(version.lower().lstrip("v"))
        typer.secho(do_promote(v, Store()), fg=typer.colors.GREEN)
    except (ValueError, FileNotFoundError, PromotionError) as e:
        typer.secho(f"Not promoted: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
