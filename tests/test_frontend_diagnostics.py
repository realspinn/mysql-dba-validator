"""The page's Observed performance and Lock diagnostics views (frontend/assets/js/validator.js).

Verification level: source checks of the shipped page code. Rendering, states,
keyboard use, stale reads and layout were checked in headless Chrome against the
local app with mocked database routes; that harness is not part of this suite.
"""

from __future__ import annotations

import re

import pytest

from backend import lock_diagnostics as ld
from backend import observed_performance as op
from tests.frontend_page import FRONTEND_DIR, page_source

JS = (FRONTEND_DIR / "assets" / "js" / "validator.js").read_text(encoding="utf-8")
CSS = (FRONTEND_DIR / "assets" / "css" / "validator.css").read_text(encoding="utf-8")
HTML = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = JS.index(f"function {name}(")
    following = re.search(r"\n(async function|function|const|let|// -) ", JS[start + 1:])
    return JS[start:start + 1 + following.start()] if following else JS[start:]


def _block(start: str, end: str) -> str:
    return JS[JS.index(start):JS.index(end, JS.index(start))]


def _states(name: str) -> set[str]:
    block = _block(f"const {name} = ", "\n};") if name != "LOCK_STATES" else _block("const COMMON_STATES", "\n});")
    return set(re.findall(r"^\t(\w+): ", block, re.M))


@pytest.mark.parametrize("states, statuses", [("PERF_STATES", op.STATUSES), ("LOCK_STATES", ld.STATUSES)])
def test_every_collector_status_has_its_own_state(states, statuses):
    assert set(statuses) - {"available"} <= _states(states)


def test_snapshots_are_read_only_when_the_dba_asks():
    # Bound to each view's button only: no call on load, on view change or on a timer.
    assert len(re.findall(r"loadSnapshot\(", JS)) == 2  # its definition and the click binding
    assert "viewNode(view, 'LoadBtn').addEventListener('click', () => loadSnapshot(view))" in JS
    assert not re.search(r"set(Interval|Timeout)\([^)]*([Ss]napshot|[Pp]erf|[Ll]ock)", JS)


def test_routes_are_the_two_diagnostics_only():
    views = _block("const SNAPSHOT_VIEWS", "\n};")
    assert re.findall(r"route: '([\w-]+)'", views) == ["observed-performance", "lock-waits"]


def test_remote_snapshot_goes_only_to_the_connector_with_a_discovered_database():
    request = _function("requestSnapshot")
    remote = request[:request.index("const data = await requestJson")]
    assert "connectorRequest(CONNECTOR_URL + '/company/targets/' + encodeURIComponent(target.target_id) + '/' + view.route" in remote
    assert "discoveryMatches(target)" in remote
    assert "/api/" not in remote
    # The body carries the login and the database only, never SQL or a session ID.
    assert "body: JSON.stringify({ username: login.username, password: login.password, database: state.selectedDatabase })" in remote


def test_a_read_is_discarded_after_any_context_change():
    # Every change of target, login or database (even back to an earlier one) starts
    # a new generation; a read is applied only if no change happened while it ran.
    update = _function("updateSnapshotContext")
    assert "if (current !== view.seen) {" in update and "view.generation += 1;" in update
    assert update.index("view.generation += 1;") < update.index("if (view.loading) {")
    in_flight = update[update.index("if (view.loading) {"):update.index("setSnapshotButton(view);")]
    assert "if (view.loadingGeneration !== view.generation) {" in in_flight
    assert "renderSnapshotIdle(view);" in in_flight and "viewNode(view, 'LoadBtn').disabled = true;" in in_flight
    load = _function("loadSnapshot")
    assert load.index("view.loadingGeneration = view.generation;") < load.index("await requestSnapshot(view)")
    after = load[load.index("await requestSnapshot(view)"):]
    guard = after.index("if (view.loadingGeneration !== view.generation) {")
    assert guard < after.index("view.result =") and guard < after.index("renderSnapshotResult(view, data)")


def test_snapshot_context_never_includes_the_password():
    assert "password" not in _function("perfContextKey")


def test_untimed_values_are_never_shown_as_zero():
    assert "if (ms == null) return 'Not recorded';" in _function("formatDuration")
    assert "Under 1 µs" in _function("formatDuration")
    assert "Under 0.001 ms" in _function("exactDuration")
    seconds = _function("formatSeconds")
    assert "return 'Unknown';" in seconds and "Under 1 s" in seconds


def test_lock_view_never_offers_an_action_on_a_session():
    lock = _block("// ---------------------------------------------------------------- lock diagnostics", "const COMMON_STATES")
    for word in ("KILL", "kill", "Kill", "terminate", "Terminate", "COMMIT", "ROLLBACK"):
        assert word not in lock, word
    assert "Nothing is killed, committed or rolled back." in HTML


def test_public_demo_has_no_diagnostics_view_or_control():
    # Hidden by the page's stylesheet before any script runs, then removed.
    for view, section in (("performance", "performanceView"), ("locks", "locksView")):
        assert f'class="nav-item private-only" data-view="{view}"' in HTML
        assert f'class="view private-only" id="{section}"' in HTML
    assert "html[data-public-demo] .private-only {\n\tdisplay: none !important\n}" in CSS
    assert "if (PUBLIC_DEMO) document.querySelectorAll('.private-only').forEach(node => node.remove());" in JS
    assert "const VIEWS = PUBLIC_DEMO ? ['dashboard', 'review'] : ['dashboard', 'review', 'performance', 'locks'];" in JS
    assert "if (PUBLIC_DEMO || view.loading || snapshotBlocker(view)) return;" in JS


def test_views_state_what_they_are_and_are_not():
    page = page_source("index.html")
    assert "Nothing is executed and no SQL review changes." in page
    assert "Ranked by total recorded time across all executions of each family." in page
    assert "Not a view of all server activity." in page
    assert "They are not a measurement of the SQL in your review." in page
    assert "A blocking session may already have finished: refresh before acting on it." in page
    assert "Metadata locks and other contention are not included." in page
