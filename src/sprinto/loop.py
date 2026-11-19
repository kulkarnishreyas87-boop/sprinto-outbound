"""S4 Evidence loop (code decides; the model only extracts).

For each required field that is still unknown, fetch the highest-priority page for it
from the config page map, re-extract, merge, re-check the gates. Hard cap on extra
fetches. A required field is *resolved* when it is verified, or when every page the map
lists for it has been checked or does not exist on the site (missing is not negative).
At the cap with unresolved fields: needs_review / insufficient_evidence. Never "try harder".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .jobboards import Board, detect_board, read_board
from .models import ExtractedFields

JOBBOARD = "jobboard"


@dataclass
class LoopState:
    pages: dict[str, str]                       # url -> cleaned text (what quotes are checked against)
    discovered: dict[str, str]                  # page_type -> url
    tried: set[str] = field(default_factory=set)
    board: Board | None = None
    board_ok: bool = False


def _known(fields: ExtractedFields, name: str) -> bool:
    return getattr(fields, name).known


def candidates(field_name: str, st: LoopState, cfg: Config) -> list[str]:
    out: list[str] = []
    for page_type in cfg.loop.page_map.get(field_name, []):
        if page_type == "careers":
            if st.board is not None and JOBBOARD not in st.tried:
                out.append(JOBBOARD)
                continue
            if st.board_ok:
                continue   # the job board already answered what the careers page would
        if page_type in st.discovered and page_type not in st.tried:
            out.append(page_type)
    return out


def unresolved(fields: ExtractedFields, st: LoopState, cfg: Config) -> list[str]:
    return [f for f in cfg.extraction.required_fields if not _known(fields, f) and candidates(f, st, cfg)]


@dataclass
class LoopStep:
    page_type: str
    url: str
    status: str
    method: str
    words: int
    new_fields: ExtractedFields | None
    counted: bool            # counted against the cap (a request was made or served from cache)
    note: str = ""
    raw: str = ""            # raw HTML, for code-only detectors (hosted trust centres)


def next_target(fields: ExtractedFields, st: LoopState, cfg: Config) -> str | None:
    for f in unresolved(fields, st, cfg):   # required-field order from config = priority
        c = candidates(f, st, cfg)
        if c:
            return c[0]
    return None


def fetch_target(target: str, st: LoopState, *, fetcher, domain: str, cfg: Config) -> LoopStep:
    """Fetch one page (or the job board). Extraction is done by the caller."""
    st.tried.add(target)
    if target == JOBBOARD:
        res = read_board(fetcher, st.board, domain, cfg.jobboards)
        ok = res.page.status == "ok"
        st.board_ok = ok
        if ok:
            st.pages[res.page.url] = res.text
        return LoopStep(JOBBOARD, res.page.url, res.page.status, res.page.method, res.page.words,
                        res.fields if ok else None, counted=res.page.status != "robots_disallowed",
                        note=res.page.note)
    url = st.discovered[target]
    r = fetcher.fetch(url, target)
    if r.raw:
        # pages we fetched can point to pages the homepage didn't (e.g. /security -> trust.acme.io)
        for page_type, link in fetcher.discover_links(r.final_url or url, r.raw).items():
            st.discovered.setdefault(page_type, link)
        if st.board is None:
            st.board = detect_board(r.raw)   # careers pages often link the job board
    if r.page.status == "ok":
        st.pages[url] = r.cleaned
    return LoopStep(target, url, r.page.status, r.page.method, r.page.words, None,
                    counted=r.page.status != "robots_disallowed", note=r.page.note, raw=r.raw)
