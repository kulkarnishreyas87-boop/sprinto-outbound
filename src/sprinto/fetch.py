"""S1 fetching: robots.txt, polite single fetches, page cache.

Never crawls, never logs in, never tries to get around bot protection. If blocked, it
returns a typed failure and the pipeline moves on.
"""
from __future__ import annotations

import random
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from .clean import clean_html, word_count
from .config import ScrapeCfg
from .models import PageFetch
from .store import Store


def normalize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    if not raw or raw.startswith("#"):
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    host = urlparse(raw).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host.rstrip(".")


def playwright_render(url: str, user_agent: str, timeout_s: float) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, wait_until="load", timeout=int(timeout_s * 1000))
            page.wait_for_timeout(1500)   # let client-side rendering settle; no clicks, no scrolling
            return page.content()
        finally:
            browser.close()


def default_renderer() -> Callable[[str, str, float], str] | None:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return None
    return playwright_render


@dataclass
class FetchResult:
    page: PageFetch
    cleaned: str = ""
    raw: str = ""
    final_url: str = ""


@dataclass
class Fetcher:
    cfg: ScrapeCfg
    store: Store
    client: httpx.Client | None = None
    sleep: callable = time.sleep
    renderer: Callable[[str, str, float], str] | None = field(default_factory=lambda: default_renderer())
    use_cache: bool = True   # False = re-fetch pages (cache is still written)
    _robots: dict[str, urllib.robotparser.RobotFileParser] = field(default_factory=dict)
    _last_hit: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.Client(
                headers={"User-Agent": self.cfg.user_agent, "Accept": "text/html,application/xhtml+xml"},
                timeout=self.cfg.timeout_seconds,
                follow_redirects=True,
            )

    # ----- politeness -----
    def _wait(self, domain: str) -> None:
        lo, hi = self.cfg.delay_seconds
        last = self._last_hit.get(domain)
        if last is not None:
            gap = random.uniform(lo, hi) - (time.monotonic() - last)
            if gap > 0:
                self.sleep(gap)
        self._last_hit[domain] = time.monotonic()

    def _robots_for(self, domain: str) -> urllib.robotparser.RobotFileParser:
        if domain in self._robots:
            return self._robots[domain]
        # RFC 9309 section 2.3.1: 2xx -> parse; 4xx (incl. 401/403) -> "unavailable", may access;
        # 5xx or unreachable -> assume complete disallow.
        rp = urllib.robotparser.RobotFileParser()
        try:
            self._wait(domain)
            r = self.client.get(f"https://{domain}/robots.txt")
            if r.status_code >= 500:
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            rp.disallow_all = True
        self._robots[domain] = rp
        return rp

    def allowed(self, url: str) -> bool:
        host = urlparse(url).hostname or ""      # robots.txt is per host (e.g. api.ashbyhq.com)
        return self._robots_for(host).can_fetch(self.cfg.user_agent, url)

    # ----- fetch -----
    def fetch(self, url: str, page_type: str = "home", use_cache: bool = True) -> FetchResult:
        domain = normalize_domain(url)
        if use_cache and self.use_cache and (row := self.store.get_page(url)) is not None and row["raw"]:
            # re-clean from stored raw HTML so cached pages always match the current cleaner
            cleaned = clean_html(row["raw"], url, self.cfg.min_block_words, self.cfg.max_tokens_per_page)
            if cleaned != row["cleaned"]:
                self.store.put_page(url, domain, page_type, row["method"], row["status_code"], row["raw"], cleaned)
            # a page with no visible text (e.g. a JS trust centre) is still a cache hit: its raw HTML
            # matters to code-only detectors, and re-fetching would just get the same empty page
            return FetchResult(PageFetch(url=url, page_type=page_type, method="cache", words=word_count(cleaned),
                                         status="ok" if cleaned else "empty_after_render"),
                               cleaned, row["raw"], url)
        if not self.allowed(url):
            return FetchResult(PageFetch(url=url, page_type=page_type, status="robots_disallowed"))
        self._wait(domain)
        try:
            r = self.client.get(url)
        except httpx.TimeoutException:
            return FetchResult(PageFetch(url=url, page_type=page_type, status="timeout"))
        except httpx.HTTPError:
            return FetchResult(PageFetch(url=url, page_type=page_type, status="error"))
        if r.status_code in (401, 403, 429, 503):
            return FetchResult(PageFetch(url=url, page_type=page_type, status="blocked"))
        if r.status_code >= 400:
            return FetchResult(PageFetch(url=url, page_type=page_type, status="error"))
        final, raw, method, note = str(r.url), r.text, "httpx", ""
        cleaned = clean_html(raw, final, self.cfg.min_block_words, self.cfg.max_tokens_per_page)
        if is_bot_challenge(raw) and word_count(cleaned) < self.cfg.render_fallback_min_words:
            # never render a challenge page: that would be getting around bot protection
            return FetchResult(PageFetch(url=url, page_type=page_type, status="blocked", note="bot challenge"))
        if word_count(cleaned) < self.cfg.render_fallback_min_words:
            rendered, note = self._render(final)
            if rendered is not None:
                if is_bot_challenge(rendered) and word_count(
                        clean_html(rendered, final, self.cfg.min_block_words, 400)) < self.cfg.render_fallback_min_words:
                    return FetchResult(PageFetch(url=url, page_type=page_type, status="blocked",
                                                 note="challenge after render"))
                r_clean = clean_html(rendered, final, self.cfg.min_block_words, self.cfg.max_tokens_per_page)
                if word_count(r_clean) > word_count(cleaned):
                    raw, cleaned, method = rendered, r_clean, "playwright"
                else:
                    note = "rendered, but no extra text; kept httpx version"
        self.store.put_page(url, domain, page_type, method, r.status_code, raw, cleaned)
        words = word_count(cleaned)
        status = "ok" if words else "empty_after_render"
        return FetchResult(PageFetch(url=url, page_type=page_type, method=method, words=words, status=status,
                                     note=note), cleaned, raw, final)

    def _render(self, url: str) -> tuple[str | None, str]:
        """Headless render for thin pages. Same URL, already robots-checked; one page, no clicks."""
        if self.renderer is None:
            return None, "thin page; render fallback unavailable (install the 'render' extra)"
        try:
            return self.renderer(url, self.cfg.user_agent, self.cfg.timeout_seconds), "rendered (thin httpx text)"
        except Exception as e:  # rendering is best-effort
            return None, f"render failed: {type(e).__name__}"

    def fetch_json(self, url: str) -> tuple[object | None, str]:
        """Polite JSON GET for public job-board APIs. Returns (data, status)."""
        host = urlparse(url).hostname or ""
        if not self.allowed(url):
            return None, "robots_disallowed"
        self._wait(host)
        try:
            r = self.client.get(url, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            return None, "timeout"
        except httpx.HTTPError:
            return None, "error"
        if r.status_code in (401, 403, 429):
            return None, "blocked"
        if r.status_code >= 400:
            return None, "error"
        try:
            return r.json(), "ok"
        except ValueError:
            return None, "error"

    def homepage(self, domain: str) -> FetchResult:
        return self.fetch(f"https://{domain}/", "home")

    def discover_links(self, base_url: str, raw_html: str) -> dict[str, str]:
        """Same-site (or subdomain) links whose path matches page_keywords. Shortest path per type wins."""
        from bs4 import BeautifulSoup

        return self._classify(base_url, [urljoin(base_url, a["href"]) for a in
                                         BeautifulSoup(raw_html, "html.parser").find_all("a", href=True)])

    def _classify(self, base_url: str, urls: list[str]) -> dict[str, str]:
        domain = normalize_domain(base_url)
        found: dict[str, str] = {}
        for url in urls:
            url = url.split("#")[0].split("?")[0]
            host = normalize_domain(url)
            if not host or not (host == domain or host.endswith("." + domain)):
                continue
            path = urlparse(url).path.lower()
            if re.search(r"\.(pdf|png|jpe?g|svg|zip|xml)$", path):
                continue
            segs = [s for s in path.split("/") if s]
            if segs and segs[0] in self.cfg.skip_path_sections:
                continue
            joined = "/" + "/".join(segs) + "/"
            # a subdomain like trust.acme.io / careers.acme.io names the page type by its first label
            label = host[: -len(domain) - 1].split(".")[-1] if host != domain else ""
            for page_type, kws in self.cfg.page_keywords.items():
                by_label = bool(label) and label in kws and not segs
                # whole segments only: /trust/ and /trust-center/ match, /trustpilot/ does not
                by_path = any(f"/{kw}/" in joined or f"/{kw}-" in joined for kw in kws)
                if by_label or by_path:
                    if page_type not in found or len(url) < len(found[page_type]):
                        found[page_type] = url
        return found

    def discover_pages(self, domain: str, home_raw: str, home_url: str) -> dict[str, str]:
        """Homepage links first; sitemap.xml fills in page types the homepage doesn't link to."""
        found = self.discover_links(home_url, home_raw)
        if set(self.cfg.page_keywords) - set(found):
            for page_type, url in self._classify(home_url, self.sitemap_urls(domain)).items():
                found.setdefault(page_type, url)
        return found

    def sitemap_urls(self, domain: str, max_urls: int = 5000) -> list[str]:
        """URLs from /sitemap.xml. If it is an index, read only its first child (no crawling)."""
        locs = self._sitemap_locs(f"https://{domain}/sitemap.xml")
        if locs and all(u.endswith(".xml") or "sitemap" in u.lower() for u in locs[:5]):
            locs = self._sitemap_locs(locs[0])
        return locs[:max_urls]

    def _sitemap_locs(self, url: str) -> list[str]:
        if not self.allowed(url):
            return []
        self._wait(normalize_domain(url))
        try:
            r = self.client.get(url)
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        return [u.strip() for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", r.text)]


_CHALLENGE = re.compile(
    r"(cf-browser-verification|challenge-platform|cf_chl_|<title>\s*just a moment|attention required!? \| cloudflare|"
    r"px-captcha|verify you are (a )?human|are you a robot|checking your browser)",
    re.I,
)


def is_bot_challenge(html: str) -> bool:
    """Markers of an interstitial challenge. Callers also require the page to be nearly empty."""
    return bool(_CHALLENGE.search(html[:50000]))
