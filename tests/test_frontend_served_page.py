"""Regression tests for the frontend page served at GET /.

These are static checks on the served HTML/JS. They guard against:
- serving a page whose rendering helpers are missing (the legacy page called
  undefined helpers, so no result could ever render);
- the served page losing the connector validation route;
- remote credentials being routed to the public /api/* endpoints;
- a hidden browser global being treated as a connector pairing mechanism.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.db import EVIDENCE_STATUSES
from backend.main import app
from tests.frontend_page import with_page_files

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


PAGE_FILE_TYPES = {".css": "text/css", ".js": "text/javascript"}


def _fetch_page_file(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers["content-type"].startswith(PAGE_FILE_TYPES[Path(path).suffix]), path
    return response.text


@pytest.fixture(scope="module")
def served_html() -> str:
    """The page at GET / with the stylesheet and script it loads, as served, inline."""
    client = TestClient(app, base_url="http://127.0.0.1:8420")
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return with_page_files(response.text, lambda path: _fetch_page_file(client, path))


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
    response = TestClient(app, base_url="http://127.0.0.1:8420").get("/")
    assert response.content == (FRONTEND_DIR / "index.html").read_bytes()
    assert "<title>MySQL DBA Validator</title>" in served_html


def test_page_loads_its_own_stylesheet_and_script_in_place():
    page = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    head, body = page.split("</head>")
    # The appearance is set before first paint (inline), then the stylesheet loads.
    assert head.index("localStorage.getItem('mdv-theme')") < head.index(
        '<link rel="stylesheet" href="/static/assets/css/validator.css" />')
    # The script runs where the inline script ran: after the markup, at the end of
    # the body, as an ordinary script (no defer, async or module).
    assert body.count("<script") == 1
    assert body.rstrip().endswith('<script src="/static/assets/js/validator.js"></script>\n</body>\n\n</html>')


def test_page_files_keep_their_types_when_the_system_says_text_plain():
    # On Windows, Python's mimetypes reads the registry, where some programs register
    # .js as text/plain; with nosniff a browser would then not run the page's script.
    code = """
import mimetypes
mimetypes.add_type("text/plain", ".js")
mimetypes.add_type("text/plain", ".css")
from fastapi.testclient import TestClient
from backend.main import app
client = TestClient(app, base_url="http://127.0.0.1:8420")
print(client.get("/static/assets/js/validator.js").headers["content-type"])
print(client.get("/static/assets/css/validator.css").headers["content-type"])
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=FRONTEND_DIR.parent, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    js, css = result.stdout.splitlines()[-2:]
    assert js.startswith("text/javascript") and css.startswith("text/css"), result.stdout


def test_favicon_is_a_local_self_contained_svg(served_html):
    assert '<link rel="icon" type="image/svg+xml" href="/static/assets/favicon.svg" />' in served_html
    response = TestClient(app, base_url="http://127.0.0.1:8420").get("/static/assets/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    svg = response.text.replace('xmlns="http://www.w3.org/2000/svg"', "")  # namespace, not a resource
    # No script, external reference or embedded resource: safe under the page CSP.
    for forbidden in ("<script", "href=", "http://", "https://", "url(", "@import", "onload", "<foreignObject"):
        assert forbidden not in svg, forbidden


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
    # localStorage is allowed only for the appearance preference; see
    # test_browser_storage_holds_only_the_theme_preference.
    for forbidden in ("sessionStorage", "document.cookie", "indexedDB",
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


def test_browser_storage_holds_only_the_theme_preference(served_html):
    """The appearance toggle may remember Light/Dark/System, and the update notice the
    version it was dismissed for and the version being installed. Nothing else on the
    page may touch browser storage: no session token, credentials or connector state."""
    uses = re.findall(r"localStorage\.(\w+)\(([^)]*)\)", served_html)
    assert uses, "theme preference is expected to use localStorage"
    assert served_html.count("localStorage") == len(uses)  # no other kind of access
    for method, args in uses:
        assert method in ("getItem", "setItem", "removeItem"), method
        assert args.split(",")[0].strip() in ("'mdv-theme'", "'mdv-update-dismissed'",
                                              "'mdv-update-pending'"), args
    # Only saveTheme, saveUpdateDismissal and saveUpdatePending write, and only a checked value.
    writes = [args for method, args in uses if method == "setItem"]
    assert writes == ["'mdv-theme', choice", "'mdv-update-dismissed', version",
                      "'mdv-update-pending', version"]
    assert "if (!UPDATE_VERSION.test(version)) return;" in _function_body(served_html, "saveUpdatePending")
    save = _function_body(served_html, "saveTheme")
    assert "if (!THEMES.includes(choice)) return;" in save
    assert "const THEMES = ['light', 'dark', 'system'];" in served_html
    dismissal = _function_body(served_html, "saveUpdateDismissal")
    assert "if (!UPDATE_VERSION.test(version)) return;" in dismissal
    assert ("const UPDATE_VERSION = /^(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3})$/;"
            in served_html)
    # The theme and dismissal code never sees credentials, the session or the network.
    for name in ("saveTheme", "readTheme", "applyTheme", "saveUpdateDismissal", "readUpdateDismissal",
                 "saveUpdatePending", "readUpdatePending", "clearUpdatePending"):
        body = _function_body(served_html, name)
        for secret in ("password", "username", "credentials", "SessionToken", "sessionToken",
                       "pairing", "fetch", "CONNECTOR_URL", "state."):
            assert secret not in body, (name, secret)


def test_served_page_keeps_database_list_limited_warning(served_html):
    assert 'id="discoveryWarning"' in served_html
    assert "Database list is limited. Not all visible databases are shown." in served_html
    discover = _function_body(served_html, "discoverDatabases")
    assert "data.complete === false" in discover
    assert "$('discoveryWarning').hidden = false" in discover


def test_served_page_keeps_readable_report_labels(served_html):
    # The label covers a connected database whose evidence failed too, so it never
    # says the database itself was unavailable.
    assert "'Static + database evidence unavailable'" in served_html
    assert "'Static + database unavailable'" not in served_html
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
    ("-- only a comment", "no_statements"),
    ("DELIMITER //", "delimiter_not_supported"),
    ("CREATE PROCEDURE p() BEGIN DELETE FROM t; END", "stored_program_not_supported"),
    ("SELECT 1; /*!50000 DELETE FROM t */", "executable_comment_not_supported"),
], ids=["statement_limit", "oversized_sql", "no_statements", "delimiter", "stored_program",
        "executable_comment"])
