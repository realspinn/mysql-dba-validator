"""Copy review summary (web/src/features/review/copySummary.ts, CopySummary.tsx).

Verification level:
- the text itself and the clipboard helper are tested with Node (web/tests, `npm test`);
- here, source checks that tie the summary's allowed wording to what the backend can
  produce, and that the page code keeps its rules (nothing sent, nothing stored);
- one test drives the built page in headless Chrome against real backend responses
  (skipped where Chrome is not installed; the Windows and macOS runners have it).
"""

from __future__ import annotations

import ast
import html
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from tests.frontend_page import built_page, source
from tests.test_frontend_evidence_rendering import dump_dom, find_chrome, serve

BACKEND = Path(__file__).resolve().parent.parent / "backend"
SUMMARY = source("features/review/copySummary.ts")
BUTTON = source("features/review/CopySummary.tsx")
RESULTS = source("features/review/Results.tsx")
REVIEW_VIEW = source("features/review/ReviewView.tsx")
client = TestClient(app, base_url="http://127.0.0.1:8420")


def _backend_trees():
    for path in sorted(BACKEND.glob("*.py")):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"))


def _skeleton(node: ast.expr) -> str | None:
    """A string constant as is; an f-string with each {value} as {}."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in node.values)
    return None


def _factor_labels() -> set[str]:
    labels = set()
    for _, tree in _backend_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            first = node.args[0] if node.args else None
            keyword = next((k.value for k in node.keywords if k.arg == "label"), None)
            if name == "ScoreFactor" and first is not None:
                labels.add(_skeleton(first))
            elif keyword is not None and _skeleton(keyword) is not None:
                labels.add(_skeleton(keyword))
    return labels


# The statement type or key name is a placeholder in these labels; the summary's
# PATTERN_FACTORS match the same shapes.
TEMPLATES = {
    "Base risk for {}", "{} is classified as CRITICAL", "Explains a {} statement",
    "{} changes access privileges", "Unique key {} limits the match to at most one row",
}


def test_every_backend_factor_label_is_known_wording_to_the_summary():
    labels = _factor_labels()
    assert None not in labels, "a factor label that is not a literal needs reviewing for the summary"
    start = SUMMARY.index("export const FIXED_FACTORS")
    fixed = set(re.findall(r"^\t'([^']+)',$", SUMMARY[start:SUMMARY.index("];", start)], re.M))
    assert len(fixed) >= 14, fixed
    constants = {label for label in labels if "{}" not in label}
    assert constants <= fixed, sorted(constants - fixed)
    templates = {label for label in labels if "{}" in label}
    assert templates <= TEMPLATES, sorted(templates - TEMPLATES)
    # Each template has its matching pattern in the summary.
    for template in templates:
        for part in template.split("{}"):
            assert part.strip() in SUMMARY, part


def test_backend_checklist_lines_are_fixed_text_or_a_statement_type():
    """The summary copies checklist lines as the backend wrote them. That is safe only
    while none of them carries submitted text, so any new kind of line must be reviewed."""
    allowed_expressions = {
        "first_check",  # a fixed pair per analysis status (NOT_ANALYSED_WORDING)
        "checklist", "list(dict.fromkeys(checklist))", "_deduplicate(checklist)",
        "_deduplicate(['Do not run EXPLAIN ANALYZE on a statement you would not run on its own.'] + report.checklist)",
        "CANDIDATE_ROWS_CHECK",
    }
    allowed_fstrings = {
        "Review the explained {} as if it will be run on its own.",  # a statement type keyword
    }
    found = []
    for name, tree in _backend_trees():
        constants = {t.targets[0].id for t in ast.walk(tree) if isinstance(t, ast.Assign) and len(t.targets) == 1
                     and isinstance(t.targets[0], ast.Name) and isinstance(t.value, ast.Constant)}
        for node in ast.walk(tree):
            items = []
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("append", "extend")
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "checklist"):
                items = node.args[0].elts if isinstance(node.args[0], (ast.List, ast.Tuple)) else [node.args[0]]
            elif isinstance(node, ast.keyword) and node.arg == "checklist":
                items = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            for item in items:
                if isinstance(item, ast.Constant) or (isinstance(item, ast.Name) and item.id in constants):
                    continue
                if isinstance(item, ast.BinOp) and isinstance(item.left, ast.Constant) and isinstance(item.right, ast.Constant):
                    continue
                if isinstance(item, ast.JoinedStr) and _skeleton(item) in allowed_fstrings:
                    continue
                if ast.unparse(item) in allowed_expressions:
                    continue
                found.append(f"{name}:{item.lineno}: {ast.unparse(item)}")
    assert not found, found


def test_not_analysed_wording_is_fixed_text():
    from backend.analyzer import NOT_ANALYSED_WORDING

    assert all(isinstance(title, str) and isinstance(check, str) for title, check in NOT_ANALYSED_WORDING.values())


def test_the_summary_leaves_out_the_fields_that_can_repeat_the_sql_or_name_the_database():
    code = re.sub(r"^\s*//.*$", "", SUMMARY, flags=re.M)
    for field in ("reasons", r"\.sql\b", "where_predicate", "columns_in_where", "optimization", r"\.table\b", r"\.index\b",
                  r"\.column\b", r"\.key\b", r"\.extra", "possible_keys", r"\.name\b", r"\.password", r"\.username", r"\.host\b",
                  r"\.\w*token", "localStorage", "sessionStorage"):
        assert not re.search(field, code), field
    # parse_error (it can quote the SQL) is only ever asked whether it is there, never copied.
    uses = re.findall(r"parse_error(.{0,3})", code)
    assert uses and all(use.startswith((")", " ?")) for use in uses), uses


def test_the_new_code_sends_and_stores_nothing_and_never_reads_the_clipboard():
    for text in (SUMMARY, BUTTON):
        assert not re.search(r"fetch\(|XMLHttpRequest|sendBeacon|WebSocket|localStorage|sessionStorage|indexedDB|shared/storage|readText|execCommand|console\.",
                             text)
    assert BUTTON.count("navigator.clipboard") == 1
    # Copying starts from the button's click only: no effect calls it.
    assert BUTTON.count("copy()") == 1 and "onClick={() => void copy()}" in BUTTON
    assert not re.search(r"useEffect\([^;]*copy", BUTTON)


def test_stale_detection_is_unchanged_and_gates_the_button():
    # The page's own rule (ReviewView): the SQL or the connection context differs.
    assert ("review.reviewed.sql !== review.sql || review.reviewed.context !== reviewContext(state)") in REVIEW_VIEW
    assert "stale={stale}" in REVIEW_VIEW
    assert "summary={copySummaryFor(run, reviewed, stale)}" in RESULTS
    assert "reviewed === null || stale) return null" in SUMMARY
    assert "disabled={summary === null}" in BUTTON


# ------------------------------------------------------------------ the built page

HARNESS = """
<script>
window.__FIXTURES = %s;
const clip = { texts: [], mode: 'ok' };
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
  writeText: text => clip.mode === 'ok' ? (clip.texts.push(text), Promise.resolve())
    : Promise.reject(new DOMException('denied', 'NotAllowedError')) } });
