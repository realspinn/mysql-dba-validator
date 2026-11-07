// Target-driven state: backend determines security, not frontend mode selection.
// Remote (non-loopback) targets are only ever sent to the local connector, never to the public /api/* routes.
const CONNECTOR_URL = 'http://127.0.0.1:8765';
// The hosted online demo (the server sets data-public-demo): static analysis only.
// The page sends only the SQL text and never contacts a connector; the server
// refuses anything else regardless.
const PUBLIC_DEMO = document.documentElement.hasAttribute('data-public-demo');
const NO_DATABASE = PUBLIC_DEMO ? 'the online demo does not connect to databases.' : 'no database login and database.';
const REMOTE_NOT_AVAILABLE = 'Remote targets are handled only through the local connector. ';

// DOM elements
const $ = id => document.getElementById(id);
const host = $('host'), port = $('port'), username = $('username'), password = $('password');
const database = $('database');
const testBtn = $('testBtn'), discoverBtn = $('discoverBtn'), validateBtn = $('validateBtn');
const sqlInput = $('sqlInput');

function readCredentialsFromForm() {
	return {
		host: host.value.trim(),
		port: Number(port.value),
		username: username.value.trim(),
		password: password.value
	};
}

const state = {
	// Initialised from the form so the default loopback host is used before any input event fires.
	credentials: readCredentialsFromForm(),
	tested: false,
	databases: [],
	selectedDatabase: '',
	// Connector session token: obtained only by the explicit "Pair connector" action,
	// held in memory only (never storage, cookies, or URLs). Until it exists, remote
	// validation fails closed before any credentials leave the page.
	connectorSessionToken: null,
	// Approved targets as listed by the connector for this session. A remote
	// target is referenced only by its target_id; the host/port shown in the form
	// are display-only and never sent anywhere.
	approvedTargets: [],
	remoteTarget: null,
	// target_id selected when the last connector session ended (memory only). It is
	// offered again after re-pairing if still approved; the login is never kept.
	rememberedTargetId: null,
	// Expiry notice timers for the current session, from the connector's expires_in_seconds.
	sessionTimers: [],
	// The open target request ({requestId, sessionToken}); memory only, one at a time.
	targetRequest: null,
	// Which target/login/session produced the current remote database list. A
	// database is only sent with a remote validation while all three still match;
	// the connector re-checks the same binding server-side.
	discovery: null
};
const pairingCode = $('pairingCode'), pairBtn = $('pairBtn'), unpairBtn = $('unpairBtn');
const targetSelect = $('targetSelect'), loadTargetsBtn = $('loadTargetsBtn'), reapproveBtn = $('reapproveBtn');
const addTarget = $('addTarget'), newTargetTrust = $('newTargetTrust'), newTargetCa = $('newTargetCa');
const approvalCode = $('approvalCode'), requestTargetBtn = $('requestTargetBtn'), confirmTargetBtn = $('confirmTargetBtn');

// Utility functions
function text(value) { return value == null || value === '' ? 'n/a' : String(value) }
function setStatus(node, message, kind) { node.textContent = message; node.className = 'status' + (kind ? ' ' + kind : '') }
// The public API reports some validation limits as {"error": code} without a message.
const RESPONSE_ERROR_MESSAGES = {
	statement_limit_exceeded: 'Too many statements. At most 16 statements can be validated at once.',
	no_statements: 'No SQL statements found: the input is empty or contains only comments. Nothing was assessed.',
	delimiter_not_supported: 'DELIMITER is a mysql client command, not SQL, and is not supported. Stored program definitions are not analysed; validate the statements in the body separately, ending each with a semicolon.',
	stored_program_not_supported: 'A stored program definition (CREATE PROCEDURE, FUNCTION, TRIGGER or EVENT) cannot be split into statements reliably when its body has several statements or other statements are submitted with it. Stored programs are not analysed; validate the statements in the body separately.',
	executable_comment_not_supported: 'The SQL contains an executable comment (/*! ... */). MySQL runs the SQL inside such a comment, but the validator cannot analyse it, so nothing was assessed. Move any SQL out of the comment, or remove the comment, and validate again.',
	request_too_large: 'The SQL is too large. Validation is limited to 64 KiB.',
	sql_too_complex: 'The SQL is too deeply nested, or has too long an expression, to analyse. Nothing was assessed. Split it into smaller statements and validate again.',
	server_busy: 'The online demo is busy. Wait a few seconds and validate again.'
};
function errorMessage(data, fallback) {
	if (data && typeof data.message === 'string') return data.message;
	const code = data && (typeof data.error_code === 'string' ? data.error_code : typeof data.error === 'string' ? data.error : null);
	return (code && RESPONSE_ERROR_MESSAGES[code]) || fallback;
}

// Sync credentials from form inputs into state
function syncCredentials() {
	const changed = state.credentials.host !== host.value.trim() ||
		state.credentials.port !== Number(port.value) ||
		state.credentials.username !== username.value.trim() ||
		state.credentials.password !== password.value;

	state.credentials = readCredentialsFromForm();

	if (changed) {
		clearDiscoveredDatabases();
		setStatus($('connectionStatus'), 'Connection details changed; test again.', '');
		setStatus($('discoveryStatus'), 'Test the updated connection before discovering databases.', '');
	}
}

// Drops every discovered database and the selection (target, login, or session changed).
function clearDiscoveredDatabases() {
	state.tested = false;
	state.databases = [];
	state.selectedDatabase = '';
	state.discovery = null;
	database.replaceChildren(new Option('Select a database', ''));
	database.disabled = true;
	$('discoveryWarning').hidden = true;
	updateContext();
}

function validConnection() {
	return state.credentials.host && state.credentials.username && state.credentials.password &&
		Number.isInteger(state.credentials.port) && state.credentials.port >= 1 && state.credentials.port <= 65535;
}

async function requestJson(url, options) {
	const response = await fetch(url, options);
	let data = null;
	try { data = await response.json() } catch (_) { data = null }
	if (!response.ok) {
		const error = new Error(errorMessage(data, 'The server could not complete this request.'));
		error.status = response.status;
		error.errorCode = data && typeof data.error_code === 'string' ? data.error_code : null;
		throw error;
	}
	return data;
}

async function testConnection() {
	syncCredentials();
	if (!validConnection()) {
		setStatus($('connectionStatus'), 'Enter host, port, username, and password.', 'error');
		return;
	}

	if (isRemoteTarget(state.credentials.host)) {
		// Never send remote credentials to the public API. Browser connector sessions
		// cannot open bare connections; discovery is the login check for remote targets.
		state.tested = false;
		setStatus($('connectionStatus'), state.remoteTarget
			? 'For connector targets, use "Discover databases" to check your MySQL login.'
			: REMOTE_NOT_AVAILABLE + 'Select an approved connector target instead of typing a remote host.',
			state.remoteTarget ? '' : 'error');
		updateStepper();
		return;
	}

	testBtn.disabled = true;
	discoverBtn.disabled = true;
	testBtn.textContent = 'Testing...';
	testBtn.setAttribute('aria-busy', 'true');
	setStatus($('connectionStatus'), 'Testing connection...', '');

	try {
		const data = await requestJson('/api/connections/test', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(state.credentials)
		});

		if (data.status !== 'success' || data.connected !== true)
			throw new Error(errorMessage(data, 'Unable to connect to the database.'));

		state.tested = true;
		setStatus($('connectionStatus'), 'Connection configured.', 'success');
		setStatus($('discoveryStatus'), 'Ready to discover visible databases.', 'success');
	} catch (error) {
		state.tested = false;
		setStatus($('connectionStatus'), error.message || 'Unable to connect to the database.', 'error');
	} finally {
		testBtn.disabled = false;
		discoverBtn.disabled = false; // re-enable always; the click handler validates input
		testBtn.textContent = 'Test connection';
		testBtn.removeAttribute('aria-busy');
		updateStepper();
	}
}

async function discoverDatabases() {
	syncCredentials();
	if (!validConnection()) {
		setStatus($('discoveryStatus'), 'Enter valid connection details first.', 'error');
		return;
	}

	if (isRemoteTarget(state.credentials.host)) {
		if (!state.remoteTarget) {
			// A typed remote host is never a destination: refuse locally, send nothing.
			$('discoveryWarning').hidden = true;
			setStatus($('discoveryStatus'),
				REMOTE_NOT_AVAILABLE + 'Select an approved connector target instead of typing a remote host.',
				'error');
			updateStepper();
			return;
		}
		// Otherwise continue: discovery goes to the connector by target_id (below).
	}

	discoverBtn.disabled = true;
	testBtn.disabled = true;
	discoverBtn.textContent = 'Discovering...';
	discoverBtn.setAttribute('aria-busy', 'true');
	setStatus($('discoveryStatus'), 'Reading visible database names...', '');
	$('discoveryWarning').hidden = true;

	// Remote: connector only, by target_id. Local: public API (unchanged).
	const remoteTarget = state.remoteTarget;
	const binding = remoteTarget ? {
		targetId: remoteTarget.target_id,
		username: state.credentials.username,
		password: state.credentials.password,
		sessionToken: state.connectorSessionToken
	} : null;

	try {
		const data = remoteTarget
			? await discoverRemote(remoteTarget)
			: await requestJson('/api/connections/discover-databases', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(state.credentials)
			});

		if (!data || !Array.isArray(data.databases) || (data.status !== 'success' && data.status !== 'limited'))
			throw new Error(errorMessage(data, 'Unable to discover databases.'));

		if (binding) {
			// Target, login or session changed while discovery ran: discard the result.
			if (!state.remoteTarget || state.remoteTarget.target_id !== binding.targetId ||
				state.credentials.username !== binding.username || state.credentials.password !== binding.password ||
				state.connectorSessionToken !== binding.sessionToken || data.target_id !== binding.targetId)
				throw new Error('Target, login, or connector session changed during discovery. Discover again.');
			state.discovery = {
				targetId: binding.targetId,
				username: binding.username,
				sessionToken: binding.sessionToken,
				databases: data.databases.map(String)
			};
			state.tested = true;
		}

		const previous = state.selectedDatabase;
		state.databases = data.databases;
		database.replaceChildren(new Option('Select a database', ''));
		data.databases.forEach(name => database.add(new Option(String(name), String(name))));
		database.disabled = false;
		database.value = data.databases.includes(previous) ? previous : '';
		state.selectedDatabase = database.value;

		setStatus($('discoveryStatus'),
			data.status === 'limited' ? 'Database list is limited.' : data.databases.length + ' database(s) discovered.',
			data.status === 'limited' ? '' : 'success');

		// Safety-relevant: make it obvious when the database list is incomplete.
		if (data.status === 'limited' || data.complete === false) {
			$('discoveryWarning').textContent = 'Database list is limited. Not all visible databases are shown.';
			$('discoveryWarning').hidden = false;
		}

		updateContext();
	} catch (error) {
		// A failed remote discovery also clears the connector's state for this session,
		// so drop the local list too rather than offer databases the connector will refuse.
		if (binding) clearDiscoveredDatabases();
		setStatus($('discoveryStatus'), error.message || 'Unable to discover databases.', 'error');
	} finally {
		testBtn.disabled = false;
		discoverBtn.disabled = false; // re-enable always; the click handler validates input
		discoverBtn.textContent = 'Discover databases';
		discoverBtn.removeAttribute('aria-busy');
		updateStepper();
	}
}

// Any non-empty host that is not an explicit loopback name is treated as remote,
// regardless of port, so remote credentials can never fall through to the public API.
// The backend remains authoritative for classification; this check only decides routing.
function isRemoteTarget(hostname) {
	if (!hostname || !hostname.trim()) return false;
	const normalized = hostname.trim().toLowerCase();
	return !(normalized === 'localhost' || normalized === '127.0.0.1' || normalized === '::1');
}

// The browser sends the Origin header itself; it cannot be set from script.
function connectorHeaders(sessionToken, withBody) {
	const headers = { 'Authorization': 'Bearer ' + sessionToken };
	if (withBody) headers['Content-Type'] = 'application/json';
	return headers;
}

// Never pairs implicitly: the session comes only from the explicit pairing action.
// Without one, remote validation stops here, before any credentials are sent anywhere.
async function ensureConnectorSession() {
	if (state.connectorSessionToken) return state.connectorSessionToken;
	throw new Error(REMOTE_NOT_AVAILABLE + 'Pair with the local connector first using the pairing code it printed.');
}