def test_local_limit_errors_returned_by_api_have_friendly_page_messages(served_html, sql, code):
    response = TestClient(app, base_url="http://127.0.0.1:8420").post("/api/validate", json={"sql": sql})
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


def test_database_validation_failed_has_friendly_message(served_html):
    connector_map = served_html[served_html.index("const CONNECTOR_ERROR_MESSAGES = {"):]
    block = connector_map[:connector_map.index("};")]
    line = next(line.strip() for line in block.splitlines()
                 if line.strip().startswith("database_validation_failed: "))
    # A fixed string literal: nothing from the response is interpolated.
    assert re.fullmatch(r"\w+: '(?:[^'\\]|\\')+',?", line), line


def test_batch_contract_errors_have_the_same_message_locally_and_remotely(served_html):
    def message(map_name, code):
        mapping = served_html[served_html.index(f"const {map_name} = {{"):]
        line = next(line for line in mapping[:mapping.index("};")].splitlines()
                    if line.strip().startswith(code + ": '"))
        return line.strip()[len(code) + 3:].rstrip("',")

    for code in ("no_statements", "delimiter_not_supported", "stored_program_not_supported",
                 "executable_comment_not_supported"):
        local = message("RESPONSE_ERROR_MESSAGES", code)
        assert local == message("CONNECTOR_ERROR_MESSAGES", code)
        # Nothing was assessed: no risk level is implied.
        assert "LOW" not in local and "risk" not in local.lower()
    assert "Nothing was assessed." in message("RESPONSE_ERROR_MESSAGES", "no_statements")
    assert "not analysed" in message("RESPONSE_ERROR_MESSAGES", "stored_program_not_supported")
    assert "nothing was assessed" in message("RESPONSE_ERROR_MESSAGES", "executable_comment_not_supported")


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


def test_page_states_the_credential_flow_without_overclaiming(served_html):
    text = " ".join(served_html.split())
    assert ("Your database login is used for connection, database evidence and "
            "metadata during validation. Credentials are not saved.") in text
    # The UI makes no claim about the database session's read-only state.
    for claim in ("read-only session", "read only session", "read-only transaction", "read only transaction"):
        assert claim not in text.lower(), claim


def test_every_unavailable_evidence_status_has_a_fixed_actionable_message(served_html):
    block = served_html[served_html.index("const EVIDENCE_STATUS_MESSAGES = {"):]
    block = block[:block.index("};")]
    messages = dict(re.findall(r"\n\t+(\w+): '([^']+)'", block))
    expected = set(EVIDENCE_STATUSES) - {"available"}
    assert set(messages) == expected
    # Fixed text only: nothing interpolated from the response.
    assert "+" not in block and "${" not in block
    # Messages are looked up by the stable codes in the evidence state, never by raw errors.
    assert "Object.prototype.hasOwnProperty.call(messages, code)" in _function_body(served_html, "fixedMessage")
    layer = _function_body(served_html, "evidenceLayer")
    assert "fixedMessage(EVIDENCE_STATUS_MESSAGES, state.connection.reason)" in layer
    assert "evidence.error" not in layer and "evidence.status" not in layer
    assert "fixedMessage(EVIDENCE_STATUS_MESSAGES, plan.reason)" in _function_body(served_html, "planLine")


