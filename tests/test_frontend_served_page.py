"""Regression tests for the validator page served at GET / (built from web/src).

Static checks on the TypeScript source and the built bundle. They guard against:
- serving a page whose scripts or styles are missing or not the built ones;
- remote (company) work reaching the public /api/* routes, or a typed host doing so;
- a connector session coming from anywhere but the explicit pairing action;
- browser storage holding anything but per-viewer conveniences;
- a database being used that was not discovered for this target, login and session;
- messages built from response text instead of fixed text for stable codes.
"""

import re
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from backend.db import EVIDENCE_STATUSES
from backend.main import app
from backend.version import APP_VERSION
from tests.frontend_page import (
    FRONTEND_DIR, PAGES_DIR, WEB_DIR, all_source, built_page, function_body, message_map,
    page_assets, page_bundle, source, source_files,
)

client = TestClient(app, base_url="http://127.0.0.1:8420")
CONNECTOR = source("api/connector.ts")
LOCAL = source("api/local.ts")
MESSAGES = source("api/messages.ts")
CONNECTION = source("features/connection/useConnection.ts")
STATE = source("features/connection/connectionState.ts")
REVIEW_TEXT = source("features/review/reviewText.ts")


@pytest.fixture(scope="module")
def served() -> str:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def test_root_serves_the_built_page(served):
    # The built file as it is, apart from the version on its script and stylesheet links.
    response = client.get("/")
    assert response.content.replace(f"?v={APP_VERSION}".encode(), b"") == (PAGES_DIR / "index.html").read_bytes()
    assert "<title>MySQL DBA Validator</title>" in served
    assert '<div id="root"></div>' in served


def test_page_loads_its_own_module_and_stylesheet(served):
    head = served.split("</head>")[0]
    assets = page_assets(served)
    assert [a for a in assets if a.endswith(".js")] and [a for a in assets if a.endswith(".css")]
    # The theme is set before first paint (inline), then the module and stylesheet load.
    assert head.index("localStorage.getItem('mdv-theme')") < head.index('<script type="module"')
    # One module script from the build; nothing from another origin.
    assert served.count("<script") == 2 and served.count('type="module"') == 1
    assert not re.search(r'(?:src|href)="(?:https?:)?//', served)
    for path in assets:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/javascript" if path.endswith(".js") else "text/css")


def test_the_built_page_comes_from_the_source_entry():
    entry = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/src/app/main.tsx"></script>' in entry
    # The inline theme script is the same in the source entry and the built page.
    script = entry[entry.index("<script>"):entry.index("</script>")]
    assert script in built_page("index.html")
    # No application code or styles in the HTML entries: one short inline theme script.
    for name in ("index.html", "home.html"):
        html = (WEB_DIR / name).read_text(encoding="utf-8")
        assert "<style" not in html and html.count("<script>") == 1, name
        inline = html[html.index("<script>"):html.index("</script>")]
        assert len(inline.splitlines()) < 12, name