const PAIR_AGAIN_GUIDANCE = 'Press Enter in the connector window for a new pairing code, then pair again.';
const CONNECTOR_ERROR_MESSAGES = {
	invalid_pairing_token: 'Pairing code was not accepted. Request a new code from the local connector.',
	pairing_expired: 'Pairing code has expired. Press Enter in the connector window for a new code.',
	pairing_already_used: 'Pairing code was already used. Request a new code from the local connector.',
	pairing_rate_limited: 'Too many failed pairing attempts. Wait a minute and try again.',
	invalid_pairing_request: 'Pairing code format is invalid.',
	invalid_origin: 'The local connector does not allow this page. Start it with --allow-origin ' + window.location.origin + '.',
	invalid_session: 'Connector session is no longer valid. ' + PAIR_AGAIN_GUIDANCE,
	session_expired: 'Connector session has expired. ' + PAIR_AGAIN_GUIDANCE,
	insufficient_session_scope: 'This connector session is not allowed to perform that operation.',
	company_target_not_approved: 'This target is not approved in the local connector.',
	unknown_company_target: 'This target is not registered in the local connector.',
	request_too_large: 'The request is too large. SQL for remote validation is limited to 64 KiB.',
	statement_limit_exceeded: 'Too many statements. At most 16 statements can be validated at once.',
	no_statements: 'No SQL statements found: the input is empty or contains only comments. Nothing was assessed.',
	delimiter_not_supported: 'DELIMITER is a mysql client command, not SQL, and is not supported. Stored program definitions are not analysed; validate the statements in the body separately, ending each with a semicolon.',
	stored_program_not_supported: 'A stored program definition (CREATE PROCEDURE, FUNCTION, TRIGGER or EVENT) cannot be split into statements reliably when its body has several statements or other statements are submitted with it. Stored programs are not analysed; validate the statements in the body separately.',
	executable_comment_not_supported: 'The SQL contains an executable comment (/*! ... */). MySQL runs the SQL inside such a comment, but the validator cannot analyse it, so nothing was assessed. Move any SQL out of the comment, or remove the comment, and validate again.',
	company_database_scope_unavailable: 'Discover databases again with this MySQL login before selecting one.',
	company_database_scope_stale: 'The target changed while discovering databases. Discover again.',
	database_not_authorized: 'That database is not in the list discovered for this target and login. Discover again.',
	database_discovery_failed: 'The connector could not list databases for this target and login.',
	target_needs_reapproval: 'This target must be re-approved in the local connector before use.',
	target_not_approved: 'This target is not approved in the local connector.',
	dns_identity_mismatch: 'The target host no longer resolves to its approved addresses. Ask the connector operator to review the target and re-approve it if appropriate.',
	dns_resolution_failed: 'The target host name could not be resolved. Check the VPN, DNS and network path to the server.',
	dns_result_invalid: 'The target host name did not resolve to a usable address. Ask the connector operator to review the target.',
	unsafe_destination: 'The target host resolved to an address the connector does not allow. Ask the connector operator to review the target.',
	// Company connection failures. No evidence is collected for any of them.
	database_connection_failed: 'The connector could not complete a connection to this target. No evidence was collected.',
	database_validation_failed: 'The connector could not complete the validation for this target. No result was produced.',
	tls_verification_failed: 'The server certificate could not be verified for this host name with the trusted certificates chosen for this target. Ask your administrator to check that trust choice and the server certificate.',
	tls_handshake_failed: 'A secure (TLS) connection to the server could not be established. Ask your administrator to check the server\'s TLS setup.',
	tls_not_offered: 'The server did not offer TLS, which company targets require. Ask your administrator.',
	auth_failed: 'MySQL rejected the login. Check your username and password. Repeated failures can lock the account.',
	password_expired: 'Your MySQL password has expired. Change it through your organisation\'s normal password-change process, then try again.',
	account_locked: 'This MySQL account is locked. Contact your database administrator.',
	host_not_allowed: 'MySQL does not allow this account to connect from this machine. Contact your database administrator.',
	host_blocked: 'MySQL is blocking connections from this machine after repeated connection errors. Contact your database administrator.',
	auth_method_unsupported: 'This account uses a MySQL authentication method the validator does not support. Contact your database administrator.',
	connection_limit_reached: 'MySQL refused the connection because a connection or account limit was reached. Try again later or contact your database administrator.',
	network_timeout: 'The connection to the target timed out. Check the VPN and network path to the server, then try again.',
	connection_refused: 'The connection was refused on the approved port. Check that MySQL is listening there, or ask the connector operator.',
	unreachable: 'The connection to the target failed or was lost. Check the VPN and network path to the server, then try again.',
	// Adding or re-approving a target from the page.
	approval_console_unavailable: 'The connector is not running in a terminal, so it cannot show the approval request. Start MySQL-DBA-Validator Console.exe on Windows, MySQL-DBA-Validator Console from Terminal on macOS, or python -m connector in a terminal, and try again.',
	invalid_target_request: 'The connection details were not accepted. Check the name, host name, port and certificate choice.',
	invalid_company_target_name: 'Use a connection name of up to 64 characters, without control characters.',
	invalid_company_target_host: 'Enter a DNS host name in ASCII, such as db.example.com. IP addresses and names in non-ASCII form (use the xn-- form) are not accepted.',
	invalid_company_target_port: 'Enter a port between 1 and 65535.',
	local_target_not_allowed_in_company_mode: 'MySQL on this computer does not need the connector: choose Local (this machine).',
	company_target_exists: 'A connector target with this host name and port already exists. Select it from the Target list.',
	company_dns_validation_failed: 'The host name could not be resolved. Check the VPN, DNS and network path to the server.',
	company_target_address_not_allowed: 'The host name resolves to an address the connector does not allow.',
	tls_ca_not_configured: 'The connector has no CA bundle configured. Choose a CA bundle file (or, on Windows, the Windows certificate store).',
	trust_policy_unsupported: 'The system certificate store is supported only on Windows. Choose a CA bundle file.',
	trust_store_unavailable: 'The Windows certificate store could not be loaded. Choose a CA bundle file.',
	trust_storage_unavailable: 'This connector cannot store CA bundles. Ask the connector operator.',
	invalid_trust_policy: 'Choose how the server certificate is trusted.',
	invalid_ca_bundle: 'The CA bundle is not valid PEM certificate data. A binary (DER) certificate must be exported again in PEM (Base-64) form; renaming it is not enough.',
	ca_bundle_too_large: 'The CA bundle is larger than 256 KiB.',
	ca_bundle_contains_private_key: 'The file contains a private key. Choose a file with CA certificates only, and keep private keys out of the validator.',
	ca_bundle_has_no_ca_certificate: 'The file contains no CA certificate. Use the CA bundle that issued the server certificate.',
	target_request_limit_reached: 'Too many approval requests are open. Wait a few minutes and try again.',
	company_target_not_reapprovable: 'This target cannot be re-approved from the page. Ask the connector operator.',
	trust_policy_unavailable: 'The trusted certificates recorded for this target are not available. Ask the connector operator.',
	approval_rate_limited: 'Too many wrong approval codes. Wait a minute and try again.',
	target_request_not_found: 'That approval request no longer exists. Request approval again.',
	approval_code_invalid: 'The approval code is not correct. Copy it exactly from the connector window.',
	target_request_cancelled: 'The request was cancelled after three wrong codes. Request approval again.',
	dns_changed_since_request: 'The host name now resolves to different addresses than the connector window showed. Nothing was approved. Request approval again and check the addresses.',
	trust_bundle_unavailable: 'The CA bundle could not be stored. Nothing was approved.',
	company_target_changed: 'The target changed while it was being approved. Request approval again.',
	invalid_target_confirmation: 'Enter the approval code shown in the connector window.',
	target_registry_unavailable: 'The connector could not save its target list. Nothing was approved.'
};

const MAX_CA_BUNDLE_BYTES = 256 * 1024;
const REAPPROVAL_CODES = ['dns_identity_mismatch', 'target_needs_reapproval'];

function showTargetRequest(open) {
	$('approvalCodeField').hidden = !open;
	confirmTargetBtn.hidden = !open;
	if (!open) {
		state.targetRequest = null;
		approvalCode.value = '';
	}
}

// The page only asks for a target (or re-approval of the selected one). The
// connector shows the exact request and a one-time code in its own window; only
// that code approves it. No MySQL login is part of the request.
async function requestTarget(reapproveTargetId) {
	const status = $('targetRequestStatus');
	showTargetRequest(false);
	try {
		const sessionToken = await ensureConnectorSession();
		let body;
		if (reapproveTargetId) {
			body = { target_id: reapproveTargetId };
		} else {
			body = {
				display_name: $('newTargetName').value.trim(),
				host: $('newTargetHost').value.trim(),
				port: Number($('newTargetPort').value),
				trust: newTargetTrust.value
			};
			if (body.trust === 'bundle') {
				const file = newTargetCa.files && newTargetCa.files[0];
				if (!file) throw new Error('Choose the CA bundle file (PEM certificates).');
				if (file.size > MAX_CA_BUNDLE_BYTES) throw new Error(CONNECTOR_ERROR_MESSAGES.ca_bundle_too_large);
				body.ca_pem = await file.text();
			}
		}
		requestTargetBtn.disabled = reapproveBtn.disabled = true;
		const data = await connectorRequest(CONNECTOR_URL + '/company/target-requests', {
			method: 'POST',
			headers: connectorHeaders(sessionToken, true),
			body: JSON.stringify(body)
		});
		if (!data || data.status !== 'awaiting_approval' || typeof data.request_id !== 'string')
			throw new Error('The local connector returned an unexpected response.');
		state.targetRequest = { requestId: data.request_id, sessionToken: sessionToken };
		showTargetRequest(true);
		setStatus(status, 'Check the request in the connector window. If every line is what you asked for, enter its approval code here.', '');
		approvalCode.focus();
	} catch (error) {
		setStatus(status, error.message || 'The request was not accepted.', 'error');
	} finally {
		requestTargetBtn.disabled = reapproveBtn.disabled = false;
	}
}

async function confirmTarget() {
	const status = $('targetRequestStatus');
	const pending = state.targetRequest;
	const code = approvalCode.value.trim();
	approvalCode.value = '';
	if (!pending || pending.sessionToken !== state.connectorSessionToken) {
		showTargetRequest(false);
		setStatus(status, CONNECTOR_ERROR_MESSAGES.target_request_not_found, 'error');
		return;
	}
	confirmTargetBtn.disabled = true;
	try {
		// The code goes in the request body only, never in a URL.
		const data = await connectorRequest(CONNECTOR_URL + '/company/target-requests/confirm', {
			method: 'POST',
			headers: connectorHeaders(pending.sessionToken, true),
			body: JSON.stringify({ request_id: pending.requestId, approval_code: code })
		});
		showTargetRequest(false);
		reapproveBtn.hidden = true;
		const approvedId = data && data.target ? data.target.target_id : '';
		await loadApprovedTargets();
		if (state.approvedTargets.some(t => t.target_id === approvedId)) {
			forgetRememberedTarget();
			// Entering remote mode: a login typed for Local must not be sent to
			// the new target. Switching between remote targets keeps the login.
			if (!state.remoteTarget) {
				username.value = '';
				password.value = '';
			}
			selectTarget(approvedId);
		}
		setStatus(status, 'Approved. Enter your MySQL username and password, then use Discover databases to test the connection.', 'success');
	} catch (error) {
		if (['target_request_not_found', 'target_request_cancelled', 'dns_changed_since_request', 'company_target_changed', 'company_target_exists'].includes(error.errorCode))
			showTargetRequest(false);
		setStatus(status, error.message || 'The approval was not accepted.', 'error');
	} finally {
		confirmTargetBtn.disabled = false;
	}
}

// Session timing comes only from the connector's expires_in_seconds. The page
// never renews a session: it warns before expiry and ends it at expiry.
const SESSION_EXPIRY_NOTICE_SECONDS = 300;
function scheduleSessionExpiryNotice(expiresInSeconds) {
	const seconds = Number(expiresInSeconds);
	if (!Number.isFinite(seconds) || seconds <= 0) return;
	const notice = Math.min(SESSION_EXPIRY_NOTICE_SECONDS, Math.floor(seconds / 2));
	const remaining = notice < 60 ? 'less than a minute' : 'about ' + Math.round(notice / 60) + ' minute(s)';
	state.sessionTimers.push(setTimeout(() => setStatus($('pairingStatus'),
		'Connector session ends in ' + remaining + '. ' + PAIR_AGAIN_GUIDANCE, ''),
		(seconds - notice) * 1000));
	state.sessionTimers.push(setTimeout(() => setPaired(null, CONNECTOR_ERROR_MESSAGES.session_expired, 'error'),
		seconds * 1000));
}

function setPaired(sessionToken, message, kind) {
	const changed = state.connectorSessionToken !== (sessionToken || null);
	state.connectorSessionToken = sessionToken || null;
	unpairBtn.hidden = !state.connectorSessionToken;
	loadTargetsBtn.hidden = !state.connectorSessionToken;
	addTarget.hidden = !state.connectorSessionToken;
	if (changed) {
		state.sessionTimers.forEach(clearTimeout);
		state.sessionTimers = [];
		// An open target request belongs to the session that made it.
		showTargetRequest(false);
		reapproveBtn.hidden = true;
		// Targets and discovered databases belong to one connector session only.
		// Only the selected target_id is remembered for the next session; the
		// login is cleared by selectTarget('') as always.
		if (state.remoteTarget) state.rememberedTargetId = state.remoteTarget.target_id;
		state.approvedTargets = [];
		renderTargetOptions();
		if (state.remoteTarget) selectTarget('');
		else if (state.discovery) clearDiscoveredDatabases();
	}
	setStatus($('pairingStatus'), message, kind);
}

// All connector calls go through here: maps connector errors to clear messages and
// drops the in-memory session on any 401. Errors are re-thrown, never retried
// against the public API.
async function connectorRequest(url, options) {
	if (url.indexOf(CONNECTOR_URL + '/') !== 0) throw new Error('Refusing a non-connector request.');
	try {
		return await requestJson(url, options);
	} catch (error) {
		if (error.status === undefined) {
			throw new Error('The local connector is not reachable at ' + CONNECTOR_URL + '. Is it running?');
		}
		if (error.status === 401 && url !== CONNECTOR_URL + '/pair') {
			setPaired(null, 'Connector session ended. ' + PAIR_AGAIN_GUIDANCE, 'error');
		}
		// A changed address set or a needs-reapproval target: offer the explicit
		// re-approval request for the selected target (never automatic).
		if (state.remoteTarget && REAPPROVAL_CODES.includes(error.errorCode)) reapproveBtn.hidden = false;
		const friendly = CONNECTOR_ERROR_MESSAGES[error.errorCode];
		const wrapped = new Error(friendly || error.message);
		wrapped.status = error.status;
		wrapped.errorCode = error.errorCode;
		throw wrapped;
	}
}

async function pairConnector() {
	const code = pairingCode.value.trim();
	pairingCode.value = '';
	if (!code) {
		setStatus($('pairingStatus'), 'Paste the pairing code printed by the local connector.', 'error');
		return;
	}
	pairBtn.disabled = true;
	pairBtn.setAttribute('aria-busy', 'true');
	setStatus($('pairingStatus'), 'Pairing...', '');
	try {
		const data = await connectorRequest(CONNECTOR_URL + '/pair', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ pairing_token: code })
		});
		if (!data || data.status !== 'paired' || typeof data.session_token !== 'string' || !data.session_token)
			throw new Error('The local connector returned an unexpected pairing response.');
		const minutes = Math.max(1, Math.round((Number(data.expires_in_seconds) || 0) / 60));
		setPaired(data.session_token, 'Paired with the local connector. Session expires in about ' + minutes + ' minute(s).', 'success');
		scheduleSessionExpiryNotice(data.expires_in_seconds);
		await loadApprovedTargets();
	} catch (error) {
		setPaired(null, error.message || 'Pairing failed.', 'error');
	} finally {
		pairBtn.disabled = false;
		pairBtn.removeAttribute('aria-busy');
	}
}

async function unpairConnector() {
	const sessionToken = state.connectorSessionToken;
	setPaired(null, 'Not paired. Remote targets need a pairing code from the local connector.', '');
	state.rememberedTargetId = null; // explicit unpair forgets the target too
	if (!sessionToken) return;
	try {
		await connectorRequest(CONNECTOR_URL + '/unpair', { method: 'POST', headers: connectorHeaders(sessionToken, false) });
	} catch (_) {
		// The local session is already discarded; a failed server-side unpair only
		// leaves a session that expires on its own.
	}
}

// Loads the targets this connector session may use. The connector lists approved
// targets only for browser sessions; anything else is dropped here as well.
async function loadApprovedTargets() {
	try {
		const sessionToken = await ensureConnectorSession();
		const data = await connectorRequest(CONNECTOR_URL + '/company/targets', {
			method: 'GET',
			headers: connectorHeaders(sessionToken, false),
		});
		state.approvedTargets = (data && Array.isArray(data.targets) ? data.targets : []).filter(target =>
			target && target.approval_state === 'approved' && typeof target.target_id === 'string' &&
			target.target_id && isRemoteTarget(String(target.host || '')));
	} catch (error) {
		state.approvedTargets = [];
		setStatus($('pairingStatus'), error.message || 'Could not load approved targets.', 'error');
	}
	renderTargetOptions();
	// A selection that is no longer approved is dropped (and its discovery state with it).
	if (state.remoteTarget && !state.approvedTargets.some(t => t.target_id === state.remoteTarget.target_id))
		selectTarget('');
	else if (state.remoteTarget)
		targetSelect.value = state.remoteTarget.target_id;
	else if (state.rememberedTargetId) {
		// After re-pairing: reselect the previous target only if this fresh list still
		// approves it. Login and databases are not restored; discover again.
		const remembered = state.rememberedTargetId;
		state.rememberedTargetId = null;
		if (state.approvedTargets.some(t => t.target_id === remembered)) selectTarget(remembered);
	}
}

// An explicit choice (another target or Local) or local input replaces any remembered target.
function forgetRememberedTarget() { state.rememberedTargetId = null }

function renderTargetOptions() {
	targetSelect.replaceChildren(new Option('Local (this machine)', ''));
	state.approvedTargets.forEach(target => targetSelect.add(new Option(
		String(target.display_name || target.target_id) + ' (' + String(target.host) + ':' + String(target.port) + ')',
		target.target_id)));
}

