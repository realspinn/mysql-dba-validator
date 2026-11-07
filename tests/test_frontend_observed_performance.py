"""The page's Observed performance view (frontend/assets/js/validator.js).

Verification level: source checks of the shipped page code. Rendering, states,
keyboard use and layout were checked in headless Chrome against the local app with
mocked database routes; that harness is not part of this suite.
"""

from __future__ import annotations

import re

from backend import observed_performance as op
from tests.frontend_page import FRONTEND_DIR, page_source

JS = (FRONTEND_DIR / "assets" / "js" / "validator.js").read_text(encoding="utf-8")
CSS = (FRONTEND_DIR / "assets" / "css" / "validator.css").read_text(encoding="utf-8")
HTML = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = JS.index(f"function {name}(")
    following = re.search(r"\n(async function|function) ", JS[start + 1:])
    return JS[start:start + 1 + following.start()] if following else JS[start:]


def test_every_collector_status_has_its_own_state():
    states = set(re.findall(r"^\t(\w+): \{", JS[JS.index("const PERF_STATES"):JS.index("function settingsButton")], re.M))
    assert set(op.STATUSES) - {"available"} == states


def test_snapshot_is_read_only_when_the_dba_asks():
    # Bound to the button only: no call on load, on view change or on a timer.
    calls = [m.start() for m in re.finditer(r"loadObservedPerformance\b", JS)]
    assert len(calls) == 2  # its definition and the click binding
    assert "perfLoadBtn.addEventListener('click', loadObservedPerformance)" in JS
    assert not re.search(r"set(Interval|Timeout)\([^)]*[Pp]erf", JS)


def test_remote_snapshot_goes_only_to_the_connector_with_a_discovered_database():
    request = _function("requestObservedPerformance")
    remote = request[:request.index("const data = await requestJson")]
    assert "connectorRequest(CONNECTOR_URL + '/company/targets/'" in remote
    assert "/observed-performance'" in remote
    assert "discoveryMatches(target)" in remote
    assert "/api/" not in remote
    # The body carries the login and the database only, never SQL.
    assert "body: JSON.stringify({ username: login.username, password: login.password, database: state.selectedDatabase })" in remote


def test_a_response_for_an_earlier_context_is_discarded():
    # The context is captured before the request and compared after it: a slow
    # response for another target, login or database is never shown. (Exercised in
    # headless Chrome with a held response and a database change; see PR #47.)
    load = _function("loadObservedPerformance")
    assert load.index("const key = perfContextKey();") < load.index("await requestObservedPerformance()")
    after = load[load.index("await requestObservedPerformance()"):]
    guard = after.index("if (key !== perfContextKey()) {")
    assert guard < after.index("perf.result =") and guard < after.index("renderPerfResult(data)")


def test_a_context_change_clears_the_view_while_a_read_is_in_flight():
    # The shown result is dropped on the change itself, not when the late read
    # settles; the button stays disabled until it does.
    update = _function("updatePerformanceContext")
    assert update.index("perf.result = null;") < update.index("if (perf.loading) {")
    in_flight = update[update.index("if (perf.loading) {"):update.index("setPerfButton();")]
    assert "if (perf.loadingKey !== current) {" in in_flight
    assert "renderPerfIdle();" in in_flight and "perfLoadBtn.disabled = true;" in in_flight
    assert "perf.loadingKey = key;" in _function("loadObservedPerformance")


def test_snapshot_context_never_includes_the_password():
    assert "password" not in _function("perfContextKey")


def test_untimed_values_are_never_shown_as_zero():
    assert "if (ms == null) return 'Not recorded';" in _function("formatDuration")
    assert "Under 1 µs" in _function("formatDuration")
    assert "Under 0.001 ms" in _function("exactDuration")


def test_public_demo_has_no_diagnostics_view_or_control():
    # Hidden by the page's stylesheet before any script runs, then removed.
    assert 'class="nav-item private-only" data-view="performance"' in HTML
    assert 'class="view private-only" id="performanceView"' in HTML
    assert "html[data-public-demo] .private-only {\n\tdisplay: none !important\n}" in CSS
    assert "if (PUBLIC_DEMO) document.querySelectorAll('.private-only').forEach(node => node.remove());" in JS
    assert "const VIEWS = PUBLIC_DEMO ? ['dashboard', 'review'] : ['dashboard', 'review', 'performance'];" in JS
    assert "if (PUBLIC_DEMO || state.performance.loading || perfBlocker()) return;" in JS


def test_view_states_what_it_is_and_is_not():
    page = page_source("index.html")
    assert "Nothing is executed and no SQL review changes." in page
    assert "Ranked by total recorded time across all executions of each family." in page
    assert "Not a view of all server activity." in page
    assert "They are not a measurement of the SQL in your review." in page
