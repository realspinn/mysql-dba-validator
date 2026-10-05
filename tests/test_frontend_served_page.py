"""Regression tests for the frontend page served at GET /.

These are static checks on the served HTML/JS. They guard against:
- serving a page whose rendering helpers are missing (the legacy page called
  undefined helpers, so no result could ever render);
- the served page losing the connector validation route;
- remote credentials being routed to the public /api/* endpoints;
- a hidden browser global being treated as a connector pairing mechanism.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

REQUIRED_HELPERS = [
    "el",
    "metric",
    "plainList",
    "layer",
    "factorList",
    "evidenceLayer",
    "metadataLayer",
    "statementCard",
    "renderResults",
    "renderError",
    "analysisModeLabel",
    "metadataStatusLabel",
    "updateStepper",
]


@pytest.fixture(scope="module")
def served_html() -> str:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def _function_body(source: str, name: str) -> str:
    """Return the brace-delimited body of `function name(...) { ... }`."""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", source)
    assert match, f"function {name} not found in served page"
    depth = 0
    for index in range(match.end() - 1, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.end():index]
    raise AssertionError(f"unbalanced braces in function {name}")


def test_root_serves_the_intended_frontend(served_html):
    expected = (FRONTEND_DIR / "index.html").read_bytes()
    assert served_html.encode("utf-8") == expected
    assert "<title>MySQL DBA Validator</title>" in served_html


def test_served_page_contains_connector_validation_route(served_html):
    assert "CONNECTOR_URL = 'http://127.0.0.1:8765'" in served_html
    assert "'/company/targets/'" in served_html
    assert "'/validate'" in served_html
    remote = _function_body(served_html, "validateRemote")
    assert "CONNECTOR_URL + '/company/targets/'" in remote
    assert "'/validate'" in remote


@pytest.mark.parametrize("helper", REQUIRED_HELPERS)
def test_served_page_defines_required_rendering_helpers(served_html, helper):
    assert re.search(r"function\s+" + re.escape(helper) + r"\s*\(", served_html), (
        f"served page does not define {helper}()"
    )


def test_served_page_has_no_legacy_remote_credential_post(served_html):
    # The legacy page posted every target's credentials straight to /api/validate.
    assert "const body = { sql, host: state.credentials.host" not in served_html


def test_remote_validation_never_calls_public_api(served_html):
    remote = _function_body(served_html, "validateRemote")
    assert "/api/" not in remote

    dispatch = _function_body(served_html, "validateSql")
    assert "isRemoteTarget(state.credentials.host)" in dispatch
    assert "validateRemote(sql)" in dispatch
    assert "validateLocal(sql)" in dispatch
    assert "/api/" not in dispatch

    # /api/validate is only reachable from the local path.
    assert served_html.count("'/api/validate'") == 1
    assert "'/api/validate'" in _function_body(served_html, "validateLocal")


@pytest.mark.parametrize(
    "function_name, public_endpoint",
    [
        ("testConnection", "'/api/connections/test'"),
        ("discoverDatabases", "'/api/connections/discover-databases'"),
    ],
)
def test_remote_targets_are_blocked_before_public_connection_endpoints(
    served_html, function_name, public_endpoint
):
    body = _function_body(served_html, function_name)
    guard = body.find("if (isRemoteTarget(state.credentials.host))")
    call = body.find(public_endpoint)
    assert guard != -1, f"{function_name} has no remote-target guard"
    assert call != -1
    assert guard < call, f"{function_name} calls {public_endpoint} before the remote guard"
    guard_block = body[guard:call]
    assert "return;" in guard_block


def test_remote_target_check_does_not_depend_on_port(served_html):
    body = _function_body(served_html, "isRemoteTarget")
    assert "port" not in body


def test_no_hidden_pairing_global_in_served_page(served_html):
    # Milestone B: pairing now exists, but only as an explicit user action. The
    # session must never come from a hidden global, browser storage, or the URL,
    # and ensureConnectorSession must never pair implicitly.
    assert "__CONNECTOR_PAIRING_TOKEN__" not in served_html
    for forbidden in ("localStorage", "sessionStorage", "document.cookie",
                      "URLSearchParams", "location.hash", "location.search", "window.__"):
        assert forbidden not in served_html, forbidden
    session = _function_body(served_html, "ensureConnectorSession")
    assert "throw new Error" in session
    assert "fetch" not in session and "requestJson" not in session
    assert "connectorRequest" not in session and "CONNECTOR_URL" not in session
    # The only pairing call is the explicit pairConnector() action, fed by the input field.
    assert served_html.count("CONNECTOR_URL + '/pair'") == 2  # the call, and the 401 exemption
    pair = _function_body(served_html, "pairConnector")
    assert "pairingCode.value" in pair
    assert "CONNECTOR_URL + '/pair'" in pair


def test_served_page_keeps_database_list_limited_warning(served_html):
    assert 'id="discoveryWarning"' in served_html
    assert "Database list is limited. Not all visible databases are shown." in served_html
    discover = _function_body(served_html, "discoverDatabases")
    assert "data.complete === false" in discover
    assert "$('discoveryWarning').hidden = false" in discover


def test_served_page_keeps_readable_report_labels(served_html):
    assert "'Static + database unavailable'" in served_html
    assert "'Database evidence'" in served_html
    assert "'Metadata-based scoring was not applied.'" in served_html
    assert "High estimated rows applied: " in served_html
    assert 'class="stepper"' in served_html
    assert "Ready for review" in served_html


# ---------------------------------------------------------------- B2 approved-target flow
# Static inspection of the served page only; no browser executes this JavaScript.


def test_target_selector_offers_local_and_connector_targets(served_html):
    assert '<select id="targetSelect">' in served_html
    assert '<option value="">Local (this machine)</option>' in served_html
    render = _function_body(served_html, "renderTargetOptions")
    assert "new Option('Local (this machine)', '')" in render
    assert "target.target_id" in render


def test_approved_targets_are_loaded_from_connector_and_filtered(served_html):
    load = _function_body(served_html, "loadApprovedTargets")
    assert "CONNECTOR_URL + '/company/targets'" in load
    assert "method: 'GET'" in load
    assert "target.approval_state === 'approved'" in load
    assert "isRemoteTarget(String(target.host || ''))" in load
    assert "/api/" not in load
    # A previously selected target that is no longer listed is dropped.
    assert "selectTarget('')" in load
    # Pairing loads targets; the session is the only thing that authorises the listing.
    assert "await loadApprovedTargets();" in _function_body(served_html, "pairConnector")


def test_selected_target_makes_host_and_port_display_only(served_html):
    select = _function_body(served_html, "selectTarget")
    assert "state.approvedTargets.find(t => t.target_id === targetId)" in select
    assert "host.readOnly = port.readOnly = Boolean(target)" in select
    assert "clearDiscoveredDatabases();" in select


def test_remote_validation_sends_target_id_not_host_or_port(served_html):
    remote = _function_body(served_html, "validateRemote")
    assert "const remoteTarget = selectedRemoteTarget();" in remote
    assert "encodeURIComponent(remoteTarget.target_id)" in remote
    assert "state.credentials.host" not in remote and "state.credentials.port" not in remote
    assert "host:" not in remote and "port:" not in remote
    assert "findApprovedRemoteTarget" not in served_html
    # Refusal happens before any request when no approved target is selected.
    refusal = _function_body(served_html, "selectedRemoteTarget")
    assert "if (!state.remoteTarget)" in refusal and "throw new Error" in refusal
    assert "fetch" not in refusal and "connectorRequest" not in refusal
    dispatch = _function_body(served_html, "validateSql")
    assert "state.remoteTarget || isRemoteTarget(state.credentials.host)" in dispatch


def test_remote_discovery_is_connector_only(served_html):
    discover = _function_body(served_html, "discoverRemote")
    assert "CONNECTOR_URL + '/company/targets/' + encodeURIComponent(target.target_id) + '/databases'" in discover
    assert "JSON.stringify({ username: login.username, password: login.password })" in discover
    assert "/api/" not in discover and "requestJson" not in discover
    body = _function_body(served_html, "discoverDatabases")
    # The public discovery endpoint is only the non-remote branch of the ternary.
    ternary = body.index("const data = remoteTarget")
    assert body.index("? await discoverRemote(remoteTarget)") < body.index("'/api/connections/discover-databases'")
    assert ternary < body.index("? await discoverRemote(remoteTarget)")


def test_selected_database_is_bound_to_target_login_and_session(served_html):
    matches = _function_body(served_html, "discoveryMatches")
    for check in ("discovery.targetId === target.target_id",
                  "discovery.username === state.credentials.username",
                  "discovery.sessionToken === state.connectorSessionToken",
                  "discovery.databases.includes(state.selectedDatabase)"):
        assert check in matches
    remote = _function_body(served_html, "validateRemote")
    guard = remote.index("if (!discoveryMatches(remoteTarget))")
    assert guard < remote.index("body.database = state.selectedDatabase")
    # Discovery results are discarded if target, login or session changed mid-request.
    body = _function_body(served_html, "discoverDatabases")
    assert "state.connectorSessionToken !== binding.sessionToken" in body
    assert "data.target_id !== binding.targetId" in body


def test_discovery_state_is_cleared_on_target_login_and_session_changes(served_html):
    clear = _function_body(served_html, "clearDiscoveredDatabases")
    assert "state.discovery = null;" in clear and "state.selectedDatabase = '';" in clear
    # Username/password/host changes go through syncCredentials.
    assert "clearDiscoveredDatabases();" in _function_body(served_html, "syncCredentials")
    assert "[host, port, username, password].forEach(input => input.addEventListener('input', syncCredentials));" in served_html
    # Unpair, session expiry (401) and re-pairing all go through setPaired.
    paired = _function_body(served_html, "setPaired")
    assert "state.approvedTargets = [];" in paired
    assert "selectTarget('')" in paired and "clearDiscoveredDatabases()" in paired
    assert "setPaired(null," in _function_body(served_html, "unpairConnector")
    assert "setPaired(null," in _function_body(served_html, "connectorRequest")


# ---------------------------------------------------------------- web-app pass regressions
# Found with real Chrome: local limit errors arrive as HTTP 200 {"error": code}
# without a message, and the page showed "Validation returned an unexpected response."


@pytest.mark.parametrize("sql, code", [
    (";".join(["SELECT 1"] * 17), "statement_limit_exceeded"),
    ("SELECT 1 -- " + "x" * (65 * 1024), "request_too_large"),
], ids=["statement_limit", "oversized_sql"])
def test_local_limit_errors_returned_by_api_have_friendly_page_messages(served_html, sql, code):
    response = TestClient(app).post("/api/validate", json={"sql": sql})
    assert response.status_code == 200 and response.json().get("error") == code
    mapping = served_html[served_html.index("const RESPONSE_ERROR_MESSAGES = {"):]
    mapping = mapping[:mapping.index("};")]
    assert f"{code}: '" in mapping
    error_message = _function_body(served_html, "errorMessage")
    assert "data.error" in error_message and "RESPONSE_ERROR_MESSAGES[code]" in error_message
    assert "typeof data.message === 'string'" in error_message  # explicit messages still win


def test_connector_statement_limit_has_friendly_message(served_html):
    connector_map = served_html[served_html.index("const CONNECTOR_ERROR_MESSAGES = {"):]
    assert "statement_limit_exceeded: '" in connector_map[:connector_map.index("};")]


def test_leaving_connector_mode_clears_company_login(served_html):
    """Found with real Chrome: after Remote -> Local the company login stayed in the
    form and the next Local validation posted it to the public /api/validate."""
    select = _function_body(served_html, "selectTarget")
    opening = "} else if (wasRemote) {"
    branch = select[select.index(opening) + len(opening):]
    branch = branch[:branch.index("}")]
    assert "username.value = '';" in branch and "password.value = '';" in branch
    # The clearing happens before credentials are re-read into state.
    assert select.index("password.value = '';") < select.index("syncCredentials();")
    # Unpair and session loss reach the same branch via selectTarget('').
    assert "if (state.remoteTarget) selectTarget('');" in _function_body(served_html, "setPaired")


@pytest.mark.parametrize("function_name", ["testConnection", "discoverDatabases"])
def test_discover_button_is_never_left_disabled(served_html, function_name):
    """Found with real Chrome: a finally block disabled Discover when credentials
    were invalid at that instant (e.g. cleared by session expiry mid-discovery),
    and nothing re-enabled it once valid credentials were entered again."""
    body = _function_body(served_html, function_name)
    finally_block = body[body.rindex("finally {"):]
    assert "discoverBtn.disabled = false;" in finally_block
    assert "discoverBtn.disabled = !validConnection()" not in served_html
    # Invalid input is still rejected when the button is clicked.
    assert "if (!validConnection()) {" in _function_body(served_html, "discoverDatabases")


def test_sql_editor_has_an_accessible_name(served_html):
    match = re.search(r'<textarea id="sqlInput"[^>]*aria-labelledby="([^"]+)"', served_html)
    assert match, "sqlInput has no aria-labelledby"
    assert f'id="{match.group(1)}"' in served_html


def test_default_local_page_request_is_accepted_by_api():
    """The page's default (untouched form) local request must validate statically."""
    client = TestClient(app)
    response = client.post(
        "/api/validate",
        json={
            "sql": "SELECT * FROM users;",
            "host": "127.0.0.1",
            "port": 3306,
            "username": "",
            "password": "",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data.get("statements"), list)
    assert data["statement_count"] == 1