// Selecting a target only changes which approved target_id is used. The host/port
// fields become read-only display copies; they are never sent for remote operations.
function selectTarget(targetId) {
	const target = state.approvedTargets.find(t => t.target_id === targetId) || null;
	const wasRemote = Boolean(state.remoteTarget);
	reapproveBtn.hidden = true; // offered again only by an error for the new selection
	state.remoteTarget = target;
	targetSelect.value = target ? target.target_id : '';
	host.readOnly = port.readOnly = Boolean(target);
	if (target) {
		host.value = String(target.host);
		port.value = String(target.port);
	} else if (wasRemote) {
		host.value = '127.0.0.1';
		port.value = '3306';
		// A login typed for a company target must never be reused by the local
		// path, which posts credentials to the public API: leaving connector
		// mode (target change, unpair, session expiry) clears it.
		username.value = '';
		password.value = '';
	}
	syncCredentials();
	clearDiscoveredDatabases();
	setStatus($('discoveryStatus'), target
		? 'Enter your MySQL username and password, then discover databases.'
		: 'Test the connection before discovering databases.', '');
}

// The approved target selected for remote work, or a local refusal (nothing is sent).
function selectedRemoteTarget() {
	if (!state.remoteTarget)
		throw new Error(REMOTE_NOT_AVAILABLE + 'Select an approved connector target instead of typing a remote host.');
	return state.remoteTarget;
}

async function discoverRemote(target) {
	const login = remoteLogin();
	const sessionToken = await ensureConnectorSession();
	// Fixed-purpose discovery: the body carries only the per-request login.
	return connectorRequest(CONNECTOR_URL + '/company/targets/' + encodeURIComponent(target.target_id) + '/databases', {
		method: 'POST',
		headers: connectorHeaders(sessionToken, true),
		body: JSON.stringify({ username: login.username, password: login.password })
	});
}

// True only while the selected database came from discovery for this exact
// target, MySQL username and connector session.
function discoveryMatches(target) {
	const discovery = state.discovery;
	return Boolean(discovery && target &&
		discovery.targetId === target.target_id &&
		discovery.username === state.credentials.username &&
		discovery.sessionToken === state.connectorSessionToken &&
		discovery.databases.includes(state.selectedDatabase));
}

// The user's own MySQL login for a company target, sent to the local connector in
// the body of each operation. The connector uses it for that operation only and
// never stores it; it is never sent to the public API.
function remoteLogin() {
	if (!state.credentials.username || !state.credentials.password)
		throw new Error('Enter your MySQL username and password for the remote target.');
	return { username: state.credentials.username, password: state.credentials.password };
}

// REMOTE: connector only. Must never call the public /api/* routes.
async function validateRemote(sql) {
	const remoteTarget = selectedRemoteTarget();
	const login = remoteLogin();
	// Only target_id, the SQL, the login and (optionally) a discovered database are sent.
	const body = { sql: sql, username: login.username, password: login.password };
	if (state.selectedDatabase) {
		if (!discoveryMatches(remoteTarget))
			throw new Error('The selected database was not discovered for this target, login and session. Discover again.');
		body.database = state.selectedDatabase;
	}
	const sessionToken = await ensureConnectorSession();
	return connectorRequest(CONNECTOR_URL + '/company/targets/' + encodeURIComponent(remoteTarget.target_id) + '/validate', {
		method: 'POST',
		headers: connectorHeaders(sessionToken, true),
		body: JSON.stringify(body)
	});
}