def test_every_metadata_failure_reason_has_its_own_fixed_message(served_html):
    """Metadata failures are named (timeout, permission, ...), never a generic 'Partial'."""
    from backend.evidence_state import NO_DATABASE_SELECTED

    block = served_html[served_html.index("const METADATA_REASON_MESSAGES = {"):]
    block = block[:block.index("};")]
    messages = dict(re.findall(r"\n\t+(\w+): '([^']+)'", block))
    assert set(messages) == {"timeout", "permission_denied", "budget_exceeded", "invalid_reference",
                             "unavailable", NO_DATABASE_SELECTED}
    assert len(set(messages.values())) == len(messages)
    assert "+" not in block and "${" not in block
    assert "Partial'" not in _function_body(served_html, "metadataStatusLabel")


def test_sql_editor_has_an_accessible_name(served_html):
    match = re.search(r'<textarea id="sqlInput"[^>]*aria-labelledby="([^"]+)"', served_html)
    assert match, "sqlInput has no aria-labelledby"
    assert f'id="{match.group(1)}"' in served_html


def test_default_local_page_request_is_accepted_by_api():
    """The page's default (untouched form) local request must validate statically."""
    client = TestClient(app, base_url="http://127.0.0.1:8420")
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


# ---------------------------------------------------------------- connection errors and session expiry


def _connector_messages(served_html):
    block = served_html[served_html.index("const CONNECTOR_ERROR_MESSAGES = {"):]
    return block[:block.index("};")]


def test_every_company_connection_code_has_a_fixed_message(served_html):
    from connector.server import COMPANY_CONNECTION_ERROR_CODES, COMPANY_CONNECTION_FAILED

    block = _connector_messages(served_html)
    keys = set(re.findall(r"\n\t+(\w+): ", block))
    assert COMPANY_CONNECTION_ERROR_CODES | {COMPANY_CONNECTION_FAILED} <= keys
    for code in COMPANY_CONNECTION_ERROR_CODES | {COMPANY_CONNECTION_FAILED}:
        line = next(line.strip() for line in block.splitlines() if line.strip().startswith(code + ": "))
        # A fixed string literal: nothing from the response is interpolated.
        assert re.fullmatch(r"\w+: '(?:[^'\\]|\\')+',?", line), line
    # The page never shows a raw code for these: unknown codes fall back to the message.
    assert "CONNECTOR_ERROR_MESSAGES[error.errorCode]" in _function_body(served_html, "connectorRequest")


def test_session_expiry_notice_uses_the_connector_expiry_and_never_renews(served_html):
    pair = _function_body(served_html, "pairConnector")
    assert "scheduleSessionExpiryNotice(data.expires_in_seconds);" in pair
    assert pair.index("setPaired(data.session_token") < pair.index("scheduleSessionExpiryNotice(")
    notice = _function_body(served_html, "scheduleSessionExpiryNotice")
    assert "Number(expiresInSeconds)" in notice and "setTimeout(" in notice
    assert "setPaired(null, CONNECTOR_ERROR_MESSAGES.session_expired, 'error')" in notice
    # No request, re-pairing or renewal from the timer.
    for forbidden in ("connectorRequest", "fetch", "/pair", "pairConnector", "setInterval"):
        assert forbidden not in notice, forbidden
    # Timers belong to one session: cleared whenever the session changes.
    paired = _function_body(served_html, "setPaired")
    assert "state.sessionTimers.forEach(clearTimeout);" in paired
    assert "PAIR_AGAIN_GUIDANCE" in _connector_messages(served_html).split("session_expired: ")[1].splitlines()[0]


def test_only_the_target_id_is_remembered_across_re_pairing(served_html):
    assignments = re.findall(r"state\.rememberedTargetId = ([^;}\n]+?)\s*[;}\n]", served_html)
    assert assignments and set(assignments) <= {"null", "state.remoteTarget.target_id"}
    paired = _function_body(served_html, "setPaired")
    # Remembered before the existing clearing branch, which still runs unchanged.
    assert paired.index("state.rememberedTargetId = state.remoteTarget.target_id") < paired.index(
        "if (state.remoteTarget) selectTarget('');")


def test_remembered_target_is_restored_only_from_the_fresh_approved_list(served_html):
    load = _function_body(served_html, "loadApprovedTargets")
    restore = load[load.index("else if (state.rememberedTargetId) {"):]
    assert "state.rememberedTargetId = null;" in restore  # used once
    guard = "if (state.approvedTargets.some(t => t.target_id === remembered)) selectTarget(remembered);"
    assert guard in restore
    assert load.index("state.approvedTargets = (data") < load.index(guard)
    # selectTarget clears discovered databases: the new session must discover again.
    assert "clearDiscoveredDatabases();" in _function_body(served_html, "selectTarget")


