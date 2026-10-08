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
from tests.test_local_evidence_credentials import DECOY, FakeMySQL, mysql_error, post

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
    "collected": "Database evidence was collected.",
    "not_applicable": "Connected to the database; no database evidence applies to this statement.",
}
READ_ONLY_PLAN = "Execution plan not collected because the evidence connection is read-only."
NOT_ELIGIBLE_PLAN = ("No execution plan was requested: this statement is not eligible for planning "
                     "(for example SELECT ... INTO).")
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
    }


HARNESS = """
<script>
const FIXTURES = %s;
const OUT = {};
for (const [name, data] of Object.entries(FIXTURES)) {
  renderResults(data);
  const root = document.getElementById('results');
  const cells = root.querySelectorAll('.summary-cell');
  OUT[name] = {
    mode: cells[2].querySelector('strong').textContent,
    summary: root.querySelector('.evidence-summary').textContent,
    statements: [...root.querySelectorAll('article.statement')].map(card => ({
      headline: card.querySelector('.evidence-headline').textContent,
      evidence: [...card.querySelectorAll('.layer.evidence p')].map(p => p.textContent),
      metadata: [...card.querySelectorAll('.layer.metadata p')].map(p => p.textContent),
      planGrids: card.querySelectorAll('.layer.evidence .metric-grid').length,
      confidence: card.querySelector('.metric.confidence b').textContent,
      why: [...card.querySelector('.layer.final ul.plain-list').children].map(li => li.textContent),
      text: card.textContent,
    })),
  };
}
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
            assert view["headline"] == HEADLINES[state["overall"]], name
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
            assert view["planGrids"] == (1 if state["plan"]["state"] == "collected" else 0), name
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
    assert view["evidence"][:4] == [HEADLINES["collected"], "Database metadata was collected.",
                                    "users: table found", "Execution-plan evidence was collected."]
    assert view["confidence"] == "HIGH"
    assert out["local_select_plan"]["mode"] == "Database evidence"


@pytest.mark.parametrize("name", ["local_update", "local_delete"])
def test_local_dml_shows_metadata_and_the_intentional_plan_skip(rendered, name):
    _, out = rendered
    view = out[name]["statements"][0]
    assert view["headline"] == HEADLINES["collected"]
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
    assert "Execution-plan evidence was collected." in view["evidence"]
    assert "Partial evidence" in out["metadata_failed"]["summary"]


@pytest.mark.parametrize("name", ["select_into", "trailing_select_into"])
def test_select_into_is_explained_as_not_eligible(rendered, name):
    fixtures, out = rendered
    assert fixtures[name]["statements"][0]["evidence"]["plan"]["state"] == "not_eligible"
    view = out[name]["statements"][0]
    assert NOT_ELIGIBLE_PLAN in view["evidence"]
    assert not any("this statement type" in line for line in view["evidence"])
    assert view["planGrids"] == 0
    assert out[name]["mode"] == "Static + database metadata"


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
    assert view["headline"] == HEADLINES["collected"]
    assert "Execution-plan evidence was collected." in view["evidence"]
    assert view["confidence"] == "LIMITED"


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