// LOCAL: loopback or static validation through the public API (unchanged contract).
// The online demo sends the SQL text only.
async function validateLocal(sql) {
	const body = PUBLIC_DEMO ? { sql: sql } : {
		sql: sql,
		host: state.credentials.host,
		port: state.credentials.port,
		username: state.credentials.username,
		password: state.credentials.password
	};
	if (state.selectedDatabase && !PUBLIC_DEMO) body.database = state.selectedDatabase;
	return requestJson('/api/validate', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
}

async function validateSql() {
	const sql = sqlInput.value;
	if (!sql.trim()) {
		setStatus($('validationStatus'), 'Enter SQL to validate.', 'error');
		return;
	}

	validateBtn.disabled = true;
	validateBtn.textContent = 'Validating...';
	validateBtn.setAttribute('aria-busy', 'true');
	$('results').setAttribute('aria-busy', 'true');
	setStatus($('validationStatus'), 'Reviewing statement...', '');
	renderLoading();

	try {
		// A selected connector target, or any typed remote host, takes the connector path;
		// validateRemote refuses locally when no approved target is selected.
		// The online demo never takes the connector path.
		const data = !PUBLIC_DEMO && (state.remoteTarget || isRemoteTarget(state.credentials.host))
			? await validateRemote(sql)
			: await validateLocal(sql);

		if (!data || !Array.isArray(data.statements))
			throw new Error(errorMessage(data, 'Validation returned an unexpected response.'));

		renderResults(data);
		setStatus($('validationStatus'), notAnalysedCount(data)
			? 'Review incomplete. ' + notAnalysedText(data, notAnalysedCount(data))
			: data.overall_confidence === 'HIGH'
				? 'Review complete.'
				: 'Review complete. Confidence is ' + text(data.overall_confidence) + ': read the limitations.', 'success');
	} catch (error) {
		setStatus($('validationStatus'), error.message || 'Validation failed.', 'error');
		renderError(error.message || 'Validation failed.');
	} finally {
		validateBtn.disabled = false;
		validateBtn.textContent = 'Validate SQL';
		validateBtn.removeAttribute('aria-busy');
		$('results').removeAttribute('aria-busy');
		updateStepper();
	}
}

function updateContext() {
	// Header shows where SQL is reviewed: Local, or the selected approved remote target.
	const where = state.remoteTarget
		? 'Remote: ' + String(state.remoteTarget.display_name || state.remoteTarget.target_id)
		: PUBLIC_DEMO ? 'Online demo' : 'Local';
	$('headerContext').textContent = where + ' · ' +
		(state.selectedDatabase ? 'Database: ' + state.selectedDatabase : 'Static analysis only');
	$('contextStatus').textContent = state.selectedDatabase ? 'Database: ' + state.selectedDatabase : 'No database selected';
	$('contextDot').classList.toggle('on', Boolean(state.selectedDatabase));
	$('sidebarDot').classList.toggle('on', Boolean(state.selectedDatabase));
	$('drawerDot').classList.toggle('on', Boolean(state.selectedDatabase));
	$('drawerStatus').textContent = state.selectedDatabase ? 'Database: ' + state.selectedDatabase : 'Static analysis only';
	updateStepper();
	updateDiagnosticsContext();
}

function updateStepper() {
	const steps = [...document.querySelectorAll('.stepper .step')];
	const hasResults = Boolean(document.querySelector('#results .statement'));
	const completed = state.selectedDatabase ? 3 : state.tested && state.databases.length ? 2 : state.tested ? 1 : 0;
	const active = hasResults ? 4 : state.selectedDatabase ? 3 : state.tested && state.databases.length ? 2 : 1;
	steps.forEach((step, index) => {
		step.classList.toggle('done', index < completed);
		step.classList.toggle('active', index + 1 === active);
	});
}

// UI Rendering
function el(tag, className, content) {
	const node = document.createElement(tag);
	if (className) node.className = className;
	if (content != null) node.textContent = content;
	return node;
}

function metric(label, value, extraClass) {
	const node = el('div', 'metric' + (extraClass ? ' ' + extraClass : ''));
	node.append(el('div', 'section-label', label), el('b', '', text(value)));
	return node;
}

function factorList(factors, emptyMessage) {
	const list = el('ul', 'factor-list');
	if (!factors || !factors.length) {
		list.append(el('li', 'muted', emptyMessage || 'No additional risk identified.'));
		return list;
	}
	factors.forEach(factor => {
		const row = el('li');
		row.append(el('span', '', text(factor.label || factor.text || factor.code)));
		if (factor.points != null) row.append(el('span',
			'points ' + (factor.points > 0 ? 'up' : factor.points < 0 ? 'down' : 'zero'),
			(factor.points > 0 ? '+' : '') + factor.points + ' points'));
		list.append(row);
	});
	return list;
}

function analysisModeLabel(mode) {
	return mode === 'DATABASE_EVIDENCE' ? 'Database evidence'
		: mode === 'STATIC_DATABASE_METADATA' ? 'Static + database metadata'
		: mode === 'STATIC_DATABASE_UNAVAILABLE' ? 'Static + database evidence unavailable'
			: 'Static analysis';
}

// The statement's evidence state (server source of truth). Results without one are static only.
function evidenceOf(s) {
	return s.evidence || {
		connection: { state: 'not_attempted', reason: 'not_configured' },
		plan: { state: 'not_attempted', reason: null },
		metadata: { state: 'not_attempted', reason: null, tables: [] },
		overall: 'static_only'
	};
}

function metadataStatusLabel(metadata) {
	return metadata.state === 'collected' ? 'Collected'
		: metadata.state === 'not_applicable' ? 'Not applicable'
			: 'Not collected';
}

function plainList(items, empty) {
	const list = el('ul', 'plain-list');
	if (!items || !items.length) {
		list.append(el('li', 'muted', empty || 'None reported.'));
		return list;
	}
	items.forEach(item => {
		list.append(el('li', '', typeof item === 'string' ? item : (item.text || item.name || JSON.stringify(item))));
	});
	return list;
}

function layer(title, className, children) {
	const box = el('section', 'layer ' + className);
	box.append(el('h3', '', title));
	children.forEach(child => box.append(child));
	return box;
}

// Why evidence is missing, from the stable status the server reports. These say what
// to do next; they never contain hosts, usernames, SQL or raw database errors.
const EVIDENCE_STATUS_MESSAGES = {
	not_configured: 'Enter your database login and select a database, then validate again.',
	unreachable: 'The database could not be reached. Check that MySQL is running and reachable.',
	auth_failed: 'The database rejected your login. Check the username and password.',
	database_not_found: 'The selected database was not found. Discover databases again.',
	insufficient_privileges: 'Your account lacks a privilege this evidence needs. EXPLAIN requires the same privileges as the statement itself.',
	explain_failed: 'MySQL could not produce an EXPLAIN plan for this statement.',
	unsupported_statement_type: 'No execution plan is collected for this statement type.',
	collection_failed: 'Evidence collection failed. The static analysis is still complete.',
	plan_not_collected_read_only: 'Execution plan not requested: the validator does not request plans for UPDATE or DELETE, because evidence connections are read-only and it never asks for write access.',
	plan_not_eligible: 'No execution plan was requested: this statement is not eligible for planning (for example SELECT ... INTO, or a function call that is not on the reviewed list of MySQL built-ins).',
	explain_format_unsupported: 'No execution plan is shown: MySQL returned it in a format the validator does not read. The server explain_format setting must be TRADITIONAL.'
};
if (PUBLIC_DEMO) EVIDENCE_STATUS_MESSAGES.not_configured = 'Database evidence is available only in the downloaded application.';

// Why table metadata was not collected, from the stable code the server reports.
const METADATA_REASON_MESSAGES = {
	timeout: 'Database metadata collection timed out.',
	permission_denied: 'Your account was denied access to the table metadata.',
	budget_exceeded: 'Database metadata collection exceeded its size limit.',
	invalid_reference: 'A table reference in this statement is not supported for metadata.',
	unavailable: 'Database metadata was unavailable.',
	no_database_selected: 'No database was selected, so table metadata was not collected.'
};

function fixedMessage(messages, code) {
	return Object.prototype.hasOwnProperty.call(messages, code) ? messages[code] : null;
}

// One headline per overall evidence state; the lines below it say which evidence exists.
// "collected" is named per evidence type: that a table exists says nothing about how
// many rows a statement matches.
const EVIDENCE_HEADLINES = {
	static_only: 'Assessment based on SQL structure only.',
	unavailable: 'Database evidence could not be collected.',
	partial: 'Partial database evidence: some evidence was collected and some could not be.',
	not_applicable: 'Connected to the database; no database evidence applies to this statement.'
};
const COLLECTED_HEADLINES = {
	both: 'Collected: table metadata and an execution plan (optimizer estimates).',
	metadata: 'Collected: table metadata only.',
	plan: 'Collected: an execution plan only (optimizer estimates).'
};

function evidenceHeadline(state) {
	if (state.overall === 'collected') {
		const metadata = state.metadata.state === 'collected';
		const plan = state.plan.state === 'collected';
		return COLLECTED_HEADLINES[metadata && plan ? 'both' : plan ? 'plan' : 'metadata'];
	}
	return fixedMessage(EVIDENCE_HEADLINES, state.overall) || EVIDENCE_HEADLINES.unavailable;
}

// What is established about the rows an UPDATE or DELETE matches directly (server:
// scope.basis). Never a row count: at most one row is a bound, and the row may not
// exist. Indirect effects (triggers, foreign-key actions) are never inspected.
const INDIRECT_EFFECTS_NOTE = ' Triggers and foreign-key actions (such as ON DELETE CASCADE) were not checked.';

function scopeLine(s) {
	const scope = s.scope;
	if (!scope) return null;
	const indirect = scope.indirect_effects === 'not_checked' ? INDIRECT_EFFECTS_NOTE : '';
	if (scope.basis === 'all_rows') {
		return 'Row scope: every row of ' + text(scope.table)
			+ ' (from the SQL: no WHERE clause, or one that is always true).' + indirect;
	}
	if (scope.basis === 'at_most_one_row') {
		return 'Row scope: at most one row of ' + text(scope.table) + ' matches directly, by unique key '
			+ text(scope.index) + ' (confirmed from table metadata). The row may not exist.' + indirect;
	}
	if (scope.basis === 'not_established') {
		const connected = evidenceOf(s).connection.state === 'connected';
		return 'Row scope: not established. A WHERE clause can match zero, one, some or all rows'
			+ (connected ? '; the database evidence did not establish how many.' : '; the SQL alone does not say how many.')
			+ indirect;
	}
	return null;
}

function planLine(plan) {
	if (plan.state === 'collected' && plan.interpreted === false) {
		return 'Multi-step execution plan (' + text(plan.rows) + ' rows) collected: shown for reference, not summarised or used for scoring.';
	}
	if (plan.state === 'collected') return 'Execution-plan evidence was collected (optimizer estimates, not rows changed).';
	if (plan.state === 'not_collected_read_only') return EVIDENCE_STATUS_MESSAGES.plan_not_collected_read_only;
	if (plan.state === 'not_eligible') return EVIDENCE_STATUS_MESSAGES.plan_not_eligible;
	if (plan.state === 'not_supported') return EVIDENCE_STATUS_MESSAGES.unsupported_statement_type;
	if (plan.state === 'failed') {
		return 'Execution-plan evidence could not be collected. '
			+ (fixedMessage(EVIDENCE_STATUS_MESSAGES, plan.reason) || '');
	}
	return null;
}

function metadataLines(metadata) {
	if (metadata.state === 'collected') {
		const lines = ['Database metadata was collected.'];
		(metadata.tables || []).forEach(table => {
			lines.push(text(table.name) + ': ' + (table.found === true ? 'table found'
				: table.found === false ? 'not found in the selected database'
					: 'existence could not be determined'));
		});
		return lines;
	}
	if (metadata.state === 'failed') {
		return ['Database metadata could not be collected. '
			+ (fixedMessage(METADATA_REASON_MESSAGES, metadata.reason) || '')];
	}
	if (metadata.state === 'not_applicable') return ['No table metadata applies to this statement.'];
	const reason = fixedMessage(METADATA_REASON_MESSAGES, metadata.reason);
	return reason ? [reason] : [];
}

function evidenceLayer(s) {
	const state = evidenceOf(s);
	const evidence = s.database_evidence || {};
	const body = [el('p', 'evidence-headline', evidenceHeadline(state))];
	if (state.connection.state !== 'connected') {
		// No connection: say why (no login entered, login rejected, unreachable, ...).
		const reason = fixedMessage(EVIDENCE_STATUS_MESSAGES, state.connection.reason);
		if (reason) body.push(el('p', 'evidence-reason', reason));
	} else {
		metadataLines(state.metadata).forEach(line => body.push(el('p', 'evidence-metadata', line)));
		const plan = planLine(state.plan);
		if (plan) body.push(el('p', 'evidence-plan', plan));
	}
	// Plan fields only for a collected one-row plan: never a grid of blanks, and never
	// the first row of a multi-step plan presented as the whole plan.
	if (state.plan.state === 'collected' && state.plan.interpreted !== false) {
		const grid = el('div', 'metric-grid');
		grid.append(
			metric('Access type', evidence.access_type),
			metric('Estimated rows examined', evidence.estimated_rows == null ? 'n/a' : evidence.estimated_rows + ' (estimate)'),
			metric('Possible keys', evidence.possible_keys && evidence.possible_keys.length ? evidence.possible_keys.join(', ') : 'n/a'),
			metric('Selected key', evidence.key),
			metric('Key length', evidence.key_length),
			metric('Full table scan', evidence.full_table_scan == null ? 'n/a' : evidence.full_table_scan ? 'Yes' : 'No')
		);
		body.push(grid);
		if (evidence.extra) body.push(el('p', '', 'Additional plan detail: ' + evidence.extra));
	}
	body.push(el('div', 'subheading', 'Evidence-based risk adjustment'));
	body.push(factorList(s.evidence_factors, 'No additional database-evidence adjustment.'));
	if (s.high_estimated_rows_applied != null) {
		body.push(el('p', '', 'High estimated rows applied: ' + (s.high_estimated_rows_applied ? 'Yes' : 'No')));
	}
	return layer('DATABASE EVIDENCE', 'evidence', body);
}

function metadataLayer(s) {
	const factors = s.metadata_factors || [];
	const m1 = factors.filter(f => String(f.code || '').startsWith('M1_'));
	const m2 = factors.filter(f => String(f.code || '').startsWith('M2_'));
	const metadata = evidenceOf(s).metadata;
	const status = metadataStatusLabel(metadata);
	const adjustment = s.metadata_score_adjustment || 0;
	const body = [
		el('p', '', 'Metadata: ' + status),
		el('p', '', metadata.state === 'collected'
			? 'Risk adjustment: ' + (adjustment > 0 ? '+' : '') + adjustment + ' points'
			: 'Metadata-based scoring was not applied.'),
		el('div', 'subheading', 'Table scan and estimated row volume'),
		el('p', 'assess-detail', scanLine(s)),
		...(m1.length ? [factorList(m1)] : []),
		el('div', 'subheading', 'Filter and index assessment'),
		el('p', 'assess-detail', filterLine(s)),
		...(m2.length ? [factorList(m2)] : [])
	];
	return layer('PLAN AND INDEX ASSESSMENT', 'metadata', body);
}

function statementCard(s, index) {
	const card = el('article', 'statement lvl-' + text(s.risk_level));
	card.id = 'statement-' + (index + 1);
	const head = el('div', 'statement-head');
	const title = el('div', 'statement-title');
	title.append(
		el('span', 'statement-number', 'STATEMENT ' + (index + 1)),
		el('span', 'statement-type', text(s.statement_type)),
		el('span', 'statement-tables', s.tables && s.tables.length ? s.tables.join(', ') : 'No table identified')
	);
	head.append(title, el('span', 'risk ' + text(s.risk_level), text(s.risk_level) + ' / ' + text(s.score)));
	card.append(head);

	const body = el('div', 'statement-body');
	if (s.analysis_text_differs) {
		body.append(el('div', 'warning analysis-warning', 'The SQL could only be read partly. The analysed text below may differ from what you submitted; review the original statement by hand.'));
	}
	body.append(el('pre', 'sql-block', s.sql));

	const metrics = el('div', 'metric-grid');
	metrics.append(
		metric('Final score', s.score + ' / 100'),
		metric('Confidence', s.confidence, 'confidence'),
		metric('Static score', s.static_score + ' / 100')
	);
	body.append(metrics);

	const layers = el('div', 'layer-grid');
	layers.append(
		layer('FINAL RESULT', 'final', [
			el('p', '', 'Final risk: ' + text(s.risk_level)),
			...(scopeLine(s) ? [el('p', 'row-scope', scopeLine(s))] : []),
			el('div', 'subheading', 'Why'),
			plainList(s.reasons, 'No additional reasons reported.'),
			el('div', 'subheading', 'Review checklist'),
			plainList(s.checklist, 'No checklist items reported.')
		]),
		reasoningDetails(s),
		evidenceLayer(s),
		metadataLayer(s),
		...(s.optimization ? [optimizationDetails(s.optimization)] : [])
	);
	body.append(layers);
	card.append(body);
	return card;
}

// Detailed score reasoning, collapsed by default so the result reads first.
function reasoningDetails(s) {
	const box = el('details', 'reasoning');
	box.append(
		el('summary', '', 'Score reasoning: static score ' + text(s.static_score)
			+ ', final score ' + text(s.score) + ' (out of 100)'),
		layer('STATIC ANALYSIS', 'static', [
			el('p', '', 'Static risk: ' + text(s.static_risk_level)),
			el('div', 'subheading', 'Static factors'),
			factorList(s.factors, 'No additional static factors.')
		])
	);
	return box;
}

// Optimization advice: separate from the risk result, collapsed by default. Every item
// is an observation from the SQL; nothing here is measured or claims a speed-up.
function optimizationDetails(o) {
	const items = o.recommendations || [];
	const box = el('details', 'reasoning optimization');
	const summary = o.status === 'advised'
		? 'Optimization advice: ' + items.length + ' observation' + (items.length === 1 ? '' : 's')
		: o.status === 'none_found' ? 'Optimization advice: no observations from the rules checked'
		: 'Optimization advice: not checked';
	box.append(el('summary', '', summary + ' (advisory; does not change the risk score)'));
	const children = [];
	if (o.status === 'not_eligible' || o.status === 'unavailable') {
		children.push(el('p', 'muted', o.reason || 'Optimization advice could not be produced for this statement.'));
	} else if (!items.length) {
		children.push(el('p', 'muted', 'The rules checked are: SELECT * over tables, tables joined without a join condition, a LIKE pattern that starts with a wildcard, a filtered column inside a function or calculation, a HAVING condition on a grouping column, ORDER BY RAND(), and a UNION that removes duplicates. The join, HAVING and UNION checks look at the outer query only, not inside subqueries, derived tables or CTEs. This is not a full performance review.'));
	}
	items.forEach(item => {
		const caveats = plainList(item.caveats, 'No caveats reported.');
		caveats.classList.add('caveats');
		children.push(
			el('div', 'subheading', text(item.title) + ' (observed in the SQL)'),
			el('p', '', item.observation),
			el('p', '', 'Suggested action: ' + item.action),
			el('p', 'muted', 'Why: ' + item.reasoning),
			el('p', 'caveats-label', 'Caveats:'),
			caveats,
			el('p', 'muted', 'How to verify: ' + item.verify)
		);
	});
	box.append(layer('OPTIMIZATION', 'optimization', children));
	return box;
}

// A future feature, shown as a label only: no action, no request, nothing sent anywhere.
// The marks are inline SVG built here (no external asset) and hidden from screen readers.
function svgIcon(viewBox, paths) {
	const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
	svg.setAttribute('viewBox', viewBox);
	svg.setAttribute('aria-hidden', 'true');
	svg.setAttribute('focusable', 'false');
	paths.forEach(d => {
		const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
		path.setAttribute('d', d);
		svg.append(path);
	});
	return svg;
}

// Closing the teaser lasts until the page is reloaded: kept in memory only, never stored.
let aiTeaserClosed = false;

function aiAdvisorNote() {
	const note = el('aside', 'ai-teaser');
	note.setAttribute('aria-label', 'AI Advisor, coming soon');
	const mark = el('div', 'ai-mark');
	mark.append(svgIcon('0 0 24 24', [
		'M11 3c.5 4.6 2.9 7 7.5 7.5-4.6.5-7 2.9-7.5 7.5-.5-4.6-2.9-7-7.5-7.5C8.1 10 10.5 7.6 11 3z',
		'M18.5 15c.2 1.6 1 2.3 2.5 2.5-1.5.2-2.3 1-2.5 2.5-.2-1.5-1-2.3-2.5-2.5 1.5-.2 2.3-.9 2.5-2.5z'
	]));
	const head = el('div', 'ai-teaser-head');
	head.append(el('span', 'ai-teaser-title', 'AI Advisor'), el('span', 'ai-badge', 'COMING SOON'));
	const privacy = el('p', 'ai-privacy');
	privacy.append(svgIcon('0 0 24 24', ['M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z']),
		el('span', '', 'No SQL is sent to an AI service.'));
	const body = el('div', 'ai-teaser-body');
	body.append(head,
		el('p', 'ai-teaser-text', 'Deeper SQL explanations and intelligent optimization suggestions, with optional AI assistance in a future version.'),
		privacy);
	const close = el('button', 'close-x');
	close.type = 'button';
	close.title = 'Dismiss';
	close.setAttribute('aria-label', 'Dismiss AI Advisor teaser');
	close.append(svgIcon('0 0 24 24', ['M6 6l12 12M18 6 6 18']));
	close.onclick = () => {
		aiTeaserClosed = true;
		// Keyboard users keep their place: focus moves to the nearest control above.
		const previous = [...$('results').querySelectorAll('summary')].pop();
		const hadFocus = document.activeElement === close;
		note.remove();
		if (hadFocus && previous) previous.focus({ preventScroll: true });
	};
	note.append(mark, body, close);
	return note;
}

const PLAN_COUNT_LABELS = {
	collected: 'collected',
	not_collected_read_only: 'not collected (read-only connection)',
	not_eligible: 'not eligible',
	not_supported: 'not applicable',
	failed: 'could not be collected'
};
const METADATA_COUNT_LABELS = {
	collected: 'collected',
	failed: 'could not be collected',
	not_applicable: 'not applicable',
	not_attempted: 'not collected'
};

function countsText(counts, labels) {
	return Object.keys(labels).filter(key => counts && counts[key])
		.map(key => counts[key] + ' ' + labels[key]).join(', ');
}

// What the batch's database evidence actually was; the analysis mode label alone
// never implies that all evidence was collected.
function evidenceSummaryText(summary) {
	if (!summary || summary.connection.state === 'not_attempted') return 'Assessment based on SQL structure only.';
	if (summary.connection.state === 'failed') return 'Database evidence could not be collected.';
	const parts = [];
	const plan = countsText(summary.plan, PLAN_COUNT_LABELS);
	const metadata = countsText(summary.metadata, METADATA_COUNT_LABELS);
	if (plan) parts.push('Execution plan: ' + plan);
	if (metadata) parts.push('Metadata: ' + metadata);
	if (summary.partial) parts.push('Partial evidence');
	return parts.join(' · ') || 'Connected; no database evidence applies.';
}

const RISK_ORDER = { CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 };

function briefSection(title, children) {
	const box = el('section', 'brief-section');
	box.append(el('h3', '', title));
	children.forEach(child => box.append(child));
	return box;
}

function briefList(items, empty) {
	const list = el('ul', 'brief-list');
	if (!items.length) list.append(el('li', 'muted', empty));
	items.forEach(item => {
		const row = el('li');
		if (item.level) row.append(el('span', 'sev ' + item.level, item.level));
		row.append(el('span', '', item.text));
		list.append(row);
	});
	return list;
}

// What the overview and the review brief show, derived only from fields the server
// returned: it never calls SQL safe, never states a row count and never implies a
// plan that was not collected.
// Plan and metadata factors in a DBA's words. Estimates stay labelled as estimates.
function factorText(s, factor) {
	const label = text(factor.label || factor.text || factor.code);
	const rows = s.database_evidence && s.database_evidence.estimated_rows;
	if (label === 'EXPLAIN indicates a full table scan') return 'Full table scan (EXPLAIN access type ALL)';
	if (label === 'EXPLAIN estimated rows are high') {
		return typeof rows === 'number'
			? 'Optimizer estimate of ' + rows.toLocaleString('en-US') + rowsWord(rows) + ' examined (estimate)'
			: 'High optimizer row estimate (estimate)';
	}
	if (String(factor.code || '').startsWith('M2_')) {
		const filter = s.filter_index || {};
		return 'Full scan, and no index starts with the filter column'
			+ (filter.column ? ' ' + filter.column : '') + ' (table metadata)';
	}
	return label;
}

function formatRows(value) {
	return typeof value === 'number' ? value.toLocaleString('en-US') : 'n/a';
}

function rowsWord(value) {
	return value === 1 ? ' row' : ' rows';
}

// The plan in one line: access path and the optimizer's row estimate, never a count.
function scanLine(s) {
	const plan = evidenceOf(s).plan;
	const evidence = s.database_evidence || {};
	if (plan.state === 'collected' && plan.interpreted === false) {
		return 'Multi-step plan (' + text(plan.rows) + ' rows): shown under Database evidence, not summarised.';
	}
	if (plan.state === 'collected') {
		const path = evidence.full_table_scan
			? 'Full table scan (access type ALL)'
			: 'Access type ' + text(evidence.access_type) + (evidence.key ? ' using key ' + evidence.key : ', no key used');
		const extra = typeof evidence.extra === 'string' ? evidence.extra : '';
		const steps = ['Using filesort', 'Using temporary'].filter(step => extra.includes(step));
		const sorted = !steps.length ? ''
			: ' The plan shows ' + steps.join(' and ') + (s.has_limit
				? ': every matching row is read and sorted before LIMIT applies.'
				: '.');
		return path + '; optimizer estimate: ' + formatRows(evidence.estimated_rows)
			+ rowsWord(evidence.estimated_rows) + ' examined.' + sorted;
	}
	if (plan.state === 'not_collected_read_only') return 'Not collected: plans are not requested for UPDATE or DELETE.';
	if (plan.state === 'not_eligible') return 'Not collected: this statement is not eligible for a plan.';
	if (plan.state === 'not_supported') return 'Not applicable to this statement type.';
	if (plan.state === 'failed') return 'Collection failed. ' + (fixedMessage(EVIDENCE_STATUS_MESSAGES, plan.reason) || '');
	return 'Not collected: ' + NO_DATABASE;
}

// The filter and the table's indexes, from table metadata (server: filter_index).
function filterLine(s) {
	const filter = s.filter_index || {};
	const on = filter.table ? ' on ' + filter.table : '';
	if (filter.state === 'no_filter') return 'No WHERE filter.';
	if (filter.state === 'not_assessed') return 'Not assessed: only a single-column comparison with a value, on one table, is checked.';
	if (filter.state === 'matching_index') return 'An index' + on + ' starts with ' + text(filter.column) + ' (table metadata). The plan shows whether MySQL uses it.';
	if (filter.state === 'no_matching_index') return 'No index' + on + ' starts with ' + text(filter.column) + ' (table metadata).';
	if (filter.state === 'not_checked') {
		return 'Not checked: ' + (evidenceOf(s).connection.state === 'connected'
			? 'complete table and index metadata were not available.' : NO_DATABASE);
	}
	return 'Not applicable to this statement type.';
}

// What the overview and the review assessment show, derived only from fields the
// server returned: it never calls SQL safe, never states a row count and never
// implies a plan that was not collected.
function reviewModel(data) {
	const statements = data.statements || [];
	const ranked = statements.map((s, index) => ({ s, n: index + 1 }))
		.sort((a, b) => (RISK_ORDER[b.s.risk_level] || 0) - (RISK_ORDER[a.s.risk_level] || 0)
			|| (b.s.score || 0) - (a.s.score || 0));
	const summary = data.evidence_summary;
	const connection = summary ? summary.connection.state : 'not_attempted';
	const writes = statements.filter(s => s.statement_type === 'UPDATE' || s.statement_type === 'DELETE');

	const limits = [];
	if (connection === 'not_attempted') limits.push({ text: 'No database evidence: based on the SQL text only.' });
	if (connection === 'failed') limits.push({ text: 'The database connection failed: based on the SQL text only.' });
	ranked.forEach(({ s, n }) => {
		const label = 'Statement ' + n + ': ';
		if (s.parse_error) limits.push({ text: label + 'not analysed: ' + (NOT_ANALYSED_REASONS[s.analysis_status] || 'it could not be analysed') + '.' });
		else if (s.analysis_text_differs) limits.push({ text: label + 'the SQL could only be read partly.' });
		const state = evidenceOf(s);
		if (state.metadata.state === 'collected') {
			(state.metadata.tables || []).forEach(table => {
				if (table.found === false) limits.push({ text: label + text(table.name) + ' was not found in the selected database.' });
				else if (table.found !== true) limits.push({ text: label + 'whether ' + text(table.name) + ' exists could not be determined.' });
			});
		}
		if (state.plan.state === 'failed') limits.push({ text: label + 'the execution plan could not be collected.' });
		if (state.metadata.state === 'failed') limits.push({ text: label + 'table metadata could not be collected.' });
		if (s.scope && s.scope.basis === 'not_established') limits.push({ text: label + 'the rows it can change are not established.' });
	});

	const findings = [];
	ranked.forEach(({ s, n }) => {
		[...(s.factors || []), ...(s.evidence_factors || []), ...(s.metadata_factors || [])]
			.filter(f => f.points > 0).forEach(f => findings.push({
				level: text(s.risk_level),
				points: f.points,
				n,
				text: 'Statement ' + n + ': ' + factorText(s, f) + ' (+' + f.points + ')'
			}));
	});

	// Highest severity first, then the largest score contribution.
	findings.sort((a, b) => (RISK_ORDER[b.level] || 0) - (RISK_ORDER[a.level] || 0) || b.points - a.points);

	const actions = [];
	ranked.forEach(({ s }) => (s.checklist || []).forEach(item => {
		const line = typeof item === 'string' ? item : (item.text || item.name || '');
		if (line && !actions.some(a => a.text === line)) actions.push({ text: line });
	}));

	// Each evidence type with its state: collected, collection failed, not collected,
	// not checked or not applicable.
	const counts = summary && summary.metadata ? summary.metadata : {};
	const planCounts = summary && summary.plan ? summary.plan : {};
	const metadataParts = [];
	if (counts.collected) metadataParts.push('collected for ' + counts.collected + ' statement(s)');
	if (counts.failed) metadataParts.push('collection failed for ' + counts.failed + ' statement(s)');
	if (counts.not_attempted) metadataParts.push('not collected for ' + counts.not_attempted + ' statement(s) (no database selected)');
	const metadataText = connection === 'not_attempted' ? 'Not collected: ' + NO_DATABASE
		: connection === 'failed' ? 'Collection failed: the database connection failed.'
			: metadataParts.length ? metadataParts.join('; ').replace(/^./, c => c.toUpperCase()) + '.' : 'Not applicable.';
	const planText = connection === 'not_attempted' ? 'Not collected: ' + NO_DATABASE
		: connection === 'failed' ? 'Collection failed: the database connection failed.'
			: (countsText(planCounts, PLAN_COUNT_LABELS) || 'not applicable').replace(/^./, c => c.toUpperCase()) + '.';
	const metadataState = connection !== 'connected' ? 'none'
		: counts.failed ? 'partial' : counts.collected ? 'done' : 'none';
	const planState = connection === 'connected' && planCounts.collected ? 'done'
		: connection === 'connected' && planCounts.failed ? 'partial' : 'none';
	const evidenceRows = [['Static analysis', 'Collected for ' + statements.length + ' statement(s).', 'done'],
	['Table metadata', metadataText, metadataState],
	['Execution plan', planText, planState]];

	// Unknowns that apply to this review, each with why it is unknown.
	const unknowns = [];
	if (writes.length) {
		unknowns.push(['Rows changed', 'Not collected: the statement is never run. A read-only count of the candidate rows estimates it.', 'none']);
		if (writes.some(s => s.scope && s.scope.indirect_effects === 'not_checked')) {
			unknowns.push(['Triggers, foreign keys', 'Not checked: triggers and foreign-key actions are not inspected.', 'none']);
		}
	}
	if (statements.length) {
		unknowns.push(['Run time, locks', 'Not measured: plain EXPLAIN reports the optimizer plan; it does not measure run time or locks.', 'info']);
	}
	return { statements, limits, findings, actions, evidenceRows: evidenceRows.concat(unknowns), unknowns };
}

function evidenceTable(rows) {
	const evidence = el('dl', 'evidence-table');
	rows.forEach(([term, detail, coverage]) => evidence.append(
		el('dt', coverage ? 'cov-' + coverage : '', term), el('dd', '', detail)));
	return evidence;
}

// "1-3, 7" from [1, 2, 3, 7].
function numberRanges(numbers) {
	const parts = [];
	numbers.forEach((n, i) => {
		if (i > 0 && n === numbers[i - 1] + 1) {
			parts[parts.length - 1][1] = n;
		} else {
			parts.push([n, n]);
		}
	});
	return parts.map(([a, b]) => (a === b ? String(a) : a + '\u2013' + b)).join(', ');
}

// Why a statement was not analysed (analysis_status from the API). An unsupported
// statement is valid MySQL; an unreadable one may or may not be valid.
const NOT_ANALYSED_LABELS = {
	invalid: 'Not valid MySQL: ',
	unsupported: 'Not analysed (not supported): ',
	unreadable: 'Could not be read: '
};
const NOT_ANALYSED_REASONS = {
	invalid: 'not valid MySQL',
	unsupported: 'valid MySQL, but this statement type is not supported',
	unreadable: 'the validator could not read it'
};

// One compact row per statement, in submitted order: what it is, its risk and
// confidence, and its largest scoring factor. Each row opens its detailed card.
function statementIndex(statements) {
	const table = el('table', 'ov-table stmt-index');
	const head = el('tr');
	[['#', 'num'], ['Statement', ''], ['Risk', ''], ['Confidence', ''], ['Key finding', '']].forEach(([label, cls]) => {
		const th = el('th', cls, label);
		th.setAttribute('scope', 'col');
		head.append(th);
	});
	const thead = el('thead');
	thead.append(head);
	const tbody = el('tbody');
	statements.forEach((s, index) => {
		const n = index + 1;
		const factors = [...(s.factors || []), ...(s.evidence_factors || []), ...(s.metadata_factors || [])]
			.filter(f => f.points > 0).sort((a, b) => b.points - a.points);
		const key = s.parse_error ? (NOT_ANALYSED_LABELS[s.analysis_status] || 'Not analysed: ') + s.parse_error
			: factors.length ? factorText(s, factors[0]) + ' (+' + factors[0].points + ')'
				: 'No rule raised the score.';
		const row = el('tr', 'stmt-row');
		const link = el('a', '', '#' + n);
		link.href = '#statement-' + n;
		const number = el('td', 'num');
		number.append(link);
		const what = el('td', 'stmt-what', text(s.statement_type)
			+ (s.tables && s.tables.length ? ' \u00b7 ' + s.tables.join(', ') : ''));
		const risk = el('td');
		risk.append(el('span', 'sev ' + text(s.risk_level), text(s.risk_level)), el('span', 'assess-points', ' ' + text(s.score)));
		row.append(number, what, risk, el('td', '', text(s.confidence)), el('td', '', key));
		tbody.append(row);
	});
	table.append(thead, tbody);
	return table;
}

function assessRow(label, children) {
	const row = el('div', 'assess-row');
	const value = el('div', 'assess-value');
	children.forEach(child => value.append(child));
	row.append(el('div', 'assess-label', label), value);
	return row;
}

// The primary assessment, under the summary row: the top finding, what the plan and
// the indexes show, what limits the result, what is unknown and what to check first.
// Risk, score and confidence are in the summary row above and are not repeated.
function primaryAssessment(data) {
	const model = reviewModel(data);
	const box = el('section', 'assessment');
	box.setAttribute('aria-label', 'Primary assessment');

	const top = model.findings[0];
	const finding = el('div', 'assess-line');
	if (top) {
		const link = el('a', '', top.text.replace(/ \(\+\d+\)$/, ''));
		link.href = '#statement-' + top.n;
		finding.append(el('span', 'sev ' + top.level, top.level), link, el('span', 'assess-points', '+' + top.points));
	} else {
		finding.append(el('span', 'muted', 'No rule raised the score. This does not mean the SQL is safe.'));
	}

	const planned = model.statements.map((s, index) => ({ s, n: index + 1 }));
	const showN = planned.length > 1;
	const lines = (fn) => {
		if (!showN) return planned.map(({ s }) => el('div', 'assess-line', fn(s)));
		const groups = new Map();
		planned.forEach(({ s, n }) => {
			const textLine = fn(s);
			if (!groups.has(textLine)) groups.set(textLine, []);
			groups.get(textLine).push(n);
		});
		return [...groups].map(([textLine, numbers]) => el('div', 'assess-line',
			(numbers.length > 1 ? 'Statements ' : 'Statement ') + numberRanges(numbers) + ': ' + textLine));
	};

	const more = (items, limit, rest) => items.length > limit
		? [el('div', 'assess-line muted', '+' + (items.length - limit) + ' more ' + rest)] : [];
	const listLines = (items, limit) => items.slice(0, limit).map(item => el('div', 'assess-line', item.text));

	box.append(
		assessRow('Top finding', [finding]),
		assessRow('Execution plan', lines(scanLine)),
		assessRow('Filter and index', lines(filterLine)),
		assessRow('Limitations', model.limits.length
			? [...listLines(model.limits, 3), ...more(model.limits, 3, 'in the statement details')]
			: [el('div', 'assess-line muted', 'None identified beyond the unknowns below.')]),
		assessRow('Unknown', model.unknowns.map(([term, detail]) => el('div', 'assess-line', term + ': ' + detail))),
		assessRow('Check first', model.actions.length
			? [...listLines(model.actions, 3), ...more(model.actions, 3, 'in the statement details')]
			: [el('div', 'assess-line muted', 'No review actions reported.')]));
	return box;
}

function overviewTile(label, value, detail, className) {
	const tile = el('div', 'ov-tile' + (className ? ' ' + className : ''));
	tile.append(el('div', 'label', label), el('strong', '', value), el('small', '', detail));
	return tile;
}

function overviewPanel(title, children, className) {
	const panel = el('section', 'ov-card' + (className ? ' ' + className : ''));
	panel.append(el('h3', '', title));
	children.forEach(child => panel.append(child));
	return panel;
}

function actionButton(label, className, attributes) {
	const button = el('button', className, label);
	button.type = 'button';
	Object.entries(attributes).forEach(([name, value]) => button.setAttribute(name, value));
	return button;
}

function countBy(items, key) {
	const counts = {};
	items.forEach(item => { const value = text(item[key]); counts[value] = (counts[value] || 0) + 1; });
	return Object.entries(counts).map(([value, count]) => count + ' ' + value).join(', ');
}

// Statements of the response that were not analysed (they carry a parse_error).
function notAnalysedCount(data) {
	return data && Array.isArray(data.statements) ? data.statements.filter(s => s.parse_error).length : 0;
}

function notAnalysedText(data, count) {
	const total = data.statements.length;
	return count + ' of ' + total + (total === 1 ? ' statement was' : ' statements were')
		+ ' not analysed: the validator did not assess ' + (count === 1 ? 'its' : 'their')
		+ ' risk. Review ' + (count === 1 ? 'it' : 'them') + ' by hand.';
}

// The current review at a glance: idle, running, complete or failed. Values come only
// from this page's state and the last response; nothing is counted across reviews.
function renderOverview(status, data, message) {
	const band = el('div', 'ov-band');
	const model = data ? reviewModel(data) : null;
	const limited = data && data.overall_confidence !== 'HIGH';
	// Statements that were not analysed (invalid, unsupported or could not be read in
	// full): the SQL analysis itself is incomplete, whatever the database evidence.
	const notAnalysed = notAnalysedCount(data);

	const statusTile = status === 'running'
		? overviewTile('Review status', 'In progress', 'Waiting for the validator.', 'state-running')
		: status === 'failed'
			? overviewTile('Review status', 'Failed', message || 'The review did not complete.', 'state-failed')
			: status === 'complete' && notAnalysed
				? overviewTile('Review status', 'Incomplete analysis', notAnalysedText(data, notAnalysed), 'state-warning')
			: status === 'complete'
				? overviewTile('Review status', limited ? 'Complete, with limitations' : 'Complete',
					limited ? 'Read the limitations before relying on the result.' : 'All statements were assessed.',
					limited ? 'state-warning' : 'state-complete')
				: overviewTile('Review status', 'No review yet', 'Start a SQL review to see its assessment here.');
	statusTile.setAttribute('role', 'status');

	if (!model) {
		const none = status === 'running' ? 'Pending' : 'No result';
		band.append(
			overviewTile('Overall risk', none, 'The highest statement score of the review.', 'ov-risk'),
			overviewTile('Confidence', none, 'How well the evidence supports the assessment; not a safety rating.'),
			overviewTile('Statements', none, 'Up to 16 MySQL statements per review.'),
			statusTile);
		const row = el('div', 'ov-inline' + (status === 'failed' ? ' failed' : status === 'running' ? '' : ' start'));
		if (status === 'running') {
			row.append(el('span', 'spinner'), el('span', '', 'Reviewing your SQL...'));
		} else if (status === 'failed') {
			row.setAttribute('role', 'alert');
			row.append(el('span', '', message || 'The review did not complete.'),
				actionButton('Back to SQL review', 'secondary', { 'data-view': 'review' }));
		} else {
			const lead = el('div', 'ov-start-lead');
			lead.append(el('b', '', 'Start a SQL review'),
				el('span', '', 'Paste the SQL from a ticket. Each statement is classified and scored, with how '
					+ 'confident that assessment is and what to check before you run it. Your SQL is never executed.'));
			const steps = el('ol', 'ov-start-steps');
			[['Paste SQL', 'up to 16 statements'], ['Connect (optional)', 'adds plan and metadata evidence'],
			['Read', 'risk, confidence, findings']].forEach(([title, note]) => {
				const step = el('li');
				step.append(el('b', '', title), el('span', '', note));
				steps.append(step);
			});
			row.append(lead, steps, actionButton('New SQL review', 'primary', { 'data-action': 'new-review' }));
		}
		$('overviewBody').replaceChildren(band, row);
		return;
	}

	const risk = el('div', 'ov-tile ov-risk lvl-' + text(data.overall_risk_level));
	const riskValue = el('strong');
	riskValue.append(el('span', 'risk-badge ' + text(data.overall_risk_level), text(data.overall_risk_level)),
		el('span', 'visually-hidden', ' · '), el('span', '', text(data.overall_score) + ' / 100'));
	risk.append(el('div', 'label', 'Overall risk'), riskValue,
		el('small', '', 'Highest statement score; not a sum or an average.'));
	band.append(risk,
		overviewTile('Confidence', text(data.overall_confidence),
			'Lowest statement confidence. How well the evidence supports the assessment; not a safety rating.'),
		overviewTile('Statements', String(model.statements.length),
			(countBy(model.statements, 'statement_type') || 'None') + '. By risk: '
			+ (countBy(model.statements, 'risk_level') || 'none') + '.'),
		statusTile);

	// The highest-risk statement and its row scope, as reported.
	const top = model.statements.map((s, index) => ({ s, n: index + 1 }))
		.sort((a, b) => (RISK_ORDER[b.s.risk_level] || 0) - (RISK_ORDER[a.s.risk_level] || 0)
			|| (b.s.score || 0) - (a.s.score || 0))[0];
	const headline = el('p', 'ov-headline');
	if (top) {
		headline.append(el('b', '', 'Highest risk: statement ' + top.n + ' · ' + text(top.s.statement_type)
			+ (top.s.tables && top.s.tables.length ? ' · ' + top.s.tables.join(', ') : '') + '. '),
			el('span', '', scopeLine(top.s) || ''));
	}

	const table = el('table', 'ov-table');
	const thead = el('thead');
	const headRow = el('tr');
	[['Severity', ''], ['Statement', ''], ['Finding', ''], ['Points', 'num']].forEach(([label, cls]) => {
		const th = el('th', cls, label);
		th.setAttribute('scope', 'col');
		headRow.append(th);
	});
	thead.append(headRow);
	const tbody = el('tbody');
	model.findings.slice(0, 8).forEach(finding => {
		const row = el('tr', 'ov-finding');
		const severity = el('td');
		severity.append(el('span', 'sev ' + finding.level, finding.level));
		const cell = el('td');
		const link = el('a', '', finding.text.replace(/^Statement \d+: /, '').replace(/ \(\+\d+\)$/, ''));
		link.href = '#statement-' + finding.n;
		cell.append(link);
		row.append(severity, el('td', 'stmt', '#' + finding.n), cell, el('td', 'num', '+' + finding.points));
		tbody.append(row);
	});
	table.append(thead, tbody);
	const findingsSection = el('section', 'ov-section');
	findingsSection.append(el('h3', '', 'Priority findings'),
		model.findings.length ? table : el('p', 'ov-empty-note', 'No rule raised the score. This does not mean the SQL is safe.'));
	const open = el('div', 'ov-card-foot');
	open.append(actionButton('Open full review', 'secondary', { 'data-view': 'review' }));
	findingsSection.append(open);

	const actionsList = el('ol', 'ov-actions');
	model.actions.slice(0, 6).forEach(action => actionsList.append(el('li', '', action.text)));
	const actionsSection = el('section', 'ov-section');
	actionsSection.append(el('h3', '', 'DBA actions'),
		model.actions.length ? actionsList : el('p', 'ov-empty-note', 'No review actions reported.'));
	const evidenceSection = el('section', 'ov-section');
	evidenceSection.append(el('h3', '', 'Evidence'), evidenceTable(model.evidenceRows));
	const limitsSection = el('section', 'ov-section');
	limitsSection.append(el('h3', '', 'Limitations'), briefList(model.limits, ''));
	const side = el('div', 'ov-side');
	side.append(actionsSection, evidenceSection, limitsSection);

	const layout = el('div', 'ov-layout');
	layout.append(findingsSection, side);
	$('overviewBody').replaceChildren(band, headline, layout);
}

function renderLoading() {
	const box = el('div', 'loading');
	box.setAttribute('role', 'status');
	box.append(el('span', 'spinner'), el('span', '', 'Reviewing your SQL...'));
	$('results').replaceChildren(box);
	renderOverview('running');
}

function renderResults(data) {
	$('results').replaceChildren();

	const summary = el('div', 'summary');
	const aggregate = el('div', 'summary-cell risk-' + text(data.overall_risk_level));
	aggregate.append(
		el('div', 'label', 'Aggregate / highest final risk'),
		el('strong', '', text(data.overall_risk_level)),
		el('small', '', text(data.statement_count) + ' statement(s), maximum final score')
	);

	const score = el('div', 'summary-cell');
	score.append(
		el('div', 'label', 'Overall score'),
		el('strong', '', text(data.overall_score) + ' / 100'),
		el('small', '', 'Not summed or averaged')
	);

	const mode = el('div', 'summary-cell');
	mode.append(
		el('div', 'label', 'Analysis mode'),
		el('strong', '', analysisModeLabel(data.analysis_mode)),
		el('small', 'evidence-summary', evidenceSummaryText(data.evidence_summary))
	);

	const confidence = el('div', 'summary-cell');
	confidence.append(
		el('div', 'label', 'Batch confidence'),
		el('strong', '', text(data.overall_confidence || 'n/a')),
		el('small', '', 'Lowest statement confidence; not a safety rating')
	);

	summary.append(aggregate, score, mode, confidence);
	$('results').append(summary, primaryAssessment(data));
	if (data.statements.length > 1) {
		$('results').append(el('h3', 'details-heading', 'Statements'), statementIndex(data.statements));
	}
	$('results').append(el('h3', 'details-heading', 'Statement details'));

	data.statements.forEach((statement, index) => {
		$('results').append(statementCard(statement, index));
	});
	if (!aiTeaserClosed) $('results').append(aiAdvisorNote());
	renderOverview('complete', data);
}

function renderError(message) {
	const box = el('div', 'error-box', message);
	box.setAttribute('role', 'alert');
	$('results').replaceChildren(box);
	renderOverview('failed', null, message);
}

// ---------------------------------------------------------------- observed performance
// Historical schema workload: statement families MySQL already recorded in
// performance_schema.events_statements_summary_by_digest, read only when the DBA asks.
// Separate from SQL review: nothing here reads or changes a validation result. The
// online demo has no such view and never calls these routes.
const PERF_DOCS = 'https://github.com/realspinn/mysql-dba-validator/blob/main/docs/historical-schema-workload.md';
const LOCK_DOCS = 'https://github.com/realspinn/mysql-dba-validator/blob/main/docs/innodb-lock-waits.md';

// What the snapshot was read for. The password is never part of it.
function perfContextKey() {
	const where = state.remoteTarget ? 'remote:' + state.remoteTarget.target_id
		: 'local:' + state.credentials.host + ':' + state.credentials.port;
	return [where, state.credentials.username, state.selectedDatabase].join('|');
}

// Milliseconds from the API (rounded there to 0.001 ms, so microseconds are exact).
// Null means MySQL recorded no time: never shown as 0.
function formatDuration(ms) {
	if (ms == null) return 'Not recorded';
	if (ms >= 1000) return (ms / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' s';
	if (ms >= 1) return ms.toLocaleString(undefined, { maximumFractionDigits: ms >= 100 ? 0 : 1 }) + ' ms';
	const us = Math.round(ms * 1000);
	return us >= 1 ? us + ' µs' : 'Under 1 µs';
}

function exactDuration(ms) {
	if (ms == null) return 'Not recorded';
	if (ms === 0) return 'Under 0.001 ms';  // recorded, but below the API's 0.001 ms resolution
	return ms.toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 }) + ' ms';
}

