"""The page and its assets after an update (backend/main.py frontend_page).

v0.10.0 after an in-place update from v0.9.0: Chrome reused v0.9.0's page and
script, which were served with no Cache-Control at unchanged URLs, so the new page
ran the old script and the new views stayed hidden. The page is now revalidated on
every load, and its stylesheet and script links carry the running version, so a new
release's page never runs an earlier release's files.

Verification level: TestClient (HTTP responses). The upgrade itself was reproduced
and the fix checked in headless Chrome, serving v0.9.0 then this code on one port
with one browser profile; that harness is not part of this suite.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from backend.main import FRONTEND_DIR, app, frontend_page
from backend.version import APP_VERSION

LOCAL = TestClient(app, base_url="http://127.0.0.1:8420")
LINK = re.compile(r'(?:href|src)="(/static/assets/[\w/.-]+\.(?:css|js))(\?v=[^"]*)?"')


@pytest.mark.parametrize("path", ["/", "/demo"])
def test_the_validator_page_is_revalidated_and_links_this_release(path):
    response = LOCAL.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    links = LINK.findall(response.text)
    assert sorted(file for file, _ in links) == ["/static/assets/css/validator.css", "/static/assets/js/validator.js"]
    assert {query for _, query in links} == {f"?v={APP_VERSION}"}


def test_the_linked_files_are_this_release_s_files():
    page = LOCAL.get("/").text
    for file, query in LINK.findall(page):
        response = LOCAL.get(file + query)
        assert response.status_code == 200
        assert response.content == (FRONTEND_DIR / file.removeprefix("/static/")).read_bytes(), file


@pytest.mark.parametrize("name", ["index.html", "home.html"])
def test_only_the_stylesheet_and_script_links_change(name):
    original = (FRONTEND_DIR / name).read_text(encoding="utf-8")
    served = frontend_page(name)
    assert served.replace(f"?v={APP_VERSION}", "") == original
    assert served.count(f"?v={APP_VERSION}") == 2


def test_api_responses_are_still_never_stored():
    assert LOCAL.get("/api/health").headers["cache-control"] == "no-store"
