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


# What may feed a checklist without being a literal: variables and calls that only pass
# on lines built elsewhere from the same literals (each source is itself scanned), and the
# one line that names a statement type.
PASS_THROUGH = {
    "first_check", "checklist", "list(report.checklist)", "list(dict.fromkeys(checklist))", "_deduplicate(checklist)",
    "_deduplicate(['Do not run EXPLAIN ANALYZE on a statement you would not run on its own.'] + report.checklist)",
    "field(default_factory=list)",
}
TYPED_LINES = {"Review the explained {} as if it will be run on its own."}


def _checklist_sources(source_text: str, known: dict[str, str]) -> tuple[set[str], list[str]]:
    """(literal lines, expressions that are neither literals nor allowed) that can reach a checklist.

    Looks at checklist.append/extend, any assignment to a name `checklist`, and checklist=...
    keywords. A name is a literal only if a module-level constant (here or in `known`) says so:
    a value stored in a variable first is not safe merely because of that."""
    tree = ast.parse(source_text)
    constants = dict(known)
    constants.update({t.targets[0].id: t.value.value for t in tree.body if isinstance(t, ast.Assign) and len(t.targets) == 1
                      and isinstance(t.targets[0], ast.Name) and isinstance(t.value, ast.Constant) and isinstance(t.value.value, str)})
    literals: set[str] = set()
    dynamic: list[str] = []

    def take(node: ast.expr) -> None:
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for item in node.elts:
                take(item)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
        elif isinstance(node, ast.Name) and node.id in constants:
            literals.add(constants[node.id])
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and all(
                isinstance(side, ast.Constant) for side in (node.left, node.right)):
            literals.add(node.left.value + node.right.value)
        elif isinstance(node, ast.JoinedStr) and _skeleton(node) in TYPED_LINES:
            pass
        elif ast.unparse(node) not in PASS_THROUGH:
            dynamic.append(f"line {node.lineno}: {ast.unparse(node)}")

    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("append", "extend")
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "checklist"):
            take(node.args[0])
        elif isinstance(node, ast.keyword) and node.arg == "checklist":
            take(node.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "checklist" for t in targets):
                take(node.value)
    return literals, dynamic


def _fixed_checks() -> set[str]:
    start = SUMMARY.index("export const FIXED_CHECKS")
    body = SUMMARY[start:SUMMARY.index("];", start)]
    return {json.loads('"' + m.group(2) + '"') if m.group(1) == '"' else m.group(2)
            for m in re.finditer(r"^\t(['\"])(.*)\1,$", body, re.M)}


def _backend_constants() -> dict[str, str]:
    values = {}
    for _, tree in _backend_trees():
        for t in tree.body:
            if (isinstance(t, ast.Assign) and len(t.targets) == 1 and isinstance(t.targets[0], ast.Name)
                    and isinstance(t.value, (ast.Constant, ast.BinOp))):
                try:
                    value = ast.literal_eval(t.value) if isinstance(t.value, ast.Constant) else eval(compile(ast.Expression(t.value), "c", "eval"), {})
                except Exception:
                    continue
                if isinstance(value, str):
                    values[t.targets[0].id] = value
    return values


def test_every_backend_checklist_line_is_known_wording_to_the_summary():
    """The summary copies only the lines listed in FIXED_CHECKS. This keeps that list complete
    and the backend's checklists free of lines built from submitted text."""
    from backend.parser import NOT_ANALYSED_WORDING

    known = _backend_constants()
    literals, dynamic = set(), []
    for name, _ in _backend_trees():
        found, other = _checklist_sources((BACKEND / name).read_text(encoding="utf-8"), known)
        literals |= found
        dynamic += [f"{name} {line}" for line in other]
    literals |= {check for _, check in NOT_ANALYSED_WORDING.values()}
    assert not dynamic, dynamic
    fixed = _fixed_checks()
    assert len(fixed) >= 44
    assert literals <= fixed, sorted(literals - fixed)