function formatCount(value) { return typeof value === 'number' ? value.toLocaleString() : 'n/a' }

// FIRST_SEEN / LAST_SEEN as MySQL returned them (server time, no zone).
function serverTime(value) {
	return typeof value === 'string' ? value.replace('T', ' ').replace(/\.\d+$/, '') : 'n/a';
}

const PERF_STATES = {
	no_observations: {
		title: 'No observations recorded',
		text: db => 'MySQL has no statement families recorded for ' + db + ' since the digest table was last reset or the server restarted. Run the workload you want to inspect, then refresh the snapshot.'
	},
	insufficient_privileges: {
		title: 'Unable to read Performance Schema',
		text: () => 'Your MySQL account cannot read the statement digest summary for this database. A DBA can grant SELECT on the columns this view reads. That privilege is server-wide: the account can then read normalized statements from every schema, so granting it is a DBA decision. The validator never requests or changes privileges.',
		docs: 'Required privileges'
	},
	performance_schema_disabled: {
		title: 'Performance Schema is disabled',
		text: () => 'This server runs with performance_schema off, so MySQL records no statement families. It can only be enabled at server startup. The validator never changes server settings.'
	},
	digest_consumer_disabled: {
		title: 'Statement digests are not being collected',
		text: () => 'The statements_digest or global_instrumentation consumer is disabled in performance_schema.setup_consumers, so MySQL is not aggregating statement families. The validator never changes instrumentation.'
	},
	collection_failed: {
		title: 'Collection did not complete',
		text: () => 'The Performance Schema read did not complete, for example because the connection was lost. Nothing more was sent on that connection. Refresh the snapshot to try again.'
	},
	unexpected_response: {
		title: 'Unexpected server response',
		text: () => 'MySQL returned values that do not match the documented digest summary, so no partial result is shown.'
	},
	auth_failed: {
		title: 'MySQL login failed',
		text: () => 'Check the username and password in the SQL review connection settings, then refresh the snapshot.',
		settings: true
	},
	unreachable: {
		title: 'Database connection unavailable',
		text: () => 'MySQL could not be reached. Check that the server is running and reachable, then refresh the snapshot.'
	},
	database_not_found: {
		title: 'Database not found',
		text: db => db + ' does not exist or your account cannot see it. Discover databases again in the connection settings.',
		settings: true
	}
};