def test_target_request_goes_only_to_the_connector_without_a_login(served_html):
    request = _function_body(served_html, "requestTarget")
    assert "connectorRequest(CONNECTOR_URL + '/company/target-requests'" in request
    for forbidden in ("username", "password", "state.credentials", "/api/", "requestJson", "localStorage"):
        assert forbidden not in request, forbidden
    # Only the fields the connector accepts; the CA file is read into the body, not kept.
    assert "display_name:" in request and "host:" in request and "port:" in request and "trust:" in request
    assert "body.ca_pem = await file.text();" in request
    assert "state.targetRequest = { requestId: data.request_id, sessionToken: sessionToken };" in request


def test_approval_code_is_sent_in_the_body_of_the_same_session_only(served_html):
    confirm = _function_body(served_html, "confirmTarget")
    assert "connectorRequest(CONNECTOR_URL + '/company/target-requests/confirm'" in confirm
    assert "JSON.stringify({ request_id: pending.requestId, approval_code: code })" in confirm
    assert "pending.sessionToken !== state.connectorSessionToken" in confirm
    assert "approvalCode.value = '';" in confirm  # cleared from the field at once
    assert "?" not in confirm.split("'/company/target-requests/confirm'")[0].rsplit("CONNECTOR_URL", 1)[-1]
    # The page never approves by itself: approval is only the explicit button.
    assert served_html.count("confirmTarget(") == 1  # defined, never called by the page itself
    assert "confirmTargetBtn.addEventListener('click', confirmTarget);" in served_html


def test_local_login_is_cleared_when_a_newly_approved_target_is_selected(served_html):
    """A login typed in Local mode must not be carried into the remote target that
    is selected automatically after approval (it would be sent on Discover)."""
    confirm = _function_body(served_html, "confirmTarget")
    guard = "if (!state.remoteTarget) {"
    branch = confirm[confirm.index(guard) + len(guard):]
    branch = branch[:branch.index("}")]
    assert "username.value = '';" in branch and "password.value = '';" in branch
    # Cleared before the selection, which re-reads the form (syncCredentials).
    assert confirm.index(guard) < confirm.index("selectTarget(approvedId);")
    assert "syncCredentials();" in _function_body(served_html, "selectTarget")


def test_session_change_drops_the_open_request_and_reapproval_is_explicit(served_html):
    paired = _function_body(served_html, "setPaired")
    assert "showTargetRequest(false);" in paired and "addTarget.hidden = !state.connectorSessionToken;" in paired
    show = _function_body(served_html, "showTargetRequest")
    assert "state.targetRequest = null;" in show and "approvalCode.value = '';" in show
    request = _function_body(served_html, "connectorRequest")
    assert "REAPPROVAL_CODES.includes(error.errorCode)) reapproveBtn.hidden = false;" in request
    assert "requestTarget(state.remoteTarget.target_id)" in served_html  # only from the button
    assert "reapproveBtn.hidden = true;" in _function_body(served_html, "selectTarget")


def test_every_onboarding_code_has_a_fixed_message(served_html):
    block = _connector_messages(served_html)
    keys = set(re.findall(r"\n\t+(\w+): ", block))
    codes = {
        "approval_console_unavailable", "invalid_target_request", "invalid_company_target_name",
        "invalid_company_target_host", "invalid_company_target_port", "local_target_not_allowed_in_company_mode",
        "company_target_exists", "company_dns_validation_failed", "company_target_address_not_allowed",
        "tls_ca_not_configured", "trust_policy_unsupported", "trust_store_unavailable", "trust_storage_unavailable",
        "invalid_trust_policy", "invalid_ca_bundle", "ca_bundle_too_large", "ca_bundle_contains_private_key",
        "ca_bundle_has_no_ca_certificate", "target_request_limit_reached", "company_target_not_reapprovable",
        "trust_policy_unavailable", "approval_rate_limited", "target_request_not_found", "approval_code_invalid",
        "target_request_cancelled", "dns_changed_since_request", "trust_bundle_unavailable",
        "company_target_changed", "invalid_target_confirmation", "target_registry_unavailable",
        "unknown_company_target",
    }
    assert codes <= keys, codes - keys


def test_unpair_and_local_mode_forget_the_remembered_target(served_html):
    unpair = _function_body(served_html, "unpairConnector")
    assert unpair.index("setPaired(null,") < unpair.index("state.rememberedTargetId = null;")
    assert ("targetSelect.addEventListener('change', () => { forgetRememberedTarget(); "
            "selectTarget(targetSelect.value); });") in served_html
    assert "if (!state.remoteTarget) forgetRememberedTarget();" in served_html
    assert "state.rememberedTargetId = null" in _function_body(served_html, "forgetRememberedTarget")