def test_the_checklist_scan_catches_a_sensitive_value_stored_in_a_variable_first():
    table = "orders_private"
    for code in (f'checklist = []\nline = f"Back up {{table}} first"\nchecklist.append(line)\n',
                 'checklist: list[str] = [f"Check {table}"]\n',
                 'checklist = [f"Check {table}"]\n',
                 'checklist = list(report.checklist) + [f"Check {table}"]\n',
                 'checklist.extend([facts.where_predicate_summary])\n',
                 'text = "Back up " + table\nchecklist.append(text)\n'):
        _, dynamic = _checklist_sources(code, {})
        assert dynamic, code
    literals, dynamic = _checklist_sources('FIXED = "Do it."\nchecklist = [FIXED, "Also."]\n', {})
    assert literals == {"Do it.", "Also."} and not dynamic


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
const clip = { texts: [], mode: 'ok', held: [] };
// mode 'hold': the write stays pending until the harness settles it (clip.held).
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
  writeText: text => {
    if (clip.mode === 'hold') return new Promise((resolve, reject) => clip.held.push({ text, resolve, reject }));
    return clip.mode === 'ok' ? (clip.texts.push(text), Promise.resolve())
      : Promise.reject(new DOMException('denied', 'NotAllowedError')); } } });
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
  // A clipboard write that is still pending when the result changes must not report on it.
  const settle = (index, ok = true) => { const w = clip.held[index]; if (ok) { clip.texts.push(w.text); w.resolve(); } else w.reject(new DOMException('denied', 'NotAllowedError')); };
  const forget = () => { clip.held = []; };
  clip.mode = 'hold';
  copyButton().click();
  await sleep(30);
  OUT.heldCurrent = { before: note() };
  settle(0);
  await until(() => note() === 'Summary copied.', 'held write, result still current');
  OUT.heldCurrent.after = note();
  forget();
  await sleep(30);
  // 1. the SQL changes while the write is pending
  copyButton().click();
  await sleep(30);
  typeSql('SELECT 5');
  await sleep(60);
  settle(0);
  await sleep(120);
  OUT.heldThenEdited = { note: note(), disabled: copyButton().disabled, stale: !!$('.stale') };
  forget();
  // 2. a newer review replaces the result while a write for the older one is pending
  await review(window.__FIXTURES.batch);
  copyButton().click();
  await sleep(30);
  typeSql('SELECT 6');
  await sleep(40);
  await review(window.__FIXTURES.single);
  settle(0);
  await sleep(120);
  OUT.heldThenReplaced = { note: note(), disabled: copyButton().disabled };
  forget();
  // 3. a refused write for a result that is no longer current shows no error either
  copyButton().click();
  await sleep(30);
  typeSql('SELECT 7');
  await sleep(60);
  settle(0, false);
  await sleep(120);
  OUT.refusedAfterEdit = { note: note() };
  forget();
  // 4. the result view goes away while the write is pending
  await review(window.__FIXTURES.batch);
  copyButton().click();
  await sleep(30);
  $$('.nav-item').find(b => b.textContent === 'Dashboard').click();
  await sleep(60);
  settle(0);
  await sleep(120);
  $$('.nav-item').find(b => b.textContent === 'SQL review').click();
  await until(() => copyButton(), 'review view again');
  OUT.heldThenLeft = { note: note(), disabled: copyButton().disabled };
  clip.mode = 'ok';
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


def test_a_completed_write_for_the_current_result_shows_its_confirmation(page):
    _, out = page
    assert out["heldCurrent"] == {"before": "", "after": "Summary copied."}


def test_a_write_that_finishes_after_the_sql_changed_shows_nothing(page):
    _, out = page
    assert out["heldThenEdited"] == {"note": "", "disabled": True, "stale": True}


def test_a_write_that_finishes_after_a_newer_review_replaced_the_result_shows_nothing(page):
    _, out = page
    assert out["heldThenReplaced"] == {"note": "", "disabled": False}


def test_a_refused_write_for_an_older_result_shows_no_error(page):
    _, out = page
    assert out["refusedAfterEdit"] == {"note": ""}


def test_a_write_that_finishes_after_the_result_view_was_left_shows_nothing_when_it_returns(page):
    _, out = page
    assert out["heldThenLeft"] == {"note": "", "disabled": False}
