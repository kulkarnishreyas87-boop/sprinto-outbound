"""S1 cleaning: main-content extraction, short-block and duplicate removal, token cap.

Output is plain text, one block per line, prefixed with the source URL tag so that
the model (and the quote verifier) always know where a block came from.
"""
from __future__ import annotations

import re
import unicodedata

import trafilatura
from bs4 import BeautifulSoup

_BOILERPLATE = re.compile(
    r"(cookie|we use cookies|accept all|privacy policy|terms of service|all rights reserved|©|subscribe to our newsletter)",
    re.I,
)
_WORDS_PER_TOKEN = 0.75  # rough English average


def _trafilatura_lines(html: str) -> list[str]:
    text = trafilatura.extract(html, include_comments=False, include_tables=True,
                               favor_recall=True, deduplicate=True) or ""
    return text.split("\n")


def _visible_lines(html: str) -> list[str]:
    """All visible text from <main> (or <body>), minus page chrome. Catches div/span-built sites."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg", "form", "aside",
                     "template", "iframe"]):
        tag.decompose()
    root = soup.find("main") or soup.body or soup
    return root.get_text("\n", strip=True).split("\n")


_SEP = " · "
_MAX_MERGED_WORDS = 40


def _blocks(lines: list[str], min_words: int) -> list[str]:
    """Drop boilerplate lines; merge runs of short lines (pricing rows, feature lists) into blocks
    of at least `min_words`; drop leftover fragments shorter than that."""
    out: list[str] = []
    buf: list[str] = []
    buf_words = 0

    def flush(force_drop: bool = False):
        nonlocal buf, buf_words
        if buf and buf_words >= min_words and not force_drop:
            out.append(_SEP.join(buf))
        buf, buf_words = [], 0

    for line in lines:
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"^(?:[-*•▪◦]|\d+[.)])\s+", "", line)   # list bullets added by extractors
        if not line or _BOILERPLATE.search(line):
            continue
        n = len(line.split())
        if n >= min_words:
            flush()
            out.append(line)
            continue
        buf.append(line)
        buf_words += n
        if buf_words >= _MAX_MERGED_WORDS:
            flush()
    flush()
    return out


def _assemble(blocks: list[str], budget_words: int) -> list[str]:
    seen: set[str] = set()
    out, used = [], 0
    for block in blocks:
        key = normalize(block)
        if key in seen:
            continue
        seen.add(key)
        n = len(block.split())
        if used + n > budget_words:
            break
        out.append(block)
        used += n
    return out


def clean_html(html: str, url: str, min_block_words: int = 8, max_tokens: int = 2000) -> str:
    budget = int(max_tokens * _WORDS_PER_TOKEN)
    best: list[str] = []
    for lines in (_trafilatura_lines(html), _visible_lines(html)):
        cand = _assemble(_blocks(lines, min_block_words), budget)
        if len(" ".join(cand).split()) > len(" ".join(best).split()):
            best = cand
    return "\n".join(best)


def word_count(text: str) -> int:
    return len(text.split())


def tag_for_prompt(url: str, cleaned: str) -> str:
    return f'<page url="{url}">\n{cleaned}\n</page>'


_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", " ": " "})


def normalize(text: str) -> str:
    """Normalisation shared by dedupe and quote verification."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_MAP)
    return re.sub(r"\s+", " ", text).strip().lower()
