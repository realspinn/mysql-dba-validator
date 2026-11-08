"""The pages and their assets after an update (backend/main.py frontend_page).

v0.10.0 after an in-place update from v0.9.0: Chrome reused v0.9.0's page and
script, which were served with no Cache-Control at unchanged URLs, so the new page
ran the old script and the new views stayed hidden. The page is now revalidated on
every load, and its script and stylesheet links carry the running version. The built
file names also carry a content hash, so a changed file always has a new address.

Verification level: TestClient (HTTP responses). The upgrade itself was reproduced
and the fix checked in headless Chrome, serving v0.9.0 then this code on one port
with one browser profile; that harness is not part of this suite.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from backend.main import FRONTEND_DIR, PAGES_DIR, app, frontend_page
from backend.version import APP_VERSION

LOCAL = TestClient(app, base_url="http://127.0.0.1:8420")
LINK = re.compile(r'(?:href|src)="(/static/app/assets/[\w.-]+\.(?:css|js))(\?v=[^"]*)?"')
HASHED = re.compile(r"/static/app/assets/(index|home)-[\w-]{8}\.(css|js)")


@pytest.mark.parametrize("path", ["/", "/demo"])
def test_the_validator_page_is_revalidated_and_links_this_release(path):
    response = LOCAL.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    links = LINK.findall(response.text)
    assert sorted(file.rsplit(".", 1)[1] for file, _ in links) == ["css", "js"]
    assert all(HASHED.fullmatch(file) for file, _ in links), links
    assert {query for _, query in links} == {f"?v={APP_VERSION}"}


def test_the_linked_files_are_this_release_s_files():
    page = LOCAL.get("/").text
    for file, query in LINK.findall(page):
        response = LOCAL.get(file + query)
        assert response.status_code == 200
        assert response.content == (FRONTEND_DIR / file.removeprefix("/static/")).read_bytes(), file


@pytest.mark.parametrize("name", ["index.html", "home.html"])
def test_only_the_script_and_stylesheet_links_change(name):
    original = (PAGES_DIR / name).read_bytes().decode("utf-8")  # line endings as on disk
    served = frontend_page(name)
    assert served.replace(f"?v={APP_VERSION}", "") == original
    assert served.count(f"?v={APP_VERSION}") == 2


def test_api_responses_are_still_never_stored():
    assert LOCAL.get("/api/health").headers["cache-control"] == "no-store"
