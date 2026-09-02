"""The SPA fallback in api/main.py: the app shell must never be cached (a
rebuilt frontend references new hashed asset filenames), while the hashed
assets themselves are untouched by this and may cache freely."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app, _WEB_DIST

pytestmark = pytest.mark.skipif(
    not _WEB_DIST.is_dir(), reason="web/dist not built in this checkout")


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_root_shell_is_never_cached(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_unknown_client_route_falls_through_to_the_uncached_shell(client):
    # Not a real API prefix — those 503 pre-setup via the setup gate before
    # ever reaching this route, which would defeat the point of this test.
    r = client.get("/not-a-real-api-route/nested")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_direct_index_html_request_is_also_uncached(client):
    r = client.get("/index.html")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


def test_a_real_asset_file_is_untouched_by_the_no_store_rule(client):
    assets = list((_WEB_DIST / "assets").glob("*.js"))
    assert assets, "expected at least one built JS asset"
    r = client.get(f"/assets/{assets[0].name}")
    assert r.status_code == 200
    assert r.headers.get("cache-control") != "no-store"