function settingsButton() {
	const button = el('button', 'secondary', 'Open connection settings');
	button.type = 'button';
	button.addEventListener('click', () => {
		showView('review');
		$('connectionDrawer').open = true;
		(state.remoteTarget ? username : host).focus();
	});
	return button;
}

function docsLink(label, href) {
	const link = el('a', 'perf-docs', label);
	link.href = href;
	link.target = '_blank';
	link.rel = 'noopener noreferrer';
	return link;
}

function diagState(title, text, extras, kind) {
	const box = el('div', 'perf-state' + (kind ? ' ' + kind : ''));
	box.append(el('strong', '', title), el('p', '', text));
	if (extras && extras.length) {
		const actions = el('div', 'perf-state-actions');
		actions.append(...extras);
		box.append(actions);
	}
	return box;
}

// ---------------------------------------------------------------- snapshot views
// Observed performance and lock diagnostics share one mechanism: a read only when the
// DBA asks, bound to the current target, login and database. Any change of those (even
// back to an earlier one) starts a new generation and discards a read still in flight;
// a shown result is dropped as soon as the context changes. Memory only, never stored.

// Why nothing can be loaded yet, or null when a snapshot can be requested.
function snapshotBlocker(view) {
	if (state.remoteTarget) {
		if (!state.selectedDatabase || !discoveryMatches(state.remoteTarget))
			return view.subject + ' is read for one database. Enter your MySQL login for ' +
				String(state.remoteTarget.display_name || state.remoteTarget.target_id) +
				' and discover and select a database in the connection settings.';
		return null;
	}
	if (isRemoteTarget(state.credentials.host))
		return REMOTE_NOT_AVAILABLE + 'Select an approved connector target in the connection settings.';
	if (!validConnection() || !state.selectedDatabase)
		return view.subject + ' is read for one database. Enter your MySQL login and choose a database in the SQL review connection settings.';
	return null;
}

function viewNode(view, name) { return $(view.prefix + name) }

function factList(facts) {
	const list = el('ul', 'perf-facts');
	facts.forEach(([term, detail]) => {
		const item = el('li');
		item.append(el('b', '', term + '. '), document.createTextNode(detail));
		list.append(item);
	});
	return list;
}

function renderSnapshotIdle(view) {
	const blocker = snapshotBlocker(view);
	if (blocker) {
		viewNode(view, 'Body').replaceChildren(diagState('Select a database first', blocker, [settingsButton()]));
		return;
	}
	const box = el('div', 'perf-state perf-intro');
	const actions = el('div', 'perf-state-actions');
	actions.append(docsLink('What this reads and why', view.docs));
	box.append(el('strong', '', 'No snapshot loaded'), el('p', '', view.intro(state.selectedDatabase)),
		factList(view.facts), actions);
	viewNode(view, 'Body').replaceChildren(box);
}

function renderSnapshotResult(view, data) {
	if (view.hasData(data)) {
		view.render(data);
		return;
	}
	const known = view.states[data.status] || view.states.unexpected_response;
	const extras = [];
	if (known.settings) extras.push(settingsButton());
	if (known.docs) extras.push(docsLink(known.docs, view.docs));
	const kind = data.status === view.emptyStatus ? '' : 'notice';
	viewNode(view, 'Body').replaceChildren(diagState(known.title, known.text(state.selectedDatabase), extras, kind));
}

function updateSnapshotContext(view) {
	if (PUBLIC_DEMO || !viewNode(view, 'Body')) return;
	viewNode(view, 'Target').textContent = state.remoteTarget
		? String(state.remoteTarget.display_name || state.remoteTarget.target_id) : 'Local';
	viewNode(view, 'Database').textContent = state.selectedDatabase || 'None selected';
	const current = perfContextKey();
	if (current !== view.seen) {
		// Any change of target, login or database, even back to an earlier one, starts
		// a new generation: a read started before it is discarded when it arrives.
		view.seen = current;
		view.generation += 1;
	}
	if (view.key !== null && view.key !== current) {
		// The target, login or database changed: the last result no longer describes it.
		view.result = null;
		view.key = null;
	}
	if (view.loading) {
		// A read from an earlier generation is still in flight: show this context at
		// once and keep the button disabled until that read settles and is discarded.
		if (view.loadingGeneration !== view.generation) {
			setObservedAt(view, null);
			renderSnapshotIdle(view);
			viewNode(view, 'LoadBtn').disabled = true;
		}
		return;
	}
	setSnapshotButton(view);
	if (!view.result) {
		setObservedAt(view, null);
		renderSnapshotIdle(view);
	}
}

function updateDiagnosticsContext() {
	Object.values(SNAPSHOT_VIEWS).forEach(updateSnapshotContext);
}

// One action: load, or refresh once a result for this context is on screen.
function setSnapshotButton(view) {
	const button = viewNode(view, 'LoadBtn');
	button.disabled = Boolean(snapshotBlocker(view));
	button.textContent = view.result ? 'Refresh snapshot' : view.loadLabel;
	button.className = view.result ? 'secondary' : 'primary';
}

function observedAtText(iso) {
	const date = new Date(iso);
	return Number.isNaN(date.getTime()) ? 'n/a' : date.toLocaleString();
}

function setObservedAt(view, iso) {
	const node = viewNode(view, 'ObservedAt');
	node.textContent = iso ? observedAtText(iso) : 'Not loaded';
	if (iso) node.title = 'Validator clock, UTC: ' + iso;
	else node.removeAttribute('title');
}

// REMOTE: connector only, for an approved target and a discovered database.
async function requestSnapshot(view) {
	if (state.remoteTarget) {
		const target = selectedRemoteTarget();
		const login = remoteLogin();
		if (!discoveryMatches(target))
			throw new Error('The selected database was not discovered for this target, login and session. Discover again.');
		const sessionToken = await ensureConnectorSession();
		return connectorRequest(CONNECTOR_URL + '/company/targets/' + encodeURIComponent(target.target_id) + '/' + view.route, {
			method: 'POST',
			headers: connectorHeaders(sessionToken, true),
			body: JSON.stringify({ username: login.username, password: login.password, database: state.selectedDatabase })
		});
	}
	const data = await requestJson('/api/diagnostics/' + view.route, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			host: state.credentials.host,
			port: state.credentials.port,
			username: state.credentials.username,
			password: state.credentials.password,
			database: state.selectedDatabase
		})
	});
	// Refusals before any connection come back as {error_code, message}.
	if (data && typeof data.error_code === 'string' && typeof data.status !== 'string')
		throw new Error(errorMessage(data, 'The request was refused.'));
	return data;
}