def test_page_files_keep_their_types_when_the_system_says_text_plain():
    # On Windows, Python's mimetypes reads the registry, where some programs register
    # .js as text/plain; with nosniff a browser would then not run the page's script.
    js, css = sorted(page_assets(built_page("index.html")), key=lambda p: p.endswith(".css"))
    code = f"""
import mimetypes
mimetypes.add_type("text/plain", ".js")
mimetypes.add_type("text/plain", ".css")
from fastapi.testclient import TestClient
from backend.main import app
client = TestClient(app, base_url="http://127.0.0.1:8420")
print(client.get("{js}").headers["content-type"])
print(client.get("{css}").headers["content-type"])
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=FRONTEND_DIR.parent, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    js_type, css_type = result.stdout.splitlines()[-2:]
    assert js_type.startswith("text/javascript") and css_type.startswith("text/css"), result.stdout


def test_favicon_is_a_local_self_contained_svg(served):
    assert '<link rel="icon" type="image/svg+xml" href="/static/assets/favicon.svg" />' in served
    response = client.get("/static/assets/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    svg = response.text.replace('xmlns="http://www.w3.org/2000/svg"', "")  # namespace, not a resource
    for forbidden in ("<script", "href=", "http://", "https://", "url(", "@import", "onload", "<foreignObject"):
        assert forbidden not in svg, forbidden


def test_styles_and_fonts_are_never_data_urls_that_the_csp_refuses():
    # The CSP allows same-origin fonts only; Cloudscape's full stylesheet embeds fonts.
    bundle = page_bundle("index.html")
    assert "data:font" not in bundle and "data:application/font" not in bundle
    assert "global-styles/index-base.css" in source("app/main.tsx")
    assert "global-styles/index.css" not in all_source()


# ---------------------------------------------------------------- remote work: connector only


def test_connector_client_is_the_only_route_to_company_targets():
    assert "export const CONNECTOR_URL = 'http://127.0.0.1:8765';" in CONNECTOR
    assert "'/api/" not in CONNECTOR and "\"/api/" not in CONNECTOR
    for path in ("targetPath(targetId, 'validate')", "targetPath(targetId, 'databases')", "targetPath(targetId, route)"):
        assert path in CONNECTOR, path
    # Every connector call goes through one function that refuses any other address.
    request = function_body(CONNECTOR, "connectorRequest")
    assert "if (!url.startsWith(CONNECTOR_URL + '/')) throw" in request
    assert CONNECTOR.count("requestJson(") == 1 and CONNECTOR.count("fetch(") == 0
    # Nothing else in the page talks to the connector.
    for name, text in source_files().items():
        if name != "api/connector.ts":
            assert "8765" not in text, name


def test_public_api_is_only_called_from_the_local_client():
    for name, text in source_files().items():
        if name != "api/local.ts":
            assert "'/api/" not in text, name
    assert "CONNECTOR_URL" not in LOCAL and "from './connector'" not in LOCAL
    assert LOCAL.count("'/api/validate'") == 1
    # In the bundle the browser runs, /api/validate appears once (the local client).
    assert page_bundle("index.html").count("/api/validate") == 1


def test_local_mode_reaches_only_mysql_on_this_computer():
    env = source("shared/environment.ts")
    assert "export const LOCAL_HOST = '127.0.0.1';" in env and "export const LOCAL_PORT = 3306;" in env
    server = LOCAL[LOCAL.index("const server"):].split("\n")[0]
    assert "host: LOCAL_HOST, port: LOCAL_PORT" in server
    # There is no host or port field: a typed host can never become a destination.
    panel = source("features/connection/ConnectionPanel.tsx")
    assert "<span>Host" not in panel and "<span>Port" not in panel


def test_review_takes_the_connector_path_for_a_company_target_only():
    review = source("features/review/useReview.ts")
    start = function_body(review, "start")
    branch = start[start.index("!PUBLIC_DEMO && s.target"):]
    assert branch.index("connector.validateRemote(") < branch.index(": await validateLocal(")
    # The remote branch sends the target_id, never a host or port.
    remote = function_body(CONNECTOR, "validateRemote")
    assert "targetPath(targetId, 'validate')" in remote
    assert "host" not in remote and "port" not in remote


def test_company_operations_are_refused_before_anything_is_sent():
    guard = function_body(CONNECTION, "withTarget")
    checks = ["if (!s.target) throw", "if (!s.username || !s.password) throw", "!discoveryMatches(s)", "requireSession()"]
    positions = [guard.index(check) for check in checks]
    assert positions == sorted(positions) and positions[-1] < guard.index("await run(")


def test_connector_session_comes_only_from_explicit_pairing():
    source_text = all_source()
    assert "__CONNECTOR_PAIRING_TOKEN__" not in source_text
    for forbidden in ("sessionStorage", "document.cookie", "indexedDB", "URLSearchParams",
                      "location.hash", "location.search", "window.__"):
        assert forbidden not in source_text, forbidden
    session = function_body(CONNECTION, "requireSession")
    assert "throw new RequestError" in session and "connector." not in session and "fetch" not in session
    # The only pairing call is the explicit action, fed by the pairing-code field.
    assert source_text.count("connector.pair(") == 1
    assert "connectorRequest('/pair', 'POST', null, { pairing_token: code })" in CONNECTOR
    panel = source("features/connection/ConnectorPanel.tsx")
    submit = function_body(panel, "submitPairing")
    assert "setPairingCode('')" in submit and "actions.pair(code)" in submit
    # The token lives in the connection state only.
    assert source_text.count("sessionToken: token") >= 1
    assert "localStorage" not in CONNECTION and "readStored" not in CONNECTION and "writeStored" not in CONNECTION


def test_browser_storage_holds_only_per_viewer_conveniences():
    """Only the theme word, the ambient-light choice, the editor split and the update
    versions. No session token, login, SQL, target or database."""
    allowed = {"'mdv-theme'", "'mdv-home-theme'", "'mdv-update-dismissed'", "'mdv-update-pending'",
               "'mdv-ambient'", "'mdv-review-split'"}
    for name, text in source_files().items():
        if "localStorage" in text:
            assert name in ("shared/storage.ts", "home/home.ts"), name
    for call in re.findall(r"(?:readStored|writeStored|removeStored)\(([^,)]+)", all_source()):
        key = call.strip()
        if key in ("key",) or ":" in key:  # the helpers' own parameters
            continue
        assert key in allowed or key in ("KEY", "SPLIT_KEY", "AMBIENT_KEY", "DISMISSED", "PENDING"), key
    constants = dict(re.findall(r"const (KEY|SPLIT_KEY|AMBIENT_KEY|DISMISSED|PENDING) = ('[\w-]+');", all_source()))
    assert set(constants.values()) <= allowed, constants
    # Stored update versions are checked first.
    updates = source("app/updates.ts")
    assert "if (UPDATE_VERSION.test(version)) writeStored(key, version)" in updates
    assert ("export const UPDATE_VERSION = /^(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3})\\.(0|[1-9][0-9]{0,3})$/;"
            in updates)
    theme = source("shared/theme.ts")
    assert "value === 'light' || value === 'dark' || value === 'system'" in theme
    for text in (updates, theme, source("shared/storage.ts")):
        for secret in ("password", "username", "sessionToken", "pairing", "fetch", "CONNECTOR_URL"):
            assert secret not in text, secret


def test_database_list_limited_warning():
    panel = source("features/connection/ConnectionPanel.tsx")
    assert "Database list is limited. Not all visible databases are shown." in panel
    assert "s.verified && !s.listComplete" in panel
    assert "complete: data.status === 'success' && data.complete !== false" in LOCAL


def test_readable_report_labels():
    assert "'Static + database evidence unavailable'" in REVIEW_TEXT
    assert "'Static + database unavailable'" not in REVIEW_TEXT
    assert "'Database evidence'" in REVIEW_TEXT
    assert "'Metadata-based scoring was not applied.'" in REVIEW_TEXT
    assert "High estimated rows applied: " in source("features/review/StatementList.tsx")


# ---------------------------------------------------------------- approved targets and discovery


def test_target_selector_offers_local_and_approved_targets():
    panel = source("features/connection/ConnectionPanel.tsx")
    assert "label: 'Local (this machine)', value: ''" in panel
    assert "value: t.target_id" in panel
    targets = function_body(CONNECTOR, "approvedTargets")
    assert "connectorRequest('/company/targets', 'GET', token)" in targets
    assert ".approval_state === 'approved'" in targets and "isRemoteHost(String(" in targets
    load = function_body(CONNECTION, "loadTargets")
    assert "selectTarget('')" in load  # a selection no longer listed is dropped
    assert "await loadTargets();" in function_body(CONNECTION, "pair")


def test_remote_discovery_is_connector_only_and_bound():
    discover = function_body(CONNECTOR, "discoverRemote")
    assert "targetPath(targetId, 'databases')" in discover
    assert "{ username: login.username.trim(), password: login.password }" in discover
    connect = function_body(CONNECTION, "connect")
    remote = connect[connect.index("if (binding.target) {"):connect.index("} else {")]
    assert "connector.discoverRemote(" in remote and "discoverLocalDatabases" not in remote
    # Discarded if the target, login or session changed while it ran.
    assert "if (!same() || data.target_id !== binding.target.target_id)" in remote
    same = connect[connect.index("const same = () => {"):connect.index("const previous")]
    for check in ("target_id === binding.target?.target_id", "now.username === binding.username",
                  "now.password === binding.password", "now.sessionToken === binding.token"):
        assert check in same, check


def test_selected_database_is_bound_to_target_login_and_session():
    matches = function_body(STATE, "discoveryMatches")
    for check in ("d.targetId === s.target.target_id", "d.username === s.username",
                  "d.sessionToken === s.sessionToken", "d.databases.includes(s.database)"):
        assert check in matches, check
    assert "if (s.target) return discoveryMatches(s) ? s.database : null;" in function_body(STATE, "usableDatabase")
    # A database can only be one from the discovered list.
    assert "name && s.databases?.includes(name) ? name : null" in function_body(CONNECTION, "selectDatabase")


def test_discovery_is_cleared_on_target_login_and_session_changes():
    cleared = STATE[STATE.index("export const clearedDiscovery"):].split("};")[0]
    for field in ("verified: false", "databases: null", "database: null", "discovery: null"):
        assert field in cleared, field
    assert "...clearedDiscovery" in function_body(CONNECTION, "setLogin")
    assert "...clearedDiscovery" in function_body(CONNECTION, "selectTarget")
    paired = function_body(CONNECTION, "setPaired")
    assert "targets: []" in paired and "...clearedDiscovery" in paired
    assert "setPaired(null, NOT_PAIRED, '')" in function_body(CONNECTION, "unpair")
    assert "if (error.status === 401 && !pairing) setPaired(null," in function_body(CONNECTION, "connectorFailed")


def test_leaving_connector_mode_clears_the_company_login():
    """Found with real Chrome in the previous page: after Remote -> Local the company
    login stayed and the next Local validation posted it to the public /api/validate.
    Any change of target clears it (also Local -> company and company -> company), and
    a check still running for the old target no longer holds the Connect button."""
    select = function_body(CONNECTION, "selectTarget")
    assert "s.target?.target_id !== target?.target_id ? { username: '', password: '' } : {}" in select
    assert "busy: null" in select
    paired = function_body(CONNECTION, "setPaired")
    assert "if (s.target) Object.assign(change, { target: null, username: '', password: ''" in paired


def test_a_late_connector_answer_only_affects_the_session_and_target_it_was_for():
    """A 401 from an earlier session never ends the current one; re-approval is offered
    only for the target that was asked; a late discovery failure leaves the current
    state alone; pairing does not cancel a Local check; a company review's result is
    stale once its login changes (the login is always sent to the connector)."""
    failed = function_body(CONNECTION, "connectorFailed")
    assert "const current = !sent || sent.token === now.sessionToken;" in failed
    assert "if (error instanceof RequestError && current) {" in failed
    assert "if (sameTarget && ref.current.target" in failed
    connect = function_body(CONNECTION, "connect")
    assert "(!binding.target || now.sessionToken === binding.token)" in connect
    assert ("if (!same()) {\n\t\t\t\tif (binding.target) connectorFailed(error, false, "
            "{ token: binding.token, targetId: binding.target.target_id });\n\t\t\t\treturn;") in connect
    assert ("connectorFailed(error, false, { token, targetId: s.target.target_id })"
            in function_body(CONNECTION, "withTarget"))
    review = source("features/review/useReview.ts")
    assert "s.target || s.verified ? s.username : ''" in review
    # A password-only edit also makes a company result stale: every login edit is
    # counted, and the count (never the password) is part of a company review's context.
    assert "loginRevision: s.loginRevision + 1" in function_body(CONNECTION, "setLogin")
    assert "loginRevision: 0" in source("features/connection/connectionState.ts")
    context = review[review.index("export const reviewContext"):review.index("export function useReview")]
    assert "s.target ? s.loginRevision : ''" in context
    assert "password" not in context


def test_local_login_is_cleared_when_a_newly_approved_target_is_selected():
    confirm = function_body(CONNECTION, "confirmTarget")
    clear = "if (!ref.current.target) update({ username: '', password: '' });"
    assert clear in confirm and confirm.index(clear) < confirm.index("selectTarget(approvedId);")


def test_connect_button_is_never_left_disabled():
    panel = source("features/connection/ConnectionPanel.tsx")
    assert "disabled={!s.username || !s.password || busy}" in panel
    connect = function_body(CONNECTION, "connect")
    # Every way out of a connection attempt clears the busy state.
    assert connect.count("busy: null") >= 2


def test_page_states_the_credential_flow_without_overclaiming():
    text = " ".join(all_source().split())
    assert "the login is sent with each request and never saved." in text
    for claim in ("read-only session", "read only session", "read-only transaction", "read only transaction"):
        assert claim not in text.lower(), claim


# ---------------------------------------------------------------- fixed messages


@pytest.mark.parametrize("sql, code", [
    (";".join(["SELECT 1"] * 17), "statement_limit_exceeded"),
    ("SELECT 1 -- " + "x" * (65 * 1024), "request_too_large"),
    ("-- only a comment", "no_statements"),
    ("DELIMITER //", "delimiter_not_supported"),
    ("CREATE PROCEDURE p() BEGIN DELETE FROM t; END", "stored_program_not_supported"),
    ("SELECT 1; /*!50000 DELETE FROM t */", "executable_comment_not_supported"),
], ids=["statement_limit", "oversized_sql", "no_statements", "delimiter", "stored_program",
        "executable_comment"])
def test_local_limit_errors_returned_by_api_have_friendly_page_messages(sql, code):
    response = client.post("/api/validate", json={"sql": sql})
    assert response.status_code == 200 and response.json().get("error") == code
    messages = {**message_map(MESSAGES, "LIMITS"), **message_map(MESSAGES, "RESPONSE_ERROR_MESSAGES")}
    assert code in messages
    # The local client turns a 200 {"error": code} into that message, not "unexpected response".
    assert "responseMessage(data, RESPONSE_ERROR_MESSAGES, 'Validation returned an unexpected response.')" in LOCAL
    resolve = function_body(source("api/http.ts"), "responseMessage")
    assert "typeof data.message === 'string'" in resolve  # explicit messages still win
    assert "data.error_code" in resolve and "data.error" in resolve


def test_batch_contract_errors_have_the_same_message_locally_and_remotely():
    # Both maps take these from the one LIMITS block.
    assert MESSAGES.count("...LIMITS,") == 2
    limits = message_map(MESSAGES, "LIMITS")
    for code in ("statement_limit_exceeded", "no_statements", "delimiter_not_supported",
                 "stored_program_not_supported", "executable_comment_not_supported"):
        assert "LOW" not in limits[code] and "risk" not in limits[code].lower()
    assert "Nothing was assessed." in limits["no_statements"]
    assert "not analysed" in limits["stored_program_not_supported"]
    assert "nothing was assessed" in limits["executable_comment_not_supported"]


def test_every_company_connection_code_has_a_fixed_message():
    from connector.server import COMPANY_CONNECTION_ERROR_CODES, COMPANY_CONNECTION_FAILED

    messages = message_map(MESSAGES, "CONNECTOR_ERROR_MESSAGES")
    assert COMPANY_CONNECTION_ERROR_CODES | {COMPANY_CONNECTION_FAILED} <= set(messages)
    assert "database_validation_failed" in messages and "statement_limit_exceeded" in message_map(MESSAGES, "LIMITS")
    # Unknown codes fall back to the connector's own message, never a raw code.
    assert "Object.hasOwn(CONNECTOR_ERROR_MESSAGES, error.code)" in function_body(CONNECTOR, "connectorRequest")


def test_every_onboarding_code_has_a_fixed_message():
    keys = set(message_map(MESSAGES, "CONNECTOR_ERROR_MESSAGES"))
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


def test_message_maps_are_fixed_text():
    for name in ("LIMITS", "RESPONSE_ERROR_MESSAGES", "CONNECTOR_ERROR_MESSAGES"):
        block = MESSAGES[MESSAGES.index(name):]
        block = block[:block.index("};")]
        assert "${" not in block and "`" not in block, name
    # The only computed parts are the fixed guidance and the page's own origin.
    computed = re.findall(r"'\s*\+\s*([\w.]+)", MESSAGES)
    assert set(computed) <= {"PAIR_AGAIN_GUIDANCE", "window.location.origin"}, computed


def test_every_unavailable_evidence_status_has_a_fixed_actionable_message():
    messages = message_map(REVIEW_TEXT, "EVIDENCE_STATUS_MESSAGES")
    assert set(messages) == set(EVIDENCE_STATUSES) - {"available"}
    block = REVIEW_TEXT[REVIEW_TEXT.index("const EVIDENCE_STATUS_MESSAGES"):]
    assert "+" not in block[:block.index("};")]
    # Looked up by the stable codes in the evidence state, never from raw errors.
    assert "Object.hasOwn(messages, code)" in REVIEW_TEXT
    view = function_body(REVIEW_TEXT, "evidenceView")
    assert "evidenceStatusMessage(state.connection.reason)" in view
    assert "evidence.status" not in view and "evidence.message" not in view
    assert "evidenceStatusMessage(plan.reason)" in function_body(REVIEW_TEXT, "planLine")


def test_every_metadata_failure_reason_has_its_own_fixed_message():
    from backend.evidence_state import NO_DATABASE_SELECTED

    messages = message_map(REVIEW_TEXT, "METADATA_REASON_MESSAGES")
    assert set(messages) == {"timeout", "permission_denied", "budget_exceeded", "invalid_reference",
                             "unavailable", NO_DATABASE_SELECTED}
    assert len(set(messages.values())) == len(messages)
    assert "Partial'" not in function_body(REVIEW_TEXT, "metadataStatusLabel")


def test_sql_editor_has_an_accessible_name():
    editor = source("features/review/SqlEditor.tsx")
    assert 'id="sqlInput"' in editor and 'aria-labelledby="sqlLabel"' in editor and 'id="sqlLabel"' in editor


def test_default_local_page_requests_are_accepted_by_api():
    """What the page sends: the SQL alone (static), or with the fixed local server."""
    for body in ({"sql": "SELECT * FROM users;"},
                 {"sql": "SELECT * FROM users;", "host": "127.0.0.1", "port": 3306, "username": "", "password": ""}):
        response = client.post("/api/validate", json=body)
        assert response.status_code == 200
        assert response.json()["statement_count"] == 1


# ---------------------------------------------------------------- session expiry and target onboarding


def test_session_expiry_notice_uses_the_connector_expiry_and_never_renews():
    pair = function_body(CONNECTION, "pair")
    assert pair.index("setPaired(session.token") < pair.index("const seconds = session.expiresInSeconds;")
    timers = pair[pair.index("const seconds"):pair.index("await loadTargets();")]
    assert "setPaired(null, CONNECTOR_ERROR_MESSAGES.session_expired!, 'error')" in timers
    for forbidden in ("connector.", "fetch", "setInterval", "pair("):
        assert forbidden not in timers, forbidden
    paired = function_body(CONNECTION, "setPaired")
    assert "timers.current.forEach(clearTimeout);" in paired
    assert "'Connector session has expired. ' + PAIR_AGAIN_GUIDANCE" in MESSAGES


def test_only_the_target_id_is_remembered_across_re_pairing():
    assignments = re.findall(r"rememberedTargetId: ([^,}\n]+)", CONNECTION)
    assert assignments
    allowed = {"null", "s.target ? s.target.target_id : s.rememberedTargetId", "s.target ? s.rememberedTargetId : null"}
    assert {a.strip() for a in assignments} <= allowed, assignments


def test_remembered_target_is_restored_only_from_the_fresh_approved_list():
    load = function_body(CONNECTION, "loadTargets")
    restore = load[load.index("else if (!s.target && s.rememberedTargetId)"):]
    assert "update({ rememberedTargetId: null });" in restore  # used once
    assert "if (targets.some(t => t.target_id === remembered)) selectTarget(remembered);" in restore
    assert "...clearedDiscovery" in function_body(CONNECTION, "selectTarget")


def test_unpair_and_local_mode_forget_the_remembered_target():
    unpair = function_body(CONNECTION, "unpair")
    assert unpair.index("setPaired(null,") < unpair.index("update({ rememberedTargetId: null });")
    assert "selectTarget: (id: string) => { update({ rememberedTargetId: null }); selectTarget(id); }" in CONNECTION
    assert "rememberedTargetId: s.target ? s.rememberedTargetId : null" in function_body(CONNECTION, "setLogin")


def test_target_request_goes_only_to_the_connector_without_a_login():
    request = function_body(CONNECTION, "requestTarget")
    for forbidden in ("username", "password", "/api/", "readStored", "writeStored"):
        assert forbidden not in request, forbidden
    assert "display_name:" in request and "host:" in request and "port:" in request and "trust:" in request
    assert "body.ca_pem = await details.caFile.text();" in request
    assert "details.caFile.size > MAX_CA_BUNDLE_BYTES" in request
    assert "connectorRequest('/company/target-requests', 'POST', token, body)" in CONNECTOR


def test_approval_code_is_sent_in_the_body_of_the_same_session_only():
    confirm = function_body(CONNECTION, "confirmTarget")
    assert "pending.sessionToken !== ref.current.sessionToken" in confirm
    assert ("connectorRequest('/company/target-requests/confirm', 'POST', token,\n\t\t{ request_id: requestId, "
            "approval_code: code })") in CONNECTOR
    panel = source("features/connection/ConnectorPanel.tsx")
    submit = function_body(panel, "submitApproval")
    assert "setApprovalCode('')" in submit  # cleared from the field at once
    # Approval is only the explicit action.
    assert all_source().count("actions.confirmTarget(") == 1


def test_session_change_drops_the_open_request_and_reapproval_is_explicit():
    paired = function_body(CONNECTION, "setPaired")
    assert "targetRequest: null, reapprovable: false" in paired
    failed = function_body(CONNECTION, "connectorFailed")
    assert "REAPPROVAL_CODES.includes(error.code)) update({ reapprovable: true })" in failed
    assert all_source().count("requestTarget('reapprove')") == 1  # only from the button
    assert "reapprovable: false" in function_body(CONNECTION, "selectTarget")


def test_the_online_demo_never_reaches_connection_or_diagnostics_code():
    app_source = source("app/App.tsx")
    assert "PUBLIC_DEMO ? ['dashboard', 'review'] :" in app_source
    assert "{!PUBLIC_DEMO && <UpdateNotice />}" in app_source and "useHeartbeat(!PUBLIC_DEMO)" in app_source
    review = source("features/review/ReviewView.tsx")
    assert "{PUBLIC_DEMO\n\t\t\t\t\t\t? <p className=\"panel demo-note\">" in review
    assert "const body: Json = PUBLIC_DEMO || !login ? { sql } : { sql, ...server(login) };" in LOCAL
    assert "if (!PUBLIC_DEMO && login && database) body.database = database;" in LOCAL
