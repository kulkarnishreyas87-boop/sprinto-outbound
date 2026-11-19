# sprinto-outbound

A research and drafting pipeline for Sprinto's Growth Lead (MESA Term 2, Build 2).

You give it a list of company domains, and for each one it:
1. scrapes the website;
2. extracts facts, and **code checks every quote** against the page;
3. qualifies the company against Sprinto's ICP, using buying moments and tiers;
4. finds the right person through Hunter;
5. writes two pain hypotheses;
6. drafts 3 email variants;
7. runs QA, and has a judge pick the best variant.

It then waits for a human review. Approved emails go **only to your own inbox** (`MY_EMAIL`).

Every decision is stored in a decision record and explained by `report`. Dropping, flagging and sending a company to review are normal outcomes, not errors.

## Setup (Windows, macOS or Linux, with uv)

```
uv sync --extra render                   # Python deps, plus Playwright for thin pages
uv run playwright install chromium
copy .env.example .env                   # then fill it in yourself; never commit it
uv run python -m sprinto check           # shows which settings are present (values are never printed)
```

`.env` needs four values:
- `MY_EMAIL`: your Resend sign-up address. It's the only address mail can ever go to.
- `GEMINI_API_KEY`
- `HUNTER_API_KEY`
- `RESEND_API_KEY`

Sprinto proof points live in `config/claims.yaml`. Only claims marked `approved: true` are used.

## Commands

| Command | What it does |
|---|---|
| `run domains.txt [--dry-run] [--force]` | Runs the whole pipeline on a list of domains, with no decisions needed mid-run. It never sends email. |
| `slice <domain> [--name X]` | Runs a single company. `--name` skips Hunter. |
| `report [run_id] [--full]` | Explains every company in a run: why it qualified or not, why this person, why this email. It also shows totals and cost per company. |
| `review [--no-send]` | Your review queue, sorted by tier and then by strength of evidence. For drafts: approve (sends), edit (opens `$EDITOR`) or reject. For flagged companies: qualify, drop or needs more info. |
| `send <record_id>` | Sends an approved draft to `MY_EMAIL`. |
| `outcome <record_id> --replied … --meeting … --bounced …` | Records what happened after a manual send. |
| `propose [--since 7d]` | Prints a weekly pattern report and writes a **proposed** `config/v{N+1}.yaml`. That file is never activated automatically. |
| `test-gold [--config N] [--freeze domains]` | Runs the gold-set regression test on frozen fixtures, with no network and no Hunter calls. |
| `promote vN` | Makes config version N active. Only a human runs this. Moving forward needs a passing gold result; rolling back is always allowed. |

## Live web app (Vercel)

The same pipeline runs as a FastAPI app (`src/sprinto/web.py`), deployed on Vercel with Neon Postgres.

- **Public, no login:** the list of runs, plus every company's decision, evidence, tier, hook, claim and cost. People's names, pain hypotheses, email bodies and review notes are hidden.
- **Operator, password needed:**
  - **New run:** up to 15 domains, one batch at a time. Each company runs in its own request, and the page updates as each one finishes.
  - **Review:** approve and send, edit and send, or reject, using the reason codes; give verdicts on review-queue items; record outcomes.
- **Guards:**
  - every operator action needs a signed session plus a CSRF token;
  - operator routes switch off unless both secrets are set;
  - Hunter stops at `contacts.hunter_monthly_cap` searches in a rolling 30 days;
  - email still goes only to `MY_EMAIL`.

Vercel environment variables:

| Variable | Notes |
|---|---|
| `DATABASE_URL` | Neon. Set automatically when Neon is added from Vercel → Storage. |
| `GEMINI_API_KEY`, `HUNTER_API_KEY`, `RESEND_API_KEY`, `MY_EMAIL` | Same as `.env`. |
| `OPERATOR_PASSWORD` | Password for the operator pages. |
| `SESSION_SECRET` | Random string of 32 or more characters, used to sign sessions. |
| `RUNS_DIR=/tmp/runs` | Optional. Where JSON copies of decision records are written. |

To run the app locally: `uv run uvicorn sprinto.web:app --port 8765`. It uses SQLite unless `DATABASE_URL` is set.

**Limits on Vercel:**
- No headless-browser fallback for thin pages; the CLI still has it.
- A batch advances only while its run page is open.
- `test-gold`, `propose` and `promote` stay in the CLI. A promoted config goes live by committing `config/ACTIVE` and redeploying.

## Pipeline

S0 input → S1 scrape → S2 extract, then the S2b support check → S3 hard gates → S4 evidence loop (at most 4 extra fetches) → S5 qualify → S6 contact → S7 pains → S8 variants → S9 QA and judge → S10 review → S11 send → S12 outcomes.

## Rules enforced in code

- **The only recipient is `MY_EMAIL`.** `send_email` has no to, cc or bcc parameter, and the email is checked twice before it goes to Resend (`src/sprinto/send.py`). Tests try the prospect's address, injected addresses, lists of recipients, cc and bcc.
- **Every fact needs evidence.** Each fact stores its value, a verbatim quote and the source URL. Code checks that the quote appears in the cleaned page text. A second cheap model pass rejects quotes that exist on the page but don't actually support the value. An unknown fact is never treated as a negative one.
- **The model proposes and code decides.** The gates, the loop cap, the tier rules (which can lower a tier but never raise it), contact ranking and QA are all plain code.
- **Hunter is only called for tier A or B companies** with an approved claim. Responses are cached per domain, and `--dry-run` never calls it.
- **Scraping is polite:**
  - robots.txt is read per host, following RFC 9309;
  - a clear User-Agent, 1–3 seconds between requests to a site, and a page cache;
  - one fetch per page, with no crawling and no logging in;
  - pages showing a bot challenge are recorded as `blocked` and never rendered in the browser.
- **The pipeline never edits its own config, prompts or examples.** New config versions go through `propose`, then `test-gold`, then `promote`.

## Changes from the brief

- **Resend replaces Gmail for sending.** It's a simpler setup, and its test mode only delivers to the account owner's address, which adds a second recipient lock.
- **Gemini is the LLM.** Model names live in config, and an Anthropic provider is also included.
- **Hosted trust centres** (for example Vanta's) name the tool only in asset URLs, not in visible text. A code-only detector reads the raw HTML for markers listed in config, and the report labels this evidence as raw-HTML.
- **A required field is resolved** when it's either verified, or every page the page map lists for it has been checked. This stops "no SOC 2 badge found" from sending every company to review.

## Tests

```
uv run pytest
```

LLM, Hunter, Resend and network calls are faked in the tests. Live checks were run separately and are recorded in the decision records.

## Data not in this repo

- **`data/gold_fixtures/*/pages.json`** holds copies of other companies' web pages. To rebuild: research the domain once, then run `test-gold --freeze <domain>`.
- **Also excluded:** `.env`, `data/pipeline.db`, `runs/` and `proposals/`.