async function loadSnapshot(view) {
	if (PUBLIC_DEMO || view.loading || snapshotBlocker(view)) return;
	const key = perfContextKey();
	const body = viewNode(view, 'Body'), button = viewNode(view, 'LoadBtn'), live = viewNode(view, 'Live');
	view.loading = true;
	view.loadingGeneration = view.generation;
	button.disabled = true;
	button.setAttribute('aria-busy', 'true');
	body.setAttribute('aria-busy', 'true');
	live.textContent = 'Reading Performance Schema.';
	if (!view.result) body.replaceChildren(diagState('Reading Performance Schema', view.reading));
	let data = null, failure = null;
	try {
		data = await requestSnapshot(view);
		if (!data || typeof data.status !== 'string' || !Array.isArray(data[view.list]))
			throw new Error('The diagnostic returned an unexpected response.');
	} catch (error) {
		failure = error.message || 'The request did not complete.';
	}
	view.loading = false;
	button.removeAttribute('aria-busy');
	body.removeAttribute('aria-busy');
	if (view.loadingGeneration !== view.generation) {
		// The context changed while reading (even if it changed back): discard.
		updateSnapshotContext(view);
		return;
	}
	view.key = key;
	view.result = failure ? { failure: failure } : data;
	setSnapshotButton(view);
	if (failure) {
		setObservedAt(view, null);
		body.replaceChildren(diagState('Request failed', failure, [], 'notice'));
		live.textContent = 'Request failed.';
		return;
	}
	setObservedAt(view, data.observed_at || null);
	renderSnapshotResult(view, data);
	live.textContent = view.hasData(data) ? view.loaded(data)
		: (view.states[data.status] || view.states.unexpected_response).title + '.';
}

function familyLabel(family) {
	return family.digest_text || 'Digest ' + family.digest.slice(0, 12) + ' (no text recorded)';
}

function detailRow(list, term, value, mono) {
	list.append(el('dt', '', term));
	const dd = el('dd', mono ? 'mono' : '', value);
	list.append(dd);
	return dd;
}

function renderFamilyDetail(family) {
	const pane = $('perfDetail');
	if (!pane) return;
	const heading = el('h3', 'details-heading', 'Statement family');
	heading.id = 'perfDetailHeading';
	const statement = el('pre', 'perf-sql', familyLabel(family));
	const list = el('dl', 'perf-detail-list');
	detailRow(list, 'Digest', family.digest, true);
	detailRow(list, 'Executions', formatCount(family.executions));
	detailRow(list, 'Total time', exactDuration(family.total_time_ms));
	detailRow(list, 'Average time', exactDuration(family.avg_time_ms));
	detailRow(list, 'Maximum time', exactDuration(family.max_time_ms));
	detailRow(list, '95th percentile', family.p95_time_ms == null ? 'Not recorded'
		: exactDuration(family.p95_time_ms) + ' (approximate, a high estimate from MySQL\'s histogram)');
	detailRow(list, 'Lock time', exactDuration(family.lock_time_ms) + (family.lock_time_ms == null ? '' : ' in total'));
	detailRow(list, 'Rows examined', formatCount(family.rows_examined));
	detailRow(list, 'Rows sent', formatCount(family.rows_sent));
	detailRow(list, 'Rows affected', formatCount(family.rows_affected));
	detailRow(list, 'Executions without an index', formatCount(family.no_index_used));
	detailRow(list, 'First seen', serverTime(family.first_seen) + ' (server time)');
	detailRow(list, 'Last seen', serverTime(family.last_seen) + ' (server time)');
	const notes = el('ul', 'perf-notes');
	notes.append(el('li', '', 'Totals cover every execution in this family since it was first seen. They are not a measurement of the SQL in your review.'));
	if (family.timing === 'not_recorded')
		notes.append(el('li', '', 'MySQL counted these executions but recorded no time for them, so time values are not shown.'));
	if (family.digest_text_truncated)
		notes.append(el('li', '', 'The statement text is shortened to 1024 characters.'));
	pane.replaceChildren(heading, statement, list, notes);
}

function selectFamily(rows, index, families) {
	rows.forEach((row, i) => {
		row.classList.toggle('selected', i === index);
		const button = row.querySelector('button');
		button.setAttribute('aria-pressed', String(i === index));
	});
	renderFamilyDetail(families[index]);
}

function renderPerfSnapshot(data) {
	const families = data.families;
	const head = el('div', 'perf-summary');
	const count = el('h3', 'perf-count', 'Statement families (' + families.length + ')');
	head.append(count, el('p', 'perf-rank', 'Ranked by total recorded time across all executions of each family. ' +
		(data.more_families ? 'MySQL recorded more families for this database; only the top ' + data.limit + ' are shown.'
			: 'These are all the families MySQL recorded for this database.') +
		' Not a view of all server activity.'));
	const notes = el('ul', 'perf-notes');
	if (data.digest_consumer === 'unverified')
		notes.append(el('li', '', 'Collection state not verified: your account cannot read performance_schema.setup_consumers.'));
	if (data.overflow_executions > 0)
		notes.append(el('li', '', formatCount(data.overflow_executions) + ' statements were counted in MySQL\'s catch-all row because the digest table is full, so these families may not be representative.'));
	if (families.some(family => family.timing === 'not_recorded'))
		notes.append(el('li', '', 'Some families have no recorded time: MySQL counted their executions without timing them.'));
	notes.append(el('li', '', 'The validator\'s own connection statements and evidence queries are recorded here too.'));

	const table = el('table', 'data-table perf-table');
	table.setAttribute('aria-describedby', 'perfRankNote');
	head.querySelector('.perf-rank').id = 'perfRankNote';
	const caption = el('caption', 'visually-hidden', 'Statement families for ' + data.schema);
	const thead = el('thead');
	const headRow = el('tr');
	[['Statement family', ''], ['Executions', 'num'], ['Total time', 'num'], ['Average', 'num opt'],
		['Maximum', 'num opt'], ['Rows examined', 'num opt-phone'], ['Rows sent', 'num opt'], ['Last seen', 'opt']]
		.forEach(([label, cls]) => { const th = el('th', cls, label); th.scope = 'col'; headRow.append(th); });
	thead.append(headRow);
	const tbody = el('tbody');
	const rows = families.map((family, index) => {
		const row = el('tr');
		const first = el('td', 'perf-stmt');
		const button = el('button', 'perf-row-btn', familyLabel(family));
		button.type = 'button';
		button.setAttribute('aria-controls', 'perfDetail');
		button.addEventListener('click', () => selectFamily(rows, index, families));
		first.append(button);
		row.append(first,
			el('td', 'num', formatCount(family.executions)),
			el('td', 'num', formatDuration(family.total_time_ms)),
			el('td', 'num opt', formatDuration(family.avg_time_ms)),
			el('td', 'num opt', formatDuration(family.max_time_ms)),
			el('td', 'num opt-phone', formatCount(family.rows_examined)),
			el('td', 'num opt', formatCount(family.rows_sent)),
			el('td', 'opt nowrap', serverTime(family.last_seen)));
		tbody.append(row);
		return row;
	});
	table.append(caption, thead, tbody);
	const wrap = el('div', 'table-wrap');
	wrap.append(table);
	const detail = el('section', 'perf-detail');
	detail.id = 'perfDetail';
	detail.setAttribute('aria-labelledby', 'perfDetailHeading');
	detail.setAttribute('aria-live', 'polite');
	$('perfBody').replaceChildren(head, notes, wrap, detail);
	selectFamily(rows, 0, families);
}

// ---------------------------------------------------------------- lock diagnostics
// A snapshot of InnoDB lock waits on the selected database's tables: who waits, who
// blocks, on which table and index. It never acts on a session.

function formatSeconds(value) {
	if (typeof value !== 'number') return 'Unknown';
	if (value < 1) return 'Under 1 s';
	if (value < 60) return value + ' s';
	return Math.floor(value / 60) + ' min ' + (value % 60) + ' s';
}

function sessionCell(side) {
	const cell = el('div', 'lock-session');
	cell.append(el('strong', '', side.session_id == null ? 'Connection not verified' : 'Connection ' + side.session_id),
		el('span', 'muted', 'Thread ' + side.thread_id));
	return cell;
}

function labelled(label, content, className) {
	const cell = el('td', className || '');
	cell.setAttribute('data-label', label);
	if (content instanceof Node) cell.append(content);
	else cell.textContent = content;
	return cell;
}

function renderLockSnapshot(data) {
	const head = el('div', 'perf-summary');
	const rank = el('p', 'perf-rank', 'One row for each waiting lock and a lock that blocks it, at the time shown. ' +
		'A blocking session may already have finished: refresh before acting on it.' +
		(data.more_waits ? ' More waits exist; only the first ' + data.limit + ' are shown.' : ''));
	rank.id = 'lockRankNote';
	head.append(el('h3', 'perf-count', 'Lock waits (' + data.waits.length + ')'), rank);
	const notes = el('ul', 'perf-notes');
	if (data.sessions === 'unverified')
		notes.append(el('li', '', 'Connection IDs are not verified: your account cannot read performance_schema.threads.'));
	notes.append(el('li', '', 'Only InnoDB row and table lock waits on this database\'s tables. Metadata locks and other contention are not included.'));
	notes.append(el('li', '', 'Time in state is whole seconds in the session\'s current state: for a waiting session, about how long it has waited; for an idle blocker, how long it has been idle.'));

	const table = el('table', 'data-table lock-table');
	table.setAttribute('aria-describedby', 'lockRankNote');
	const thead = el('thead');
	const headRow = el('tr');
	['Waiting session', 'Time in state', 'Blocking session', 'Time in state', 'Table and index', 'Lock']
		.forEach(label => { const th = el('th', '', label); th.scope = 'col'; headRow.append(th); });
	thead.append(headRow);
	const tbody = el('tbody');
	data.waits.forEach(wait => {
		const where = el('div', 'lock-where');
		where.append(el('code', '', wait.table + (wait.partition ? ' (' + wait.partition + ')' : '')),
			el('span', 'muted', wait.index ? 'Index ' + wait.index : 'No index (table lock)'));
		const lock = el('div', 'lock-mode');
		lock.append(el('span', '', wait.lock_type === 'RECORD' ? 'Row lock' : 'Table lock'),
			el('span', 'muted', 'Requested ' + wait.requested_mode),
			el('span', 'muted', 'Blocked by ' + wait.blocking_mode));
		const row = el('tr');
		row.append(labelled('Waiting session', sessionCell(wait.waiting)),
			labelled('Waiting session time in state', formatSeconds(wait.waiting.seconds_in_state), 'num'),
			labelled('Blocking session', sessionCell(wait.blocking)),
			labelled('Blocking session time in state', formatSeconds(wait.blocking.seconds_in_state), 'num'),
			labelled('Table and index', where),
			labelled('Lock', lock));
		tbody.append(row);
	});
	table.append(el('caption', 'visually-hidden', 'InnoDB lock waits for ' + data.schema), thead, tbody);
	const wrap = el('div', 'table-wrap');
	wrap.append(table);
	$('lockBody').replaceChildren(head, notes, wrap);
}

const COMMON_STATES = {
	collection_failed: {
		title: 'Collection did not complete',
		text: () => 'The Performance Schema read did not complete, for example because the connection was lost. Nothing more was sent on that connection. Refresh the snapshot to try again.'
	},
	unexpected_response: {
		title: 'Unexpected server response',
		text: () => 'MySQL returned values that do not match the documented tables, so no partial result is shown.'
	},
	auth_failed: PERF_STATES.auth_failed,
	unreachable: PERF_STATES.unreachable,
	database_not_found: PERF_STATES.database_not_found
};

const LOCK_STATES = Object.assign({}, COMMON_STATES, {
	no_lock_waits: {
		title: 'No lock waits found',
		text: db => 'At the time shown, no InnoDB lock wait was found on tables in ' + db + '. This covers row and table lock waits at that moment only: metadata locks, other databases and waits that began or ended a moment earlier are not shown.'
	},
	insufficient_privileges: {
		title: 'Unable to read lock information',
		text: () => 'Your MySQL account cannot read the InnoDB lock tables in Performance Schema. A DBA can grant SELECT on the lock columns this view reads. Those privileges show lock waits for every schema, so granting them is a DBA decision. The validator never requests or changes privileges.',
		docs: 'Required privileges'
	},
	performance_schema_disabled: {
		title: 'Performance Schema is disabled',
		text: () => 'This server runs with performance_schema off, so lock waits cannot be read from it. It can only be enabled at server startup. The validator never changes server settings.'
	}
});

const SNAPSHOT_VIEWS = {
	performance: {
		prefix: 'perf', route: 'observed-performance', list: 'families', docs: PERF_DOCS,
		subject: 'Historical workload', loadLabel: 'Load historical workload', emptyStatus: 'no_observations',
		reading: 'Four fixed queries; nothing you submitted is sent.',
		intro: db => 'Load the statement families MySQL has recorded for ' + db + ': executions, recorded time and rows examined, ranked by total recorded time.',
		facts: [
			['Historical', 'Counts and recorded time accumulate from when MySQL first saw each statement family. They describe past workload, not the SQL you are reviewing.'],
			['Fixed queries', 'The validator sends four fixed Performance Schema queries of its own. Submitted SQL is never executed and no risk assessment changes.'],
			['Permissions', 'Your MySQL account needs SELECT on the digest summary columns. That privilege is server-wide.']
		],
		states: PERF_STATES,
		hasData: data => data.status === 'available' && data.families.length > 0,
		render: data => renderPerfSnapshot(data),
		loaded: data => 'Snapshot loaded: ' + data.families.length + ' statement families.'
	},
	locks: {
		prefix: 'lock', route: 'lock-waits', list: 'waits', docs: LOCK_DOCS,
		subject: 'Lock diagnostics', loadLabel: 'Read lock waits', emptyStatus: 'no_lock_waits',
		reading: 'Three fixed queries; nothing you submitted is sent and no session is touched.',
		intro: db => 'Read the InnoDB lock waits on tables in ' + db + ' right now: which session waits, which session blocks it, and on which table and index.',
		facts: [
			['Snapshot', 'Locks change within seconds. The result describes one moment; refresh it before acting on it.'],
			['Fixed queries', 'The validator sends three fixed Performance Schema queries of its own. It never kills, commits or rolls back anything, and never reads row values or SQL text.'],
			['Permissions', 'Your MySQL account needs SELECT on lock columns in Performance Schema. Those privileges show lock waits for every schema.']
		],
		states: LOCK_STATES,
		hasData: data => data.status === 'available' && data.waits.length > 0,
		render: data => renderLockSnapshot(data),
		loaded: data => 'Snapshot loaded: ' + data.waits.length + ' lock waits.'
	}
};
Object.values(SNAPSHOT_VIEWS).forEach(view => Object.assign(view,
	{ result: null, key: null, loading: false, seen: null, generation: 0, loadingGeneration: null }));

if (PUBLIC_DEMO) document.querySelectorAll('.private-only').forEach(node => node.remove());
else Object.values(SNAPSHOT_VIEWS).forEach(view =>
	viewNode(view, 'LoadBtn').addEventListener('click', () => loadSnapshot(view)));

