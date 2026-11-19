import httpx
import pytest

from sprinto.config import load_config
from sprinto.fetch import Fetcher
from sprinto.store import Store

HOME_HTML = """<html><head><title>Acme</title></head><body>
<nav>Home Pricing Careers</nav>
<main>
<h1>Acme helps finance teams close the books faster every single month</h1>
<p>Acme is a B2B SaaS platform for mid-market finance teams across the US and Europe.</p>
<p>Our Enterprise plan includes SSO, SAML, audit logs and a dedicated security review.</p>
<p>We are currently working towards SOC 2 Type II and expect our audit this year.</p>
<p>Trusted by finance teams at Globex, Initech and Umbrella Corp around the world.</p>
<p>We use cookies to improve your experience on this website, accept all to continue.</p>
<p>Too short.</p>
<a href="/careers">Careers</a> <a href="https://acme.io/pricing">Pricing</a> <a href="https://other.com/jobs">x</a>
</main><footer>All rights reserved</footer></body></html>"""


def make_transport(routes: dict[str, tuple[int, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key not in routes:
            return httpx.Response(404, text="not found")
        code, body = routes[key]
        return httpx.Response(code, text=body)
    return httpx.MockTransport(handler)


@pytest.fixture
def cfg():
    return load_config(1)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def make_fetcher(cfg, store):
    def _make(routes):
        client = httpx.Client(transport=make_transport(routes), follow_redirects=True)
        return Fetcher(cfg.scrape, store, client=client, sleep=lambda s: None, renderer=None)
    return _make
