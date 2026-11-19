"""Job boards: detect Greenhouse / Lever / Ashby / Workable and read their PUBLIC postings APIs.

Postings become a small synthetic text page (stored in the page cache), and code sets
`open_roles_total` and `compliance_security_roles` with quotes taken from that text, so
the same quote verification and reporting apply as for scraped pages.
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from .clean import normalize
from .config import JobBoardsCfg
from .models import Evidence, ExtractedFields, PageFetch, Role

_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
]
_BAD_TOKENS = {"embed", "api", "j", "jobs", "js", "v1"}


@dataclass(frozen=True)
class Board:
    kind: str
    token: str
    eu: bool = False

    @property
    def api_url(self) -> str:
        if self.kind == "greenhouse":
            return f"https://boards-api.greenhouse.io/v1/boards/{self.token}/jobs?content=true"
        if self.kind == "lever":
            return f"https://api.{'eu.' if self.eu else ''}lever.co/v0/postings/{self.token}?mode=json"
        if self.kind == "ashby":
            return f"https://api.ashbyhq.com/posting-api/job-board/{self.token}?includeCompensation=false"
        return f"https://www.workable.com/api/accounts/{self.token}?details=true"

    @property
    def label(self) -> str:
        return f"{self.kind.capitalize()} job board '{self.token}'"


def detect_board(raw_html: str) -> Board | None:
    for kind, pat in _PATTERNS:
        for m in pat.finditer(raw_html or ""):
            token = m.group(1).strip("/.")
            if token.lower() in _BAD_TOKENS:
                continue
            return Board(kind, token, eu=(kind == "lever" and ".eu." in m.group(0).lower()))
    return None


@dataclass
class Posting:
    title: str
    department: str | None
    location: str | None
    posted: str | None          # ISO date (YYYY-MM-DD) or None
    url: str | None
    description: str


def _text(html: str | None) -> str:
    if not html:
        return ""
    # Greenhouse content is entity-encoded HTML; unescape until stable, then strip tags
    prev, cur = None, html
    while prev != cur:
        prev, cur = cur, htmllib.unescape(cur)
    return " ".join(BeautifulSoup(cur, "html.parser").get_text(" ").split())


def _date(value) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # Lever createdAt is epoch milliseconds when present
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(value))
    return m.group(0) if m else None


def parse_postings(board: Board, data) -> list[Posting]:
    out: list[Posting] = []
    if board.kind == "greenhouse":
        for j in (data or {}).get("jobs", []):
            depts = ", ".join(d.get("name", "") for d in j.get("departments") or [] if d.get("name"))
            out.append(Posting(j.get("title", ""), depts or None, (j.get("location") or {}).get("name"),
                               _date(j.get("first_published") or j.get("updated_at")), j.get("absolute_url"),
                               _text(j.get("content"))))
    elif board.kind == "lever":
        for j in data or []:
            c = j.get("categories") or {}
            out.append(Posting(j.get("text", ""), c.get("department") or c.get("team"), c.get("location"),
                               _date(j.get("createdAt")), j.get("hostedUrl"),
                               " ".join(filter(None, [j.get("descriptionPlain"), j.get("additionalPlain")]))))
    elif board.kind == "ashby":
        for j in (data or {}).get("jobs", []):
            if j.get("isListed") is False:
                continue
            out.append(Posting(j.get("title", ""), j.get("department") or j.get("team"), j.get("location"),
                               _date(j.get("publishedAt")), j.get("jobUrl"), j.get("descriptionPlain") or ""))
    elif board.kind == "workable":
        for j in (data or {}).get("jobs", []):
            loc = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
            out.append(Posting(j.get("title", ""), j.get("department"), loc or None,
                               _date(j.get("published_on") or j.get("created_at")),
                               j.get("url") or j.get("shortlink"), _text(j.get("description"))))
    return [p for p in out if p.title]


def _kw_match(text: str, keywords: list[str]) -> str | None:
    t = normalize(text)
    for kw in keywords:
        if re.search(rf"(?<![a-z0-9]){re.escape(normalize(kw))}(?![a-z0-9])", t):
            return kw
    return None


def _snippet(text: str, keyword: str, width: int = 160) -> str:
    t = " ".join(text.split())
    i = normalize(t).find(normalize(keyword))
    if i < 0:
        return ""
    # the sentence around the keyword, clipped to `width` chars either side
    prev_stop = t.rfind(". ", 0, i)
    start = max(prev_stop + 2 if prev_stop >= 0 else 0, i - width)
    next_stop = t.find(". ", i)
    end = min(next_stop + 1 if next_stop >= 0 else len(t), i + width)
    return t[start:end].strip()


@dataclass
class BoardResult:
    page: PageFetch
    text: str
    fields: ExtractedFields
    postings: list[Posting]


def role_line(p: Posting) -> str:
    return (f"Open role: {p.title} | {p.department or '-'} | {p.location or '-'} | "
            f"posted {p.posted or 'date not shown'} | {p.url or '-'}")


def board_to_fields(board: Board, postings: list[Posting], cfg: JobBoardsCfg) -> tuple[str, ExtractedFields]:
    api = board.api_url
    header = f"{len(postings)} open roles listed on the {board.label}."
    lines = [header] + [role_line(p) for p in postings[: cfg.max_postings_in_text]]
    # roles whose TITLE is about security/compliance rank before roles that only mention it in the description
    by_title, by_desc = [], []
    for p in postings:
        if _kw_match(p.title, cfg.title_keywords):
            by_title.append((p, None))
        else:
            dk = _kw_match(p.description, cfg.description_keywords)
            if dk:
                by_desc.append((p, _snippet(p.description, dk)))
    roles, first_quote = [], None
    for p, mention in by_title + by_desc:
        roles.append(Role(title=p.title, posted_date=p.posted, url=p.url))
        line = role_line(p)
        if line not in lines:
            lines.append(line)
        if mention:
            mline = f'Open role {p.title} description mentions: "{mention}"'
            lines.append(mline)
            first_quote = first_quote or mline
        first_quote = first_quote or line
    text = "\n".join(lines)
    fields = ExtractedFields(
        open_roles_total=Evidence(value=len(postings), evidence_quote=header, source_url=api, verified=True,
                                  kind="api"))
    if roles:
        fields.compliance_security_roles = Evidence(value=roles, evidence_quote=first_quote, source_url=api,
                                                    verified=True, kind="api")
    return text, fields


def read_board(fetcher, board: Board, company_domain: str, cfg: JobBoardsCfg) -> BoardResult:
    api = board.api_url
    cached = fetcher.store.get_page(api) if fetcher.use_cache else None
    if cached is not None and cached["raw"]:      # postings are re-parsed from the stored JSON
        import json
        postings = parse_postings(board, json.loads(cached["raw"]))
        text, fields = board_to_fields(board, postings, cfg)
        return BoardResult(PageFetch(url=api, page_type="jobboard", method="cache", words=len(text.split())),
                           text, fields, postings)
    data, status = fetcher.fetch_json(api)
    if data is None:
        return BoardResult(PageFetch(url=api, page_type="jobboard", method="jobboard", status=status),
                           "", ExtractedFields(), [])
    postings = parse_postings(board, data)
    text, fields = board_to_fields(board, postings, cfg)
    import json
    fetcher.store.put_page(api, company_domain, "jobboard", "jobboard", 200, json.dumps(data), text)
    return BoardResult(PageFetch(url=api, page_type="jobboard", method="jobboard", words=len(text.split()),
                                 note=board.label), text, fields, postings)