// Event listeners
[host, port, username, password].forEach(input => input.addEventListener('input', syncCredentials));
// Typing while in Local mode means local work: never switch it to a company target.
[host, port, username, password].forEach(input => input.addEventListener('input', () => {
	if (!state.remoteTarget) forgetRememberedTarget();
}));
testBtn.addEventListener('click', testConnection);
discoverBtn.addEventListener('click', discoverDatabases);
validateBtn.addEventListener('click', validateSql);
// Views in one page: the dashboard, the SQL review workspace and (downloaded
// application only) observed performance and lock diagnostics. Switching is page state only; the current
// review stays in all of them.
const VIEWS = PUBLIC_DEMO ? ['dashboard', 'review'] : ['dashboard', 'review', 'performance', 'locks'];
function showView(name) {
	if (!VIEWS.includes(name)) return;
	VIEWS.forEach(view => { $(view + 'View').hidden = view !== name; });
	document.querySelectorAll('.nav-item').forEach(item => {
		const active = item.getAttribute('data-view') === name;
		item.classList.toggle('active', active);
		if (active) item.setAttribute('aria-current', 'page');
		else item.removeAttribute('aria-current');
	});
	window.scrollTo(0, 0);
}

function startReview() {
	showView('review');
	sqlInput.focus();
}

function openStatement(id) {
	const card = $(id);
	showView('review');
	if (!card) return;
	card.setAttribute('tabindex', '-1');
	card.scrollIntoView({ block: 'start' });
	card.focus({ preventScroll: true });
}

document.addEventListener('click', event => {
	const link = event.target.closest('#overview a[href^="#statement-"], .assessment a[href^="#statement-"], .stmt-index a[href^="#statement-"]');
	if (link) {
		event.preventDefault();
		openStatement(link.getAttribute('href').slice(1));
		return;
	}
	const control = event.target.closest('[data-view], [data-action="new-review"]');
	if (!control) return;
	if (control.getAttribute('data-action') === 'new-review') startReview();
	else showView(control.getAttribute('data-view'));
});
renderOverview('idle');
pairBtn.addEventListener('click', pairConnector);
unpairBtn.addEventListener('click', unpairConnector);
loadTargetsBtn.addEventListener('click', loadApprovedTargets);
targetSelect.addEventListener('change', () => { forgetRememberedTarget(); selectTarget(targetSelect.value); });
requestTargetBtn.addEventListener('click', () => requestTarget(''));
confirmTargetBtn.addEventListener('click', confirmTarget);
reapproveBtn.addEventListener('click', () => { if (state.remoteTarget) requestTarget(state.remoteTarget.target_id); });
newTargetTrust.addEventListener('change', () => { $('newTargetCaField').hidden = newTargetTrust.value !== 'bundle'; });
database.addEventListener('change', () => {
	state.selectedDatabase = database.value;
	updateContext();
});

// Ctrl/Cmd + Enter in the editor runs the same Validate action.
sqlInput.addEventListener('keydown', event => {
	if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !validateBtn.disabled) {
		event.preventDefault();
		validateBtn.click();
	}
});

// Appearance: Light / Dark / System. Only the theme word is stored, under 'mdv-theme';
// the only other stored values are update versions (below).
const THEMES = ['light', 'dark', 'system'];
function applyTheme(choice) {
	if (choice === 'light' || choice === 'dark') document.documentElement.setAttribute('data-theme', choice);
	else document.documentElement.removeAttribute('data-theme');
}
function readTheme() {
	try {
		const stored = localStorage.getItem('mdv-theme');
		return THEMES.includes(stored) ? stored : 'system';
	} catch (_) { return 'system' }
}
function saveTheme(choice) {
	if (!THEMES.includes(choice)) return;
	try { localStorage.setItem('mdv-theme', choice) } catch (_) { }
}
const savedTheme = readTheme();
applyTheme(savedTheme);
document.querySelectorAll('input[name="theme"]').forEach(radio => {
	radio.checked = radio.value === savedTheme;
	radio.addEventListener('change', () => {
		if (!radio.checked) return;
		applyTheme(radio.value);
		saveTheme(radio.value);
	});
});

// Tells the desktop app this page is still open, so it does not stop for being
// idle. Carries no data; a failure only means the app is not running.
const HEARTBEAT_MS = 60000;
function sendHeartbeat() {
	fetch('/api/heartbeat', { method: 'POST', cache: 'no-store' }).catch(() => { });
}
if (!PUBLIC_DEMO) {
	sendHeartbeat();
	setInterval(sendHeartbeat, HEARTBEAT_MS);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'visible') sendHeartbeat();
	});
}

// Update notice and download. The backend checks for a newer official release at most
// once per start (backend/update_check.py); the page only asks its own backend. Shown
// only for a newer version whose link is the official release page. Dismissal ("Later")
// is remembered per version under 'mdv-update-dismissed' (just the version, and only
// when storage is available; otherwise only for this page).
//
// The packaged Windows app can download and verify the release ZIP itself
// (backend/update_download.py); the ring and bar show the bytes actually received.
// It never installs: once verified, the user extracts the ZIP and opens the new copy.
// Elsewhere the Download link is the official file, downloaded by the browser. The
// version being installed is kept under 'mdv-update-pending' so that the new copy can
// confirm it is running.
const UPDATE_VERSION = /^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$/;
const RELEASE_PAGE = 'https://github.com/realspinn/mysql-dba-validator/releases/tag/v';
const RELEASE_DOWNLOAD = 'https://github.com/realspinn/mysql-dba-validator/releases/download/v';
const UPDATE_BUILDS = { 'Windows x64': 'windows-x64', 'macOS Apple Silicon': 'macos-arm64-unsigned' };
const UPDATE_POLL_MS = 400;
let updateDismissedHere = '';
let updateOffer = null;
let updateAnnounced = '';
let updatePollTimer = 0;
function readUpdateDismissal() {
	try {
		const stored = localStorage.getItem('mdv-update-dismissed');
		return UPDATE_VERSION.test(stored || '') ? stored : '';
	} catch (_) { return '' }
}
function saveUpdateDismissal(version) {
	if (!UPDATE_VERSION.test(version)) return;
	try { localStorage.setItem('mdv-update-dismissed', version) } catch (_) { }
}
function readUpdatePending() {
	try {
		const stored = localStorage.getItem('mdv-update-pending');
		return UPDATE_VERSION.test(stored || '') ? stored : '';
	} catch (_) { return '' }
}
function saveUpdatePending(version) {
	if (!UPDATE_VERSION.test(version)) return;
	try { localStorage.setItem('mdv-update-pending', version) } catch (_) { }
}
function clearUpdatePending() {
	try { localStorage.removeItem('mdv-update-pending') } catch (_) { }
}
function updateDownloadOffer(download, version) {
	if (!download || typeof download !== 'object' || !Object.hasOwn(UPDATE_BUILDS, download.platform)) return null;
	const name = 'MySQL-DBA-Validator-v' + version + '-' + UPDATE_BUILDS[download.platform] + '.zip';
	if (download.name !== name || download.url !== RELEASE_DOWNLOAD + version + '/' + name
		|| !Number.isSafeInteger(download.size) || download.size <= 0) return null;
	return {
		name, url: download.url, size: download.size, platform: download.platform,
		inApp: download.in_app === true && download.platform === 'Windows x64',
	};
}
function megabytes(bytes) { return (bytes / 1e6).toFixed(1) + ' MB' }
// Draws one stage. `progress` is a fraction of real bytes, or null for "not measured".
function showUpdateStage(stage, texts, progress, actions) {
	const notice = $('updateNotice');
	notice.dataset.stage = stage;
	$('updateTitle').textContent = texts.title;
	$('updateNoticeText').textContent = texts.text;
	$('updateNoticeDetail').textContent = texts.detail;
	const meter = $('updateProgress');
	meter.classList.toggle('indeterminate', progress === null);
	if (progress === null) meter.removeAttribute('aria-valuenow');
	else meter.setAttribute('aria-valuenow', String(Math.round(progress * 100)));
	const percent = progress === null ? 0 : Math.min(100, Math.max(0, progress * 100));
	$('updateRing').style.strokeDashoffset = String(100 - percent);
	$('updateBar').style.transform = 'scaleX(' + percent / 100 + ')';
	const hadFocus = notice.contains(document.activeElement);
	const action = $('updateAction');
	action.hidden = !actions.primary;
	if (actions.primary) {
		action.textContent = actions.primary;
		action.onclick = actions.onPrimary;
	}
	const link = $('updateDownloadLink');
	link.hidden = !actions.link;
	if (actions.link) {
		link.href = actions.link;
		link.onclick = actions.onLink;
	}
	const dismiss = $('updateNoticeDismiss');
	dismiss.textContent = actions.secondary;
	dismiss.onclick = actions.onSecondary;
	// Screen readers hear each new stage once, not every byte count.
	if (updateAnnounced !== stage) {
		updateAnnounced = stage;
		$('updateLive').textContent = texts.title + '. ' + texts.text;
		if (hadFocus) (actions.primary ? action : dismiss).focus();
	}
	notice.hidden = false;
}
function laterUpdate() {
	updateDismissedHere = updateOffer.version;
	saveUpdateDismissal(updateOffer.version);
	$('updateNotice').hidden = true;
}
function postUpdate(path) {
	return fetch(path, { method: 'POST', cache: 'no-store' })
		.then(response => response.ok || response.status === 409 ? response.json() : null)
		.catch(() => null);
}
function showUpdateOffer(note) {
	const offer = updateOffer;
	const download = offer.download;
	const text = 'MySQL DBA Validator ' + offer.version + (offer.current ? ' · you have ' + offer.current : '');
	let detail = 'Download it from the release notes page.';
	if (download) detail = download.platform + ' · ' + megabytes(download.size)
		+ (download.inApp ? '' : ' · downloaded by your browser');
	showUpdateStage('available', { title: 'Update available', text, detail: note || detail }, 0, {
		primary: download && download.inApp ? 'Update' : '', onPrimary: startUpdateDownload,
		link: download && !download.inApp ? download.url : '',
		onLink: () => saveUpdatePending(offer.version),
		secondary: 'Later', onSecondary: laterUpdate,
	});
}
function startUpdateDownload() {
	$('updateAction').disabled = true;
	postUpdate('/api/update-download').then(snapshot => {
		$('updateAction').disabled = false;
		if (snapshot && snapshot.error === 'update_download_running') postUpdate('/api/update-progress').then(followUpdate);
		else if (snapshot && typeof snapshot.stage === 'string') followUpdate(snapshot);
		else showUpdateOffer('The download could not be started. Try again.');
	});
}
function pollUpdate() {
	updatePollTimer = setTimeout(() => postUpdate('/api/update-progress').then(followUpdate), UPDATE_POLL_MS);
}
// Shows the backend's download state and keeps polling while it is under way: one
// timer at a time, and a failed poll during a download is tried again.
function followUpdate(snapshot) {
	clearTimeout(updatePollTimer);
	const offer = updateOffer;
	if (!offer) return;
	if (!snapshot) {
		if (['downloading', 'verifying'].includes($('updateNotice').dataset.stage)) pollUpdate();
		return;
	}
	if (snapshot.version !== offer.version) return;
	const received = Number.isSafeInteger(snapshot.received) ? snapshot.received : 0;
	const total = Number.isSafeInteger(snapshot.total) && snapshot.total > 0 ? snapshot.total : 0;
	const text = 'MySQL DBA Validator ' + offer.version;
	const cancel = { secondary: 'Cancel', onSecondary: () => postUpdate('/api/update-cancel').then(followUpdate) };
	if (snapshot.stage === 'downloading') {
		const fraction = total ? Math.min(1, received / total) : null;
		const detail = total ? megabytes(received) + ' of ' + megabytes(total) + ' · ' + Math.floor(fraction * 100) + '%'
			: megabytes(received);
		showUpdateStage('downloading', { title: 'Downloading update', text, detail }, fraction, cancel);
	} else if (snapshot.stage === 'verifying') {
		showUpdateStage('verifying', {
			title: 'Verifying download', text,
			detail: 'Checking the size, the SHA-256 digest from GitHub and the archive contents.',
		}, null, cancel);
	} else if (snapshot.stage === 'ready') {
		saveUpdatePending(offer.version);
		showUpdateStage('ready', {
			title: 'Ready to install', text: text + ' · verified',
			detail: 'Close MySQL DBA Validator, extract the ZIP, then open MySQL-DBA-Validator.exe in the new folder. Your saved settings are kept.',
		}, 1, {
			primary: 'Show in folder', onPrimary: () => postUpdate('/api/update-show'),
			secondary: 'Later', onSecondary: laterUpdate,
		});
	} else if (snapshot.stage === 'failed') {
		const message = typeof snapshot.message === 'string' && snapshot.message.length <= 200
			? snapshot.message : 'The update could not be downloaded.';
		showUpdateStage('failed', { title: 'Update not downloaded', text, detail: message }, 0, {
			primary: 'Try again', onPrimary: startUpdateDownload, secondary: 'Later', onSecondary: laterUpdate,
		});
	} else if (snapshot.stage === 'cancelled') {
		showUpdateOffer('Download cancelled. Nothing was changed.');
	}
	if (snapshot.stage === 'downloading' || snapshot.stage === 'verifying') pollUpdate();
}
function versionParts(version) { return version.split('.').map(Number) }
function versionBefore(a, b) {
	const [x, y] = [versionParts(a), versionParts(b)];
	for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] < y[i];
	return false;
}
// After an update: the new copy confirms the version it is actually running. The older
// copy, opened again before the new one is installed, leaves the pending version alone.
function confirmUpdated(current) {
	const pending = readUpdatePending();
	if (!pending || !current || versionBefore(current, pending)) return false;
	clearUpdatePending();
	if (pending !== current) return false;
	updateOffer = { version: current };
	$('updateNoticeLink').href = RELEASE_PAGE + current;
	showUpdateStage('updated', {
		title: 'Update complete', text: 'MySQL DBA Validator ' + current + ' is running.', detail: '',
	}, 1, { secondary: 'Close', onSecondary: () => { $('updateNotice').hidden = true } });
	return true;
}
function renderUpdateNotice(data) {
	const notice = $('updateNotice');
	const current = data && typeof data.current === 'string' && UPDATE_VERSION.test(data.current) ? data.current : '';
	if (confirmUpdated(current)) return;
	const version = data && data.state === 'update_available' && typeof data.version === 'string'
		&& UPDATE_VERSION.test(data.version) ? data.version : '';
	if (!version || data.url !== RELEASE_PAGE + version || version === updateDismissedHere
		|| version === readUpdateDismissal()) {
		notice.hidden = true;
		return;
	}
	updateOffer = { version, current, download: updateDownloadOffer(data.download, version) };
	$('updateNoticeLink').href = RELEASE_PAGE + version;
	showUpdateOffer('');
	// A download started earlier (or in another tab) carries on where it is.
	if (updateOffer.download && updateOffer.download.inApp) postUpdate('/api/update-progress').then(followUpdate);
}
// The online demo is not an installed copy: there is nothing to update.
if (!PUBLIC_DEMO) fetch('/api/update-status', { method: 'POST', cache: 'no-store' })
	.then(response => response.ok ? response.json() : null)
	.then(renderUpdateNotice)
	.catch(() => { });

// Initial state
if (PUBLIC_DEMO) {
	document.querySelector('#reviewView .view-lede').textContent =
		'Paste SQL and validate it. The online demo analyses the SQL text only.';
	document.querySelector('#sqlHeading + .panel-note').textContent = 'Paste up to 16 MySQL statements, up to 64 KiB.';
	document.querySelector('#results .empty span').textContent =
		'Paste SQL and choose Validate SQL. The online demo runs static analysis only; database evidence is available in the downloaded application.';
}
updateContext();
