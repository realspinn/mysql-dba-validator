"""The served page renders every evidence state as one coherent story (real Chrome).

Real /api/validate (and connector) responses, produced with the fake MySQL, are fed to
the page's own renderResults() in headless Chrome; the rendered text is then checked
against the response's evidence state. Skipped when Chrome is not installed.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pymysql
import pytest
from fastapi.encoders import jsonable_encoder

import backend.db as backend_db
from tests import test_connector_remote_evidence as remote
from tests.test_local_evidence_credentials import DECOY, FakeMySQL, explain_row, mysql_error, post

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

HEADLINES = {
    "static_only": "Assessment based on SQL structure only.",
    "unavailable": "Database evidence could not be collected.",
    "partial": "Partial database evidence: some evidence was collected and some could not be.",
    "not_applicable": "Connected to the database; no database evidence applies to this statement.",
}
# "collected" names what was collected: table metadata alone says nothing about row counts.
COLLECTED_HEADLINES = {
    "both": "Collected: table metadata and an execution plan (optimizer estimates).",
    "metadata": "Collected: table metadata only.",
    "plan": "Collected: an execution plan only (optimizer estimates).",
}


def expected_headline(state):
    if state["overall"] != "collected":
        return HEADLINES[state["overall"]]
    metadata = state["metadata"]["state"] == "collected"
    plan = state["plan"]["state"] == "collected"
    return COLLECTED_HEADLINES["both" if metadata and plan else "plan" if plan else "metadata"]


# Review L4: states the validator's policy, not unverified MySQL 8.4 behaviour.
READ_ONLY_PLAN = ("Execution plan not requested: the validator does not request plans for UPDATE or DELETE, "
                  "because evidence connections are read-only and it never asks for write access.")
INDIRECT = " Triggers and foreign-key actions (such as ON DELETE CASCADE) were not checked."
PLAN_COLLECTED = "Execution-plan evidence was collected (optimizer estimates, not rows changed)."
NOT_ELIGIBLE_PLAN = ("No execution plan was requested: this statement is not eligible for planning "
                     "(for example SELECT ... INTO, or a function call that is not on the reviewed list "
                     "of MySQL built-ins).")
DUMP_DOM_DEADLINE_SECONDS = 120


def find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def dump_dom(command: list[str], work: Path,
             deadline: float = DUMP_DOM_DEADLINE_SECONDS) -> tuple[str, str, int | None]:
    """Run a headless browser with --dump-dom: (document, stderr, exit code, or None if it was stopped).

    The document is complete when it ends with </html>: --dump-dom writes it in one
    piece once the page has loaded. The browser is then stopped instead of waited for;
    on a display-less macOS runner Chrome printed the whole page but never exited.
    Output goes to files, not pipes, because helper processes inherit the handles and a
    pipe stays open while any of them lives. A browser that exits, or reaches the
    deadline, without a complete document leaves the caller to fail on what it got.
    """
    out_path, err_path = work / "dump-dom.html", work / "browser-stderr.txt"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   start_new_session=os.name != "nt")
    try:
        end = time.monotonic() + deadline
        while process.poll() is None and time.monotonic() < end:
            if out_path.read_bytes().rstrip().endswith(b"</html>"):
                break
            time.sleep(0.2)
        code = process.poll()
    finally:
        stop_browser(process)
    return (out_path.read_text(encoding="utf-8", errors="replace"),
            err_path.read_text(encoding="utf-8", errors="replace"), code)


def stop_browser(process: subprocess.Popen) -> None:
    """Stop the browser and, on POSIX, every helper in its process group."""
    if os.name == "nt":
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:  # the whole group is already gone
            break
        except PermissionError:
            # macOS answers EPERM, not ESRCH, while the group still exists but has no member
            # it can signal (XNU killpg1 skips zombies), e.g. helpers that exited but are not
            # yet reaped. The browser is ours: while it runs it can be signalled, so EPERM
            # then is a real failure.
            if process.poll() is None:
                raise
            break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    process.wait(timeout=30)


def local_fixture(sql, server=None, **overrides):
    with pytest.MonkeyPatch.context() as patch:
        server = server or FakeMySQL()
        for key, value in DECOY.items():
            patch.setenv(key, value)
        patch.setattr(backend_db, "_client", None)
        patch.setattr(pymysql, "connect", lambda **kwargs: server.connect(**kwargs))
        response = post(sql, **overrides)
        assert response.status_code == 200, response.text
        return response.json()


def remote_fixture(sql, server, **kwargs):
    with pytest.MonkeyPatch.context() as patch:
        response, _ = remote.remote_validate(patch, server, sql, **kwargs)
        assert response.status_code == 200, response.text
        return jsonable_encoder(response.json())


BATCH_16 = [
    "INSERT INTO orders (id, status) VALUES (1, 'new')", "SELECT * FROM orders WHERE id = 1",
    "UPDATE orders SET status = 'x' WHERE id = 1", "DELETE FROM orders WHERE id = 1",
    "INSERT INTO orders (id, status) VALUES (2, 'new')", "SELECT COUNT(*) FROM orders",
    "UPDATE orders SET status = 'x' WHERE id = 2", "DELETE FROM orders WHERE id = 2",
    "UPDATE orders SET status = 'x' WHERE created_at < '2024-01-01'", "SELECT * FROM orders",
    "DELETE FROM orders", "INSERT INTO orders (id, status) VALUES (3, 'new')",
    "SELECT * FROM orders WHERE id = 3", "UPDATE orders SET status = 'y' WHERE id > 0",
    "DELETE FROM orders WHERE id = 3", "SELECT * FROM orders WHERE status = 'old'",
]


def build_fixtures() -> dict:
    missing_orders = FakeMySQL()
    real = missing_orders.rows_for

    def no_orders(sql, args, connection):
        if "information_schema.TABLES" in sql and args and args[1] == "orders":
            missing_orders.executed.append(sql.strip())
            return []
        return real(sql, args, connection)

    missing_orders.rows_for = no_orders
    return {
        "static_select": local_fixture("SELECT * FROM users WHERE id = 1", username=None, password=None),
        "local_select_plan": local_fixture("SELECT * FROM users WHERE status = 'old'"),
        "local_update": local_fixture("UPDATE users SET active = 0 WHERE status = 'old'"),
        "local_delete": local_fixture("DELETE FROM users WHERE status = 'old'"),
        "auth_failed": local_fixture("UPDATE users SET active = 0 WHERE status = 'old'",
                                     FakeMySQL(connect_error=mysql_error(1045, "Access denied"))),
        "metadata_failed": local_fixture("SELECT * FROM users WHERE status = 'old'",
                                         FakeMySQL(metadata_error=mysql_error(1142, "SELECT command denied"))),
        "select_into": local_fixture("SELECT * INTO @a FROM users"),
        # A locking read: analysed (MEDIUM, LIMITED), never planned.
        "locking_read": local_fixture("SELECT * FROM users WHERE id = 1 FOR UPDATE"),
        "trailing_select_into": local_fixture("SELECT * FROM users INTO OUTFILE '/tmp/x'"),
        "not_applicable": local_fixture("SELECT 1"),
        "connected_unavailable": local_fixture("UPDATE users SET active = 0 WHERE id = 1",
                                               FakeMySQL(metadata_error=mysql_error(1142, "SELECT command denied"))),
        "remote_no_database": remote_fixture("UPDATE users SET active = 0 WHERE id = 5",
                                             remote.FakeServer(explain=[remote.explain_row("const", 1, key="PRIMARY")]),
                                             database=None),
        "mixed_batch": local_fixture("SELECT * FROM users WHERE id = 1; UPDATE users SET active = 0 WHERE id = 2;"),
        "missing_table": local_fixture("UPDATE orders SET active = 0 WHERE id = 1", missing_orders),
        "remote_dml": remote_fixture("UPDATE users SET active = 0 WHERE status = 'old'",
                                     remote.FakeServer(explain=[remote.explain_row("ALL", 500)])),
        # Metadata confirms PRIMARY (id, int): the only narrow-scope proof.
        "verified_key": local_fixture("UPDATE users SET active = 0 WHERE id = 1"),
        # The same SQL with no database: a column named id proves nothing.
        "static_key": local_fixture("UPDATE users SET active = 0 WHERE id = 1", username=None, password=None),
        "no_where": local_fixture("DELETE FROM users", username=None, password=None),
        # Sixteen mixed statements (the batch limit), SELECT plans from the fake server.
        "batch16": local_fixture("; ".join(BATCH_16)),
        # A statement the parser reads only by its keyword, beside an analysed SELECT; a
        # server setting; SQL the parser would regenerate differently (shown as submitted).
        "opaque_command": local_fixture("LOCK TABLES users WRITE; SELECT * FROM users WHERE id = 1"),
        "server_setting": local_fixture("SET GLOBAL read_only = 0", username=None, password=None),
        "submitted_text": local_fixture("select id from users where name REGEXP '^a' LIMIT 20, 10",
                                        username=None, password=None),
        # Optimization advice beside the result (advisory only), and a statement without any.
        "optimization": local_fixture("SELECT id FROM users WHERE name LIKE '%son' ORDER BY RAND() LIMIT 5; "
                                      "UPDATE users SET active = 0 WHERE id = 1",
                                      username=None, password=None),
        # Analysed, valid-but-unsupported, invalid and unreadable statements in one batch.
        "not_analysed": local_fixture("SELECT * FROM users WHERE id = 1; VALUES ROW(1); SELEC x; XA START 'x'",
                                      username=None, password=None),
        # LIMIT with a sort: MySQL reads and sorts every matching row before LIMIT applies.
        "limit_filesort": local_fixture(
            "SELECT * FROM users ORDER BY created_at LIMIT 10",
            FakeMySQL(explain=[explain_row("ALL", 1_902_192, extra="Using filesort")])),
        "multi_row_plan": local_fixture(
            "SELECT * FROM users JOIN orders ON orders.user_id = users.id",
            FakeMySQL(explain=[explain_row("ALL", 500), explain_row("ref", 3, key="idx_user")])),
        "mixed_confidence": local_fixture("DROP TABLE audit_log; UPDATE users SET active = 0 WHERE status = 'old';",
                                          username=None, password=None),
    }


HARNESS = """
<script>
const FIXTURES = %s;
const OUT = {};
const overviewText = () => document.getElementById('overview').textContent;
const EMPTY_OVERVIEW = overviewText();
for (const [name, data] of Object.entries(FIXTURES)) {
  renderResults(data);
  const root = document.getElementById('results');
  const cells = root.querySelectorAll('.summary-cell');
  OUT[name] = {
    mode: cells[2].querySelector('strong').textContent,
    batchConfidence: cells[3].querySelector('strong').textContent,
    summary: root.querySelector('.evidence-summary').textContent,
    assessment: [...root.querySelectorAll('.assessment .assess-row')].map(row => row.textContent),
    assessmentBeforeCards: (() => { const a = root.querySelector('.assessment'); const c = root.querySelector('article.statement');
      return !!a && !!c && !!(a.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING); })(),
    overview: overviewText(),
    findingLinks: [...document.querySelectorAll('#overview .ov-finding a')].map(a => a.getAttribute('href')),
    cardIds: [...root.querySelectorAll('article.statement')].map(card => card.id),
    indexRows: [...root.querySelectorAll('.stmt-index tbody tr')].map(row => [...row.cells].map(cell => cell.textContent)),
    indexLinks: [...root.querySelectorAll('.stmt-index a')].map(a => a.getAttribute('href')),
    composition: {
      band: document.querySelectorAll('#overview .ov-band > .ov-tile').length,
      tableRows: document.querySelectorAll('#overview table.ov-table tbody tr').length,
      oldTilesOrCards: document.querySelectorAll('#overview .ov-kpis, #overview .ov-card, #overview .onboard').length,
    },
    statements: [...root.querySelectorAll('article.statement')].map(card => ({
      headline: card.querySelector('.evidence-headline').textContent,
      evidence: [...card.querySelectorAll('.layer.evidence p')].map(p => p.textContent),
      metadata: [...card.querySelectorAll('.layer.metadata p')].map(p => p.textContent),
      planGrids: card.querySelectorAll('.layer.evidence .metric-grid').length,
      scope: (card.querySelector('.row-scope') || {}).textContent || null,
      warning: (card.querySelector('.analysis-warning') || {}).textContent || null,
      confidence: card.querySelector('.metric.confidence b').textContent,
      why: [...card.querySelector('.layer.final ul.plain-list').children].map(li => li.textContent),
      optimization: (() => { const box = card.querySelector('details.optimization');
        const look = node => node ? (s => [s.fontWeight, s.color, s.fontSize])(getComputedStyle(node)) : null;
        return box ? { summary: box.querySelector('summary').textContent, open: box.open,
                       headings: [...box.querySelectorAll('.subheading')].map(h => h.textContent),
                       caveatLabels: [...box.querySelectorAll('.caveats-label')].map(p => p.textContent),
                       caveatLists: box.querySelectorAll('.caveats-label + ul.caveats').length,
                       caveatLook: look(box.querySelector('ul.caveats li')),
                       actionLook: look([...box.querySelectorAll('p')].find(p => p.textContent.startsWith('Suggested action'))),
                       text: box.textContent } : null; })(),
      text: card.textContent,
    })),
    aiAdvisor: [...root.querySelectorAll('.ai-teaser')].map(box => ({
      label: box.getAttribute('aria-label'),
      title: box.querySelector('.ai-teaser-title').textContent,
      badge: box.querySelector('.ai-badge').textContent,
      text: box.querySelector('.ai-teaser-text').textContent,
      privacy: box.querySelector('.ai-privacy').textContent,
      marks: [...box.querySelectorAll('svg')].map(svg => svg.getAttribute('aria-hidden')),
      external: box.querySelectorAll('img, image, use, link, iframe, [src], [href]').length,
      // The only control is the close button.
      controls: box.querySelectorAll('a, button:not(.close-x), input, select, textarea, form, [onclick], [tabindex]').length,
      close: [...box.querySelectorAll('button.close-x')].map(b => [b.type, b.getAttribute('aria-label'), b.textContent.trim()]),
      inCard: !!box.closest('article.statement'),
      afterCards: (() => { const cards = root.querySelectorAll('article.statement');
        return !!(cards[cards.length - 1].compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING); })(),
    })),
  };
}
// The AI Advisor teaser in each theme: text colours and the surface they sit on.
renderResults(FIXTURES.optimization);
const AI_THEMES = {};
for (const theme of ['light', 'dark']) {
  document.documentElement.setAttribute('data-theme', theme);
  const box = document.querySelector('.ai-teaser');
  const colour = selector => getComputedStyle(box.querySelector(selector)).color;
  AI_THEMES[theme] = {
    surface: getComputedStyle(document.documentElement).getPropertyValue('--surface').trim(),
    title: colour('.ai-teaser-title'), badge: colour('.ai-badge'), text: colour('.ai-teaser-text'),
    privacy: colour('.ai-privacy'), mark: getComputedStyle(box.querySelector('.ai-mark')).backgroundImage,
    titleSize: getComputedStyle(box.querySelector('.ai-teaser-title')).fontSize,
  };
}
document.documentElement.removeAttribute('data-theme');
// Closing the teaser: gone at once and for later results on this page, with nothing stored.
const storageBefore = [localStorage.length, sessionStorage.length, document.cookie];
const teaserCount = () => document.querySelectorAll('#results .ai-teaser').length;
const AI_CLOSE = { before: teaserCount() };
showView('review');  // the teaser is on the SQL review view; hidden elements take no focus
const closeButton = document.querySelector('#results .ai-teaser .close-x');
closeButton.focus();
closeButton.click();
AI_CLOSE.afterClick = teaserCount();
// Focus stays near where it was (the last Optimization summary), not back at the top.
AI_CLOSE.focusAfterClick = document.activeElement
  === [...document.querySelectorAll('#results details.optimization summary')].pop();
