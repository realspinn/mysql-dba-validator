"""The page's Observed performance and Lock diagnostics views (web/src/features/diagnostics).

Verification level: source checks of the page code, and the built bundle for the
online demo. Rendering, states and layout were checked in headless Chrome against the
local app; that harness is not part of this suite.
"""

from __future__ import annotations

import re

import pytest

from backend import lock_diagnostics as ld
from backend import observed_performance as op
from tests.frontend_page import all_source, function_body, source

TEXT = source("features/diagnostics/snapshotText.ts")
HOOK = source("features/diagnostics/useSnapshot.ts")
VIEW = source("features/diagnostics/SnapshotView.tsx")
LOCKS = source("features/diagnostics/LockResult.tsx")


def _states(name: str) -> set[str]:
    block = TEXT[TEXT.index(f"const {name}: Record<string, StateText> = {{"):]
    return set(re.findall(r"^\t(\w+): ", block[:block.index("\n};")], re.M))


@pytest.mark.parametrize("states, statuses", [("PERF_STATES", op.STATUSES), ("LOCK_STATES", ld.STATUSES)])
def test_every_collector_status_has_its_own_state(states, statuses):
    assert set(statuses) - {"available"} <= _states(states)


def test_snapshots_are_read_only_when_the_dba_asks():
    # Bound to each view's button only: no read on load, on view change or on a timer.
    assert all_source().count("snapshot.load()") == 1
    assert "onClick={() => void snapshot.load()}" in VIEW
    assert "useEffect(" not in HOOK.split("const load = ")[0].replace(
        "useEffect(() => { if (result && result.key !== key) setResult(null); }, [key, result]);", "")
    assert not re.search(r"set(Interval|Timeout)", HOOK + VIEW)


def test_routes_are_the_two_diagnostics_only():
    assert re.findall(r"route: '([\w-]+)', list", TEXT) == ["observed-performance", "lock-waits"]


def test_remote_snapshot_goes_only_to_the_connector_with_a_discovered_database():
    load = function_body(HOOK, "load")
    assert "actions.withTarget((token, target, login) => connector.remoteSnapshot(token, target.target_id, kind.route, login, database), true)" in load
    # withTarget refuses a database not discovered for this target, login and session.
    with_target = function_body(source("features/connection/useConnection.ts"), "withTarget")
    assert "if ((s.database || needDatabase) && !discoveryMatches(s))" in with_target
    remote = function_body(source("api/connector.ts"), "remoteSnapshot")
    assert "'/api/" not in remote
    # The body carries the login and the database only, never SQL or a session ID.
    assert "{ username: login.username.trim(), password: login.password, database }" in remote


def test_a_read_is_discarded_after_any_context_change():
    # Every change of target, login or database (even back to an earlier one) starts
    # a new generation; a read is applied only if no change happened while it ran.
    assert "if (seen.current !== key) {" in HOOK and "generation.current += 1;" in HOOK
    load = function_body(HOOK, "load")
    assert load.index("const started = generation.current;") < load.index("await ")
    guard = load.index("if (started !== generation.current) return;")
    assert guard < load.index("setResult({ key: startedKey, value });")
    # A shown result belongs to the context it was read for.
    assert "const shown = result && result.key === key ? result.value : null;" in HOOK


def test_snapshot_context_never_includes_the_password():
    key = source("features/connection/connectionState.ts").split("export const contextKey")[1]
    assert "password" not in key.split(";")[0]


def test_untimed_values_are_never_shown_as_zero():
    assert "if (ms == null) return 'Not recorded';" in function_body(TEXT, "formatDuration")
    assert "Under 1 µs" in function_body(TEXT, "formatDuration")
    assert "Under 0.001 ms" in function_body(TEXT, "exactDuration")
    seconds = function_body(TEXT, "formatSeconds")
    assert "return 'Unknown';" in seconds and "Under 1 s" in seconds


def test_lock_view_never_offers_an_action_on_a_session():
    for word in ("KILL", "kill", "Kill", "terminate", "Terminate", "COMMIT", "ROLLBACK", "<button"):
        assert word not in LOCKS, word
    assert "Nothing is killed, committed or rolled back." in TEXT


def test_public_demo_has_no_diagnostics_view_or_control():
    app = source("app/App.tsx")
    assert "const VIEWS: View[] = PUBLIC_DEMO ? ['dashboard', 'review'] : ['dashboard', 'review', 'performance', 'locks'];" in app
    assert "NAV.filter(item => views.includes(item.id))" in source("app/Sidebar.tsx")
    # The demo server refuses the routes whatever the page does (test_public_demo).


def test_views_state_what_they_are_and_are_not():
    text = all_source()
    assert "Nothing is executed and no SQL review changes." in text
    assert "Ranked by total recorded time across all executions of each family." in text
    assert "Not a view of all server activity." in text
    assert "They are not a measurement of the SQL in your review." in text
    assert "A blocking session may already have finished: refresh before acting on it." in " ".join(text.split())
    assert "Metadata locks and other contention are not included." in text
