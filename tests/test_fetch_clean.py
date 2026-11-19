from conftest import HOME_HTML

from sprinto.clean import clean_html
from sprinto.fetch import normalize_domain

ROBOTS_OK = (200, "User-agent: *\nAllow: /\n")


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.io/about?x=1") == "acme.io"
    assert normalize_domain("acme.io") == "acme.io"
    assert normalize_domain("  # comment") == ""


def test_clean_drops_short_cookie_and_dupes():
    text = clean_html(HOME_HTML + HOME_HTML.replace("<html>", "<div>"), "https://acme.io/")
    assert "SOC 2 Type II" in text
    assert "cookies" not in text
    assert "Too short" not in text
    assert text.count("Enterprise plan includes SSO") == 1


def test_clean_keeps_pricing_rows_by_merging_short_lines():
    html = ("<html><body><main><h2>Enterprise</h2><ul>" +
            "".join(f"<li>{x}</li>" for x in ["SAML and SCIM", "Audit log", "Advanced security",
                                               "Custom data retention", "Priority support", "$16 per user"]) +
            "</ul></main></body></html>")
    text = clean_html(html, "u")
    assert "SAML and SCIM · Audit log" in text and "$16 per user" in text


def test_clean_reads_div_built_pages():
    html = ("<html><body><nav>Home Product Pricing Careers Log in Sign up for free today</nav><main><div><span>"
            + "</span></div><div><span>".join(
                f"Linear-style product sentence number {i} about planning and building software well"
                for i in range(20)) + "</span></div></main></body></html>")
    text = clean_html(html, "u")
    assert len(text.split()) > 150 and "Sign up for free" not in text


def test_clean_caps_tokens():
    html = "<html><body>" + "".join(f"<p>{'word ' * 20} paragraph number {i}</p>" for i in range(500)) + "</body></html>"
    assert len(clean_html(html, "u", max_tokens=200).split()) <= 150


def test_fetch_ok_then_cached(make_fetcher, store):
    f = make_fetcher({"https://acme.io/robots.txt": ROBOTS_OK, "https://acme.io/": (200, HOME_HTML)})
    r = f.homepage("acme.io")
    assert r.page.status == "ok" and r.page.method == "httpx" and r.page.words > 20
    assert store.get_page("https://acme.io/") is not None
    r2 = f.homepage("acme.io")
    assert r2.page.method == "cache" and r2.cleaned == r.cleaned


def test_cache_is_recleaned_with_current_cleaner(make_fetcher, store):
    store.put_page("https://acme.io/", "acme.io", "home", "httpx", 200, HOME_HTML, "stale old cleaned text")
    r = make_fetcher({}).homepage("acme.io")       # no network routes: must come from cache
    assert r.page.method == "cache" and "SOC 2 Type II" in r.cleaned and "stale" not in r.cleaned
    assert store.get_page("https://acme.io/")["cleaned"] == r.cleaned


def test_robots_disallow_respected(make_fetcher):
    f = make_fetcher({"https://acme.io/robots.txt": (200, "User-agent: *\nDisallow: /\n"),
                      "https://acme.io/": (200, HOME_HTML)})
    assert f.homepage("acme.io").page.status == "robots_disallowed"


import pytest


@pytest.mark.parametrize("code", [401, 403, 404, 410])
def test_robots_4xx_means_unavailable_so_allowed(make_fetcher, code):
    """RFC 9309 2.3.1.3: any 4xx on robots.txt = unavailable; the crawler may access."""
    f = make_fetcher({"https://acme.io/robots.txt": (code, ""), "https://acme.io/": (200, HOME_HTML)})
    assert f.homepage("acme.io").page.status == "ok"


@pytest.mark.parametrize("code", [500, 503])
def test_robots_5xx_means_complete_disallow(make_fetcher, code):
    """RFC 9309 2.3.1.4: server error on robots.txt = assume complete disallow."""
    f = make_fetcher({"https://acme.io/robots.txt": (code, ""), "https://acme.io/": (200, HOME_HTML)})
    assert f.homepage("acme.io").page.status == "robots_disallowed"


def test_robots_unreachable_means_complete_disallow(cfg, store):
    import httpx
    from sprinto.fetch import Fetcher

    def handler(req):
        if req.url.path == "/robots.txt":
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=HOME_HTML)

    f = Fetcher(cfg.scrape, store, client=httpx.Client(transport=httpx.MockTransport(handler)),
                sleep=lambda s: None, renderer=None)
    assert f.homepage("acme.io").page.status == "robots_disallowed"


def test_robots_is_per_host(make_fetcher):
    f = make_fetcher({"https://acme.io/robots.txt": (200, "User-agent: *\nAllow: /\n"),
                      "https://api.ashbyhq.com/robots.txt": (401, "Unauthorized"),
                      "https://jobs.example.com/robots.txt": (200, "User-agent: *\nDisallow: /\n")})
    assert f.allowed("https://api.ashbyhq.com/posting-api/job-board/acme")
    assert not f.allowed("https://jobs.example.com/anything")
    assert f.allowed("https://acme.io/pricing")


def test_missing_robots_allows(make_fetcher):
    f = make_fetcher({"https://acme.io/": (200, HOME_HTML)})
    assert f.homepage("acme.io").page.status == "ok"


def test_blocked_is_reported_not_bypassed(make_fetcher):
    f = make_fetcher({"https://acme.io/robots.txt": ROBOTS_OK, "https://acme.io/": (403, "Access denied")})
    assert f.homepage("acme.io").page.status == "blocked"


def test_politeness_delay_between_requests(cfg, store):
    import httpx
    from conftest import make_transport
    from sprinto.fetch import Fetcher

    sleeps = []
    client = httpx.Client(transport=make_transport({"https://acme.io/": (200, HOME_HTML)}))
    f = Fetcher(cfg.scrape, store, client=client, sleep=sleeps.append, renderer=None)
    f.homepage("acme.io")   # robots + homepage = two requests to the same domain
    assert sleeps and all(0 < s <= cfg.scrape.delay_seconds[1] for s in sleeps)


def test_discover_links_same_site_only(make_fetcher):
    f = make_fetcher({})
    links = f.discover_links("https://acme.io/", HOME_HTML)
    assert links == {"careers": "https://acme.io/careers", "pricing": "https://acme.io/pricing"}