renderResults(FIXTURES.optimization);
AI_CLOSE.afterNextResult = teaserCount();
AI_CLOSE.storageUnchanged = JSON.stringify([localStorage.length, sessionStorage.length, document.cookie])
  === JSON.stringify(storageBefore);
aiTeaserClosed = false;  // as after a reload: nothing remembers it
renderResults(FIXTURES.optimization);
AI_CLOSE.afterReload = teaserCount();
renderResults(FIXTURES.batch16);
document.querySelectorAll('.stmt-index a')[9].click();
const BATCH_FOCUS = document.activeElement.id;
renderLoading();
const RUNNING_OVERVIEW = overviewText();
renderError('Validation failed: example');
OUT.__states = { empty: EMPTY_OVERVIEW, running: RUNNING_OVERVIEW, failed: overviewText(), batchFocus: BATCH_FOCUS,
                 aiThemes: AI_THEMES, aiClose: AI_CLOSE };
const out = document.createElement('pre');
out.id = 'render-out';
out.textContent = JSON.stringify(OUT);
document.body.append(out);
</script>
</body>"""


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("Chrome is not installed")
    fixtures = build_fixtures()
    page = FRONTEND.read_text(encoding="utf-8")
    payload = json.dumps(fixtures).replace("</", "<\\/")
    work = tmp_path_factory.mktemp("render")
    harness = work / "harness.html"
    harness.write_text(page.replace("</body>", HARNESS % payload, 1), encoding="utf-8")
    document, stderr, code = dump_dom(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={work / 'profile'}", "--dump-dom", harness.as_uri()], work)
    match = re.search(r'<pre id="render-out">(.*?)</pre>', document, re.S)
    assert match, f"page did not render (exit {code}): {stderr[-500:]}"
    return fixtures, json.loads(html.unescape(match.group(1)))


def test_every_rendered_statement_matches_its_evidence_state(rendered):
    fixtures, out = rendered
    for name, data in fixtures.items():
        assert len(out[name]["statements"]) == data["statement_count"], name
        for statement, view in zip(data["statements"], out[name]["statements"]):
            state = statement["evidence"]
            assert view["headline"] == expected_headline(state), name
            assert view["confidence"] in {"HIGH", "LIMITED", "LOW"}, name
            assert view["confidence"] == statement["confidence"], name
            assert view["why"] == (statement["reasons"] or ["No additional reasons reported."]), name
            text = view["text"]
            # Old binary wording and generic "Partial" never appear.
            assert "Database evidence is unavailable" not in text, name
            assert "Metadata: Partial" not in text, name
            assert "offline" not in text.lower() or state["connection"]["state"] == "not_attempted", name
            if state["metadata"]["state"] == "collected":
                assert "Database metadata was collected." in view["evidence"], name
                assert "Metadata: Collected" in view["metadata"], name
            else:
                assert "metadata was collected" not in text.lower(), name
            if state["plan"]["state"] != "collected":
                assert not any(line.startswith("Execution-plan evidence was collected") for line in view["evidence"])
            # The plan metric grid exists exactly when a plan was collected.
            assert view["planGrids"] == (1 if state["plan"]["state"] == "collected"
                                         and state["plan"]["interpreted"] else 0), name
            # A connected database is never presented as unavailable.
            if state["connection"]["state"] == "connected":
                assert "database unavailable" not in text.lower(), name
                assert "database unavailable" not in out[name]["mode"].lower(), name


def test_static_only_select(rendered):
    _, out = rendered
    view = out["static_select"]
    assert view["mode"] == "Static analysis"
    assert view["summary"] == "Assessment based on SQL structure only."
    assert view["statements"][0]["evidence"][0] == HEADLINES["static_only"]


def test_local_select_with_plan(rendered):
    _, out = rendered
    view = out["local_select_plan"]["statements"][0]
    assert view["evidence"][:4] == [COLLECTED_HEADLINES["both"], "Database metadata was collected.",
                                    "users: table found", PLAN_COLLECTED]
    assert view["confidence"] == "HIGH"
    assert out["local_select_plan"]["mode"] == "Database evidence"


@pytest.mark.parametrize("name", ["local_update", "local_delete"])
def test_local_dml_shows_metadata_and_the_intentional_plan_skip(rendered, name):
    _, out = rendered
    view = out[name]["statements"][0]
    assert view["headline"] == COLLECTED_HEADLINES["metadata"]
    assert READ_ONLY_PLAN in view["evidence"]
    assert "could not be collected" not in view["text"]
    assert view["confidence"] == "LIMITED"
    assert out[name]["mode"] == "Static + database metadata"
    assert out[name]["summary"] == "Execution plan: 1 not collected (read-only connection) · Metadata: 1 collected"
    assert any("table metadata was collected" in reason for reason in view["why"])


def test_invalid_login(rendered):
    _, out = rendered
    view = out["auth_failed"]["statements"][0]
    assert view["evidence"][:2] == [HEADLINES["unavailable"],
                                    "The database rejected your login. Check the username and password."]
    assert "Metadata: Not collected" in view["metadata"]
    assert out["auth_failed"]["mode"] == "Static + database evidence unavailable"


def test_metadata_failure_is_named_and_partial(rendered):
    _, out = rendered
    view = out["metadata_failed"]["statements"][0]
    assert view["headline"] == HEADLINES["partial"]
    assert ("Database metadata could not be collected. Your account was denied access to the table metadata."
            in view["evidence"])
    assert PLAN_COLLECTED in view["evidence"]
    assert "Partial evidence" in out["metadata_failed"]["summary"]


@pytest.mark.parametrize("name", ["locking_read"])
def test_a_select_refused_by_the_plan_gate_is_explained_as_not_eligible(rendered, name):
    fixtures, out = rendered
    assert fixtures[name]["statements"][0]["evidence"]["plan"]["state"] == "not_eligible"
    view = out[name]["statements"][0]
    assert NOT_ELIGIBLE_PLAN in view["evidence"]
    assert not any("this statement type" in line for line in view["evidence"])
    assert view["planGrids"] == 0
    assert out[name]["mode"] == "Static + database metadata"


def test_a_locking_read_is_shown_as_medium_limited_and_never_read_only(rendered):
    from backend.risk_engine import LOCKING_READ_REASON

    fixtures, out = rendered
    statement = fixtures["locking_read"]["statements"][0]
    assert (statement["risk_level"], statement["confidence"]) == ("MEDIUM", "LIMITED")
    view = out["locking_read"]["statements"][0]
    assert view["confidence"] == "LIMITED"
    assert LOCKING_READ_REASON in view["text"]
    assert "Read-only or no data mutation detected." not in view["text"]


def test_select_into_is_shown_as_submitted_and_not_analysed(rendered):
    fixtures, out = rendered
    statement = fixtures["select_into"]["statements"][0]
    assert statement["analysis_status"] == "unreadable"
    assert statement["sql"] == "SELECT * INTO @a FROM users"
    view = out["select_into"]["statements"][0]
    assert "SELECT * INTO @a FROM users" in view["text"] and "CREATE TABLE" not in view["text"]
    assert view["confidence"] == "LOW"
    assert "Incomplete analysis" in out["select_into"]["overview"]


def test_not_applicable_is_connected_not_unavailable(rendered):
    fixtures, out = rendered
    state = fixtures["not_applicable"]["statements"][0]["evidence"]
    assert (state["connection"]["state"], state["overall"]) == ("connected", "not_applicable")
    view = out["not_applicable"]["statements"][0]
    assert view["headline"] == HEADLINES["not_applicable"]
    assert "No table metadata applies to this statement." in view["evidence"]
    assert view["planGrids"] == 0
    assert out["not_applicable"]["mode"] == "Static analysis"


def test_connected_but_evidence_unavailable_never_says_database_unavailable(rendered):
    fixtures, out = rendered
    data = fixtures["connected_unavailable"]
    state = data["statements"][0]["evidence"]
    assert state["connection"] == {"state": "connected", "reason": None}
    assert state["overall"] == "unavailable"
    assert data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"
    view = out["connected_unavailable"]["statements"][0]
    assert out["connected_unavailable"]["mode"] == "Static + database evidence unavailable"
    assert view["headline"] == HEADLINES["unavailable"]
    assert ("Database metadata could not be collected. Your account was denied access to the table metadata."
            in view["evidence"])
    assert READ_ONLY_PLAN in view["evidence"]
    assert "database unavailable" not in (view["text"] + out["connected_unavailable"]["mode"]).lower()
    assert "could not be reached" not in view["text"]


def test_remote_without_a_database_says_metadata_was_not_requested(rendered):
    fixtures, out = rendered
    state = fixtures["remote_no_database"]["statements"][0]["evidence"]
    assert state["metadata"] == {"state": "not_attempted", "reason": "no_database_selected", "tables": []}
    view = out["remote_no_database"]["statements"][0]
    assert "No database was selected, so table metadata was not collected." in view["evidence"]
    assert "Metadata: Not collected" in view["metadata"]
    assert "Database metadata was collected." not in view["evidence"]


def test_mixed_batch_summary_shows_what_was_not_collected(rendered):
    _, out = rendered
    assert out["mixed_batch"]["mode"] == "Database evidence"
    assert out["mixed_batch"]["summary"].startswith(
        "Execution plan: 1 collected, 1 not collected (read-only connection)")


def test_missing_table_is_shown_as_not_found(rendered):
    _, out = rendered
    assert "orders: not found in the selected database" in out["missing_table"]["statements"][0]["evidence"]


def test_remote_dml_renders_with_the_same_contract(rendered):
    _, out = rendered
    view = out["remote_dml"]["statements"][0]
    # Company connections are read-only too: metadata only, the write plan is not requested.
    assert view["headline"] == COLLECTED_HEADLINES["metadata"]
    assert READ_ONLY_PLAN in view["evidence"]
    assert view["planGrids"] == 0
    assert view["confidence"] == "LIMITED"
    assert view["scope"].startswith("Row scope: not established.")


def test_verified_unique_key_scope_is_shown_as_a_bound_not_a_count(rendered):
    fixtures, out = rendered
    statement = fixtures["verified_key"]["statements"][0]
    assert statement["scope"]["basis"] == "at_most_one_row"
    view = out["verified_key"]["statements"][0]
    # Review H3: a direct bound, with indirect effects named as not checked.
    assert view["scope"] == ("Row scope: at most one row of app.users matches directly, by unique key PRIMARY "
                             "(confirmed from table metadata). The row may not exist." + INDIRECT)
    assert view["confidence"] == "HIGH"
    assert out["verified_key"]["batchConfidence"] == "HIGH"


def test_id_column_without_metadata_is_not_presented_as_a_key(rendered):
    _, out = rendered
    view = out["static_key"]["statements"][0]
    assert view["scope"] == ("Row scope: not established. A WHERE clause can match zero, one, some or all rows; "
                             "the SQL alone does not say how many." + INDIRECT)
    assert view["confidence"] == "LIMITED"
    text = view["text"].lower()
    assert "key-scoped" not in text and "specific key" not in text and "at most one row" not in text


def test_write_without_where_has_all_rows_scope_and_high_confidence(rendered):
    _, out = rendered
    view = out["no_where"]["statements"][0]
    assert view["scope"] == ("Row scope: every row of users (from the SQL: no WHERE clause, or one that is "
                             "always true)." + INDIRECT)
    assert view["confidence"] == "HIGH"


def test_sql_read_only_partly_is_shown_as_not_analysed_and_incomplete(rendered):
    fixtures, out = rendered
    statement = fixtures["trailing_select_into"]["statements"][0]
    # Never scored as the SELECT the parser could read: not analysed, shown as submitted.
    assert statement["analysis_status"] == "unreadable" and statement["analysis_text_differs"] is False
    assert statement["sql"] == "SELECT * FROM users INTO OUTFILE '/tmp/x'"
    view = out["trailing_select_into"]["statements"][0]
    assert view["confidence"] == "LOW" and view["warning"] is None
    assert "SELECT * FROM users INTO OUTFILE '/tmp/x'" in view["text"]
    overview = out["trailing_select_into"]["overview"]
    assert "Incomplete analysis" in overview and "Complete" not in overview.replace("Incomplete", "")
    assert "1 of 1 statement was not analysed" in overview
    assert out["select_into"]["statements"][0]["warning"] is None


def test_an_unparsed_command_or_server_setting_makes_the_review_incomplete(rendered):
    fixtures, out = rendered
    lock, select = fixtures["opaque_command"]["statements"]
    assert lock["analysis_status"] == "unreadable" and select["analysis_status"] == "analysed"
    overview = out["opaque_command"]["overview"]
    assert "Incomplete analysis" in overview and "All statements were assessed" not in overview
    assert "1 of 2 statements were not analysed" in overview
    assert "LOCK TABLES users WRITE" in out["opaque_command"]["statements"][0]["text"]
    setting = out["server_setting"]["overview"]
    assert "Incomplete analysis" in setting and "1 of 1 statement was not analysed" in setting


def test_the_statement_card_shows_the_sql_as_submitted(rendered):
    fixtures, out = rendered
    sql = "select id from users where name REGEXP '^a' LIMIT 20, 10"
    assert fixtures["submitted_text"]["statements"][0]["sql"] == sql
    text = out["submitted_text"]["statements"][0]["text"]
    assert sql in text and "REGEXP_LIKE" not in text and "OFFSET" not in text


def test_optimization_advice_is_a_collapsed_advisory_section(rendered):
    _, out = rendered
    select, update = (view["optimization"] for view in out["optimization"]["statements"])
    assert select["summary"] == ("Optimization advice: 2 observations "
                                 "(advisory; does not change the risk score)")
    assert select["open"] is False
    assert select["headings"] == ["LIKE pattern starts with a wildcard (observed in the SQL)",
                                  "ORDER BY RAND() (observed in the SQL)"]
    assert "Suggested action:" in select["text"] and "How to verify:" in select["text"]
    # Each observation labels its caveats, and they read like the suggested action (same
    # normal weight, colour and size), not heavier or brighter.
    assert select["caveatLabels"] == ["Caveats:", "Caveats:"] and select["caveatLists"] == 2
    assert select["caveatLook"] == select["actionLook"] and select["caveatLook"][0] == "400"
    assert update["summary"].startswith("Optimization advice: not checked")
    assert "covers a single SELECT, or a UNION of plain SELECT blocks" in update["text"]
    # Every rendered result has optimization in its own section, never in the risk summary.
    for name, view in out.items():
        if name != "__states":
            assert "Optimization" not in "".join(view["assessment"]) + view["overview"], name


def test_the_ai_advisor_is_a_label_for_a_future_feature(rendered):
    _, out = rendered
    for name, view in out.items():
        if name == "__states":
            continue
        assert len(view["aiAdvisor"]) == 1, name
        note = view["aiAdvisor"][0]
        assert (note["label"], note["title"], note["badge"]) == (
            "AI Advisor, coming soon", "AI Advisor", "COMING SOON"), name
        assert note["text"] == ("Deeper SQL explanations and intelligent optimization suggestions, "
                                "with optional AI assistance in a future version.")
        assert note["privacy"] == "No SQL is sent to an AI service."
        # Decorative inline marks only (the third is the close icon): nothing loaded, below the
        # statements, and no control but an accessible close button.
        assert note["marks"] == ["true", "true", "true"] and note["external"] == 0, name
        assert note["controls"] == 0 and note["inCard"] is False and note["afterCards"] is True, name
        assert note["close"] == [["button", "Dismiss AI Advisor teaser", ""]], name


def test_closing_the_ai_advisor_lasts_until_the_page_is_reloaded(rendered):
    _, out = rendered
    assert out["__states"]["aiClose"] == {"before": 1, "afterClick": 0, "focusAfterClick": True,
                                          "afterNextResult": 0, "storageUnchanged": True, "afterReload": 1}
    # In memory only: the dismissal code never touches browser storage or cookies.
    page = FRONTEND.read_text(encoding="utf-8")
    start = page.index("let aiTeaserClosed")
    code = page[start:page.index("\n\t\t}\n", page.index("function aiAdvisorNote(", start))]
    for persistent in ("localStorage", "sessionStorage", "cookie", "indexedDB"):
        assert persistent not in code, persistent


def contrast(foreground: str, background: str) -> float:
    """WCAG contrast ratio of two colours given as rgb(...) or #rrggbb."""
    def channels(colour):
        colour = colour.strip()
        if colour.startswith("#"):
            return [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
        return [int(float(part)) for part in re.findall(r"[\d.]+", colour)[:3]]

    def luminance(colour):
        linear = [(c / 255) / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4
                  for c in channels(colour)]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_the_ai_advisor_reads_well_in_light_and_dark_themes(rendered):
    _, out = rendered
    themes = out["__states"]["aiThemes"]
    assert themes["light"]["surface"] != themes["dark"]["surface"]
    for theme, look in themes.items():
        for part in ("title", "badge", "text", "privacy"):
            assert contrast(look[part], look["surface"]) >= 4.5, (theme, part)
        assert look["mark"].startswith("linear-gradient"), theme
        # Secondary to the risk assessment: no larger than a statement heading.
        assert look["titleSize"] == "14px", theme
    for part in ("title", "badge", "text", "privacy", "mark"):
        assert themes["light"][part] != themes["dark"][part], part


def test_multi_row_plan_is_shown_without_a_first_row_summary(rendered):
    fixtures, out = rendered
    plan = fixtures["multi_row_plan"]["statements"][0]["evidence"]["plan"]
    assert plan == {"state": "collected", "reason": "multi_row_plan", "rows": 2, "interpreted": False}
    view = out["multi_row_plan"]["statements"][0]
    assert view["planGrids"] == 0
    assert ("Multi-step execution plan (2 rows) collected: shown for reference, not summarised or used for scoring."
            in view["evidence"])


ASSESSMENT_ROWS = ["Top finding", "Execution plan", "Filter and index", "Limitations", "Unknown", "Check first"]


def test_primary_assessment_comes_first_and_claims_nothing_unsupported(rendered):
    fixtures, out = rendered
    for name, data in fixtures.items():
        if name == "__states":
            continue
        view = out[name]
        rows = view["assessment"]
        assert [row[:len(label)] for row, label in zip(rows, ASSESSMENT_ROWS)] == ASSESSMENT_ROWS, name
        assert view["assessmentBeforeCards"] is True, name
        joined = " ".join(rows).lower()
        # Never a safety claim; unknowns only where they apply; no internal metric codes.
        assert "is safe" not in joined.replace("does not mean the sql is safe", ""), name
        writes = any(st["statement_type"] in ("UPDATE", "DELETE") for st in data["statements"])
        assert ("rows changed: not collected" in joined) is writes, name
        assert "run time, locks: not measured" in joined, name
        for card in view["statements"]:
            assert "M1 —" not in card["text"] and "M2 —" not in card["text"], name
            assert "Table scan and estimated row volume" in card["text"], name
            assert "Filter and index assessment" in card["text"], name
    static = " ".join(out["static_select"]["assessment"])
    assert "Not collected: no database login and database." in static
    local = " ".join(out["local_select_plan"]["assessment"])
    assert "Full table scan (access type ALL); optimizer estimate: 500 rows examined." in local
    assert "No index on users starts with status (table metadata)." in local
    assert "Full table scan (EXPLAIN access type ALL)" in local  # the top finding
    missing = " ".join(out["missing_table"]["assessment"])
    assert "orders was not found in the selected database." in missing


def test_plan_line_shows_a_sort_before_limit_and_estimates_stay_estimates(rendered):
    _, out = rendered
    plan = " ".join(out["limit_filesort"]["assessment"])
    assert ("Full table scan (access type ALL); optimizer estimate: 1,902,192 rows examined. "
            "The plan shows Using filesort: every matching row is read and sorted before LIMIT applies.") in plan
    # One row is "row"; a one-row estimate is still labelled an estimate.
    single = " ".join(out["remote_dml"]["assessment"] + out["local_select_plan"]["assessment"])
    assert " 1 rows " not in single


def test_a_metadata_collection_failure_is_listed_as_a_limitation(rendered):
    _, out = rendered
    limits = " ".join(out["metadata_failed"]["assessment"])
    assert "Statement 1: table metadata could not be collected." in limits
    # and the filter check says why it was not done, never that no index exists
    assert "Not checked: complete table and index metadata were not available." in limits
    assert "No index on" not in limits


def test_sixteen_statements_each_get_a_row_a_card_and_a_working_link(rendered):
    fixtures, out = rendered
    data, view = fixtures["batch16"], out["batch16"]
    assert data["statement_count"] == 16
    assert view["cardIds"] == [f"statement-{n}" for n in range(1, 17)]
    assert view["indexLinks"] == [f"#statement-{n}" for n in range(1, 17)]
    rows = view["indexRows"]
    assert len(rows) == 16
    for n, (row, statement) in enumerate(zip(rows, data["statements"]), 1):
        number, what, risk, confidence, key = row
        assert number == f"#{n}"
        assert what.startswith(statement["statement_type"])  # submitted order kept
        assert risk.startswith(statement["risk_level"]) and risk.endswith(str(statement["score"]))
        assert confidence == statement["confidence"]
    # The unrestricted DELETE (statement 11) is the batch's highest risk and is visible as such.
    assert data["overall_score"] == data["statements"][10]["score"] == 100
    assert rows[10][2].startswith("CRITICAL")
    # Clicking the 10th row opens and focuses statement 10's card.
    assert out["__states"]["batchFocus"] == "statement-10"
    # Identical per-statement lines are merged, not repeated sixteen times.
    plan_row = next(row for row in view["assessment"] if row.startswith("Execution plan"))
    assert plan_row.count("Not collected: plans are not requested for UPDATE or DELETE.") == 1


def test_statements_table_says_why_a_statement_was_not_analysed(rendered):
    fixtures, out = rendered
    data, rows = fixtures["not_analysed"], out["not_analysed"]["indexRows"]
    assert [s["analysis_status"] for s in data["statements"]] == ["analysed", "unsupported", "invalid", "unreadable"]
    keys = [row[4] for row in rows]
    assert not keys[0].startswith(("Not valid", "Not analysed", "Could not be read"))
    assert keys[1] == "Not analysed (not supported): VALUES is a valid MySQL statement that this validator does not analyse"
    assert keys[2] == 'Not valid MySQL: a MySQL statement cannot begin with "SELEC"'
    assert keys[3].startswith("Could not be read: ")
    # Valid-but-unsupported and unreadable SQL is never called invalid; none is shown as low risk.
    assert "valid MySQL" not in keys[3] and "Not valid" not in keys[1]
    assert [row[2].split()[0] for row in rows[1:]] == ["CRITICAL"] * 3
    assert [row[1] for row in rows[1:3]] == ["VALUES", "UNKNOWN"]
    # Limitations name each statement that was not analysed, and why; never "read partly".
    limits = next(row for row in out["not_analysed"]["assessment"] if row.startswith("Limitations"))
    assert "Statement 2: not analysed: valid MySQL, but this statement type is not supported." in limits
    assert "Statement 3: not analysed: not valid MySQL." in limits
    for n in (2, 3, 4):
        assert f"Statement {n}: the SQL could only be read partly." not in limits


def test_a_single_statement_has_no_statements_table(rendered):
    _, out = rendered
    assert out["local_select_plan"]["indexRows"] == []


def test_overview_states_are_derived_only_from_page_state(rendered):
    _, out = rendered
    states = out["__states"]
    assert "No review yet" in states["empty"] and "No result" in states["empty"]
    assert "In progress" in states["running"] and "Pending" in states["running"]
    assert "Failed" in states["failed"] and "Validation failed: example" in states["failed"]
    assert "No result" in states["failed"]
    for state in states.values():
        assert "/ 100" not in state and "Priority findings" not in state


def test_overview_shows_the_response_and_links_findings_to_statements(rendered):
    fixtures, out = rendered
    for name, data in fixtures.items():
        view = out[name]
        overview = view["overview"]
        assert f"{data['overall_risk_level']} · {data['overall_score']} / 100" in overview, name
        assert f"Confidence{data['overall_confidence']}" in overview, name
        assert f"Statements{data['statement_count']}" in overview, name
        assert "not a safety rating" in overview, name
        not_analysed = sum(1 for s in data["statements"] if s["parse_error"])
        if not_analysed:
            # Incomplete SQL analysis is its own state, never "Complete" (with or without limitations).
            assert "Incomplete analysis" in overview, name
            assert "Complete" not in overview.replace("Incomplete", ""), name
            assert f"{not_analysed} of {data['statement_count']} statement" in overview, name
        else:
            assert "Incomplete analysis" not in overview, name
            assert ("Complete, with limitations" in overview) is (data["overall_confidence"] != "HIGH"), name
        assert view["cardIds"] == [f"statement-{n}" for n in range(1, data["statement_count"] + 1)], name
        assert set(view["findingLinks"]) <= set(f"#{card}" for card in view["cardIds"]), name
        # No safety claim and no plan claim beyond what the response reports.
        words = overview.lower().replace("not a safety rating", "").replace("does not mean the sql is safe", "")
        assert " safe" not in words, name
        if not any(s["evidence"]["plan"]["state"] == "collected" for s in data["statements"]):
            assert "Execution plan" not in overview or "1 collected" not in overview.split("Execution plan")[1][:12], name
    assert "Execution planNot collected: no database login and database." in out["static_select"]["overview"]
    assert "No review yet" not in out["static_select"]["overview"]


def test_dashboard_is_one_band_and_a_findings_table_not_tiles_and_cards(rendered):
    fixtures, out = rendered
    for name, data in fixtures.items():
        composition = out[name]["composition"]
        findings = sum(1 for s in data["statements"]
                       for f in s["factors"] + s["evidence_factors"] + s["metadata_factors"] if f["points"] > 0)
        assert composition["band"] == 4, name
        assert composition["tableRows"] == min(findings, 8), name
        assert composition["oldTilesOrCards"] == 0, name
    assert "Severity" in out["mixed_batch"]["overview"] and "DBA actions" in out["mixed_batch"]["overview"]
    # Metadata not requested (connector, no database selected) is "not collected", not "not applicable".
    assert "Not collected for 1 statement(s) (no database selected)." in out["remote_no_database"]["overview"]


def test_batch_confidence_is_the_weakest_statement(rendered):
    fixtures, out = rendered
    data = fixtures["mixed_confidence"]
    assert [s["confidence"] for s in data["statements"]] == ["HIGH", "LIMITED"]
    assert data["overall_confidence"] == "LIMITED"
    assert out["mixed_confidence"]["batchConfidence"] == "LIMITED"


def test_no_rendered_result_calls_sql_safe(rendered):
    _, out = rendered
    for name, view in out.items():
        if name == "__states":
            continue
        for statement in view["statements"]:
            words = statement["text"].lower().replace("unsafe", "")
            assert " safe" not in words and "is safe" not in words, name


# ----------------------------------------------------------------------------- browser lifecycle (no Chrome needed)

# Prints a complete document, then never exits, while a helper it started holds the
# same output open: what Chrome did on the macOS runner, plus the pipe trap.
HANGING_BROWSER = r"""
import pathlib, subprocess, sys, time
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(helper.pid))
sys.stdout.write('<html><body><pre id="render-out">{}</pre></body></html>\n')
sys.stdout.flush()
time.sleep(60)
"""


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def test_dump_dom_stops_a_browser_that_never_exits_once_the_document_is_complete(tmp_path):
    helper_pid_file = tmp_path / "helper.pid"
    started = time.monotonic()
    document, _stderr, code = dump_dom([sys.executable, "-c", HANGING_BROWSER, str(helper_pid_file)], tmp_path)
    assert time.monotonic() - started < 30
    assert code is None  # stopped, not exited
    assert '<pre id="render-out">{}</pre>' in document
    helper = int(helper_pid_file.read_text())
    if os.name == "nt":
        os.kill(helper, signal.SIGTERM)  # Windows has no process group to stop with the browser
    else:
        end = time.monotonic() + 10
        while not _gone(helper) and time.monotonic() < end:
            time.sleep(0.1)
        assert _gone(helper), "a helper in the browser's process group survived"


def test_dump_dom_returns_what_a_failing_browser_produced(tmp_path):
    document, stderr, code = dump_dom(
        [sys.executable, "-c", "import sys; sys.stderr.write('no display'); sys.exit(3)"], tmp_path)
    assert (document, stderr, code) == ("", "no display", 3)


def test_dump_dom_gives_up_at_the_deadline_without_a_document(tmp_path):
    started = time.monotonic()
    document, _stderr, code = dump_dom(
        [sys.executable, "-c", "import sys, time; sys.stdout.write('<html>'); sys.stdout.flush(); time.sleep(60)"],
        tmp_path, deadline=1)
    assert time.monotonic() - started < 30
    assert (document, code) == ("<html>", None)  # incomplete: the rendered fixture's assertion then fails


# ---------------------------------------------------------------- stop_browser on POSIX (macOS EPERM)


class _Browser:
    """A stand-in Popen: exits after `exits_after` signals (None: never)."""

    pid = 4242

    def __init__(self, exits_after):
        self.exits_after, self.signals, self.returncode = exits_after, [], None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("browser", timeout)
        return self.returncode


def _stop_with(monkeypatch, browser, errors):
    """Run stop_browser as on POSIX; killpg raises errors[i] (or signals) on its i-th call."""
    calls = iter(errors)

    def killpg(pgid, sig):
        assert pgid == browser.pid
        error = next(calls, None)
        if error is not None:
            raise error
        browser.signals.append(sig)
        if browser.exits_after is not None and len(browser.signals) >= browser.exits_after:
            browser.returncode = -sig

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "os", type("PosixOS", (), {"name": "posix", "killpg": staticmethod(killpg)}))
    monkeypatch.setattr(module, "signal", type("Signals", (), {"SIGTERM": 15, "SIGKILL": 9}))
    stop_browser(browser)