window.__hold = null;
window.fetch = (url) => {
  const json = (status, body) => Promise.resolve(new Response(JSON.stringify(body),
    { status, headers: { 'Content-Type': 'application/json' } }));
  const path = String(url);
  if (path === '/api/validate') {
    if (window.__hold) return window.__hold.then(() => json(400, { message: 'Validation failed: example' }));
    return json(200, window.__next);
  }
  if (path === '/api/update-status') return json(200, { state: 'up_to_date' });
  if (path === '/api/heartbeat') return Promise.resolve(new Response(null, { status: 204 }));
  return Promise.reject(new TypeError('blocked in the harness: ' + path));
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const until = async (test, label) => { for (let i = 0; i < 400; i++) { const v = test(); if (v) return v; await sleep(25); }
  throw new Error('timed out: ' + label); };
const $ = s => document.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const typeSql = text => { const ta = $('#sqlInput');
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(ta, text);
  ta.dispatchEvent(new Event('input', { bubbles: true })); };
const review = async (data) => { window.__next = data;
  $$('button').find(b => b.textContent === 'Review SQL').click();
  await until(() => $('.verdict') && !$('.result-loading'), 'review'); await sleep(40); };
const copyButton = () => $$('button').find(b => b.textContent === 'Copy review summary') || null;
const note = () => $('.summary-note') ? $('.summary-note').textContent : null;

async function run() {
  await until(() => $('.sidebar'), 'app');
  const OUT = {};
  $$('.nav-item').find(b => b.textContent === 'SQL review').click();
  await until(() => $('#sqlInput'), 'review view');
  typeSql('SELECT 1');
  await review(window.__FIXTURES.batch);
  const button = copyButton();
  OUT.initial = { present: !!button, disabled: button.disabled, type: button.type, note: note(),
    live: $('.summary-note').getAttribute('role'), clipboardBefore: clip.texts.length };
  button.click();
  await until(() => note() === 'Summary copied.', 'copied');
  OUT.copied = { texts: [...clip.texts], note: note(), sqlCopyButtonStillThere: $$('button').some(b => b.textContent === 'Copy' || b.textContent === 'Copied') };
  clip.mode = 'fail';
  copyButton().click();
  await until(() => note() && note().includes('did not allow'), 'failed note');
  OUT.refused = { note: note(), texts: clip.texts.length };
  clip.mode = 'ok';
  typeSql('SELECT 2');
  await sleep(60);
  OUT.stale = { disabled: copyButton().disabled, note: note(), staleNotice: $('.stale') ? $('.stale').textContent : null };
  copyButton().click();
  await sleep(40);
  OUT.stale.textsAfterClick = clip.texts.length;
  await review(window.__FIXTURES.single);
  OUT.again = { disabled: copyButton().disabled, stale: !!$('.stale') };
  let release;
  window.__hold = new Promise(r => { release = r; });
  typeSql('SELECT 3');
  $$('button').find(b => b.textContent === 'Review SQL').click();
  await sleep(60);
  OUT.running = { button: !!copyButton(), loading: !!$('.result-loading') };
  release();
  await until(() => $('.result-state'), 'failed state');
  OUT.failed = { button: !!copyButton(), message: $('.result-state').textContent };
  const out = document.createElement('pre');
  out.id = 'render-out';
  out.textContent = JSON.stringify(OUT);
  document.body.append(out);
}
run().catch(error => { const out = document.createElement('pre'); out.id = 'render-error';
  out.textContent = String(error && error.stack || error); document.body.append(out); });
</script>
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("Chrome is not installed")
    batch_sql = "SELECT 1 FROM orders_private WHERE id = 1; DELETE FROM orders_private; DROP TABLE audit_log;"
    fixtures = {"batch": client.post("/api/validate", json={"sql": batch_sql}).json(),
                "single": client.post("/api/validate", json={"sql": "SELECT id FROM customers_secret WHERE id = 7"}).json()}
    page_html = built_page("index.html")
    harness = page_html.replace("</head>", HARNESS % json.dumps(fixtures).replace("</", "<\\/") + "</head>", 1)
    work = tmp_path_factory.mktemp("copy-summary")
    server = serve(harness)
    try:
        document, stderr, code = dump_dom(
            [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={work / 'profile'}", "--virtual-time-budget=600000", "--dump-dom",
             f"http://127.0.0.1:{server.server_port}/harness.html"], work)
    finally:
        server.shutdown()
    failure = re.search(r'<pre id="render-error">(.*?)</pre>', document, re.S)
    assert not failure, html.unescape(failure.group(1))
    match = re.search(r'<pre id="render-out">(.*?)</pre>', document, re.S)
    assert match, f"page did not render (exit {code}): {stderr[-500:]}"
    return batch_sql, json.loads(html.unescape(match.group(1)))


def test_the_button_copies_the_summary_only_when_clicked(page):
    sql, out = page
    assert out["initial"] == {"present": True, "disabled": False, "type": "button", "note": "", "live": "status", "clipboardBefore": 0}
    assert len(out["copied"]["texts"]) == 1
    text = out["copied"]["texts"][0]
    assert text.startswith("MySQL DBA Validator: SQL review summary\n")
    assert "Overall risk: CRITICAL" in text and "not approval to execute the SQL" in text
    assert out["copied"]["note"] == "Summary copied."


def test_the_copied_text_holds_no_submitted_sql_or_names(page):
    sql, out = page
    text = out["copied"]["texts"][0]
    for word in ("orders_private", "audit_log", "DELETE FROM", "DROP TABLE", sql):
        assert word not in text, word


def test_a_refused_copy_says_so_and_does_not_claim_success(page):
    _, out = page
    assert "did not allow copying" in out["refused"]["note"] and "copied" not in out["refused"]["note"].lower()
    assert out["refused"]["texts"] == 1  # still only the first, successful copy


def test_a_stale_result_cannot_be_copied(page):
    _, out = page
    assert out["stale"]["disabled"] is True
    assert out["stale"]["note"] == "" and "Review again" in out["stale"]["staleNotice"]
    assert out["stale"]["textsAfterClick"] == 1
    assert out["again"] == {"disabled": False, "stale": False}


def test_no_copy_button_while_a_review_runs_or_after_it_fails(page):
    _, out = page
    assert out["running"] == {"button": False, "loading": True}
    assert out["failed"]["button"] is False and "did not complete" in out["failed"]["message"]


def test_the_sql_copy_button_is_still_there(page):
    _, out = page
    assert out["copied"]["sqlCopyButtonStillThere"] is True