def test_stop_browser_signals_the_group_until_it_is_gone(monkeypatch):
    browser = _Browser(exits_after=1)
    _stop_with(monkeypatch, browser, [None, ProcessLookupError()])
    assert browser.signals == [15] and browser.returncode == -15


def test_stop_browser_kills_a_browser_that_ignores_sigterm(monkeypatch):
    browser = _Browser(exits_after=2)
    _stop_with(monkeypatch, browser, [])
    assert browser.signals == [15, 9] and browser.returncode == -9


def test_stop_browser_accepts_eperm_once_the_browser_has_exited(monkeypatch):
    # The observed macOS case: Chrome exited (returncode 0) and only unreaped helpers were left.
    browser = _Browser(exits_after=None)
    browser.returncode = 0
    _stop_with(monkeypatch, browser, [PermissionError(1, "Operation not permitted")])
    assert browser.signals == []


def test_stop_browser_reports_eperm_while_the_browser_still_runs(monkeypatch):
    browser = _Browser(exits_after=None)
    with pytest.raises(PermissionError):
        _stop_with(monkeypatch, browser, [PermissionError(1, "Operation not permitted")])


def test_stop_browser_reports_a_browser_that_never_exits(monkeypatch):
    browser = _Browser(exits_after=None)
    with pytest.raises(subprocess.TimeoutExpired):
        _stop_with(monkeypatch, browser, [])
    assert browser.signals == [15, 9]
