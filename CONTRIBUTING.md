# Contributing to MySQL DBA Validator

MySQL DBA Validator helps a DBA review MySQL SQL before it is run. It
classifies statements, scores risk deterministically and, when asked, collects
limited read-only evidence from a database. It supports a DBA's judgement; it
does not guarantee that SQL is safe.

The project is small, but people use its output to decide whether to run SQL
against real databases. A change to parsing, scoring, evidence collection,
credential handling or connector behaviour can make the tool report something
it does not actually know. Contributions are welcome. Before you change
anything that touches a database, read this guide and [SECURITY.md](SECURITY.md).

## Where things are documented

| Document | Covers |
| --- | --- |
| [README.md](README.md) | What the tool does, how to install and use it, summaries of architecture, security and verification |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components and module map, request flows, what is sent to a database, scoring rules, evidence model |
| [SECURITY.md](SECURITY.md) | Authoritative security model, known limitations, private vulnerability reporting |
| [VERIFICATION.md](VERIFICATION.md) | What has and has not been tested, at which level, and release verification |
| [connector/README.md](connector/README.md) | Local connector: configuration, operator console, sessions, credentials |
| CONTRIBUTING.md (this file) | Development setup, architecture, review areas, tests, pull requests |
| [RELEASING.md](RELEASING.md) | Maintainer-only build and release procedure |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | How we treat each other |

Where this guide and SECURITY.md differ, SECURITY.md is correct and this
guide needs fixing.

## Development setup

The project is developed and tested by hand on Windows with Python 3.14. The
CI workflow runs the full test suite on GitHub-hosted Windows and macOS (Apple
Silicon) runners for pull requests and pushes to `main`; the release workflows
run it again, and build the apps, for version tags or when started by hand. A
passing macOS run is not a manual acceptance test on a real Mac; that has not
been done yet (see VERIFICATION.md). The commands below are PowerShell, run
from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

On macOS or Linux (bash or zsh), activate with `source .venv/bin/activate`
instead; in Windows `cmd.exe`, use `.venv\Scripts\activate.bat`.

`requirements.txt` includes the test dependencies. Run the web app and, if you
are working on the remote/company workflow, the connector in a second terminal:

```powershell
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8420
python -m connector --allow-origin http://localhost:8420
```

Open `http://localhost:8420`. The frontend is a single static page,
`frontend/index.html`, served by the backend. It has no build step: edit it and
reload. The connector's options (`--tls-ca`, `--registry`, operator commands)
are in [connector/README.md](connector/README.md).

You do not need a MySQL server to develop or run the tests. For local evidence
by hand, use a MySQL server on your own machine and enter a login in the page.
No configuration file is needed.

## Architecture

[ARCHITECTURE.md](ARCHITECTURE.md) describes the components, the module map,
the local and remote request flows, exactly what may be sent to a database,
the scoring rules and the evidence model. Read it before changing parsing,
scoring, evidence collection or the connector.

The points contributors most often need:

- **Static analysis** (`backend/parser.py`, `analyzer.py`, `risk_engine.py`)
  needs no database. The parser produces facts and does not decide anything
  about safety.
- **Local evidence** (`backend/db.py`, `db_evidence.py`, `metadata.py`) uses
  one read-only connection per validation, and a plan request is sent only
  when the exact outgoing text is `EXPLAIN` of one plain `SELECT`.
- **Evidence state** (`backend/evidence_state.py`) is the source of truth for
  what a result says about evidence. `analysis_mode`, `evidence_summary` and
  the evidence sentences in reasons are derived from it, and it never changes
  scores or confidence. A new kind of evidence gets its own state there, with
  its "collected", "deliberately not collected" and "failed" cases kept apart.
- **Remote evidence** goes only through the local connector, to
  operator-approved targets. It is scored by the same function as local
  evidence, but under the connector's own connection rules.

There is no endpoint that runs SQL supplied by the user, and the submitted SQL
is never executed. The public `/api/evidence` route deliberately returns
`evidence_queries_disabled`. Evidence queries are built by the application
from a short, fixed list. That is a design decision, not a missing feature.

## What to work on

### Current open issues

These issues are open and unassigned (checked 2026-10-27). Neither is a
beginner task. Comment on the issue before you start.

- [#16](https://github.com/realspinn/mysql-dba-validator/issues/16): an
  offline regression corpus. One new test file that pins today's risk,
  confidence and scope results. No change under `backend/`.
- [#17](https://github.com/realspinn/mysql-dba-validator/issues/17): a
  hands-on acceptance pass of the packaged macOS release on a real Apple
  Silicon Mac. No code change; post the results on the issue.

### Good first contributions

- Documentation fixes, especially where docs or docstrings no longer match
  the code. A comment-only fix is fine; changing the code is not a first
  contribution.
- Test cases for SQL the parser already handles, in `tests/test_parser.py`
  and `tests/test_risk_engine.py`, that pin down current behaviour. If the
  current behaviour looks wrong, open an issue instead of writing a test that
  locks it in.
- Regression tests for a bug you found, even without a fix.
- Accessibility and usability fixes in `frontend/index.html`, such as labels,
  keyboard focus, contrast and wording, as long as they do not change what is
  sent, where it is sent or what is stored.
- Readable messages for error codes the page shows raw.

### Intermediate contributions

- Parser coverage for MySQL syntax that currently fails to parse, with tests.
  The parser also decides which statements get `EXPLAIN` evidence and
  produces the text that is explained. So a change to how a statement is
  classified or regenerated falls under the next section.
- New static findings or checklist items. If a finding changes a score, it
  also falls under the next section.
- Larger UI improvements that keep the existing request flow.
- Test helpers and fakes, in the style of `tests/local_evidence.py`, that let
  more behaviour be tested without a real server.

### Changes that need maintainer, DBA or security review

Open an issue to discuss the design before you write code for any of these:

- What SQL is sent to a database: `EXPLAIN` construction, metadata queries,
  `SHOW DATABASES` discovery, or any new query.
- Any transformation or rewriting of submitted SQL, including changes to
  statement classification that decide what is explained.
- Local database evidence: what is collected, when it is skipped, and how its
  absence is reported.
- Connection behaviour: the local evidence connection, read-only session
  setup, timeouts, connection lifecycle and target classification in
  `backend/main.py`.
- Anything that handles credentials, in any component.
- The connector (`connector/`): origins, pairing, sessions and scopes, the
  target registry, DNS and TLS checks, request size limits and the operator
  console.
- Risk scores and their adjustments: point values, thresholds, and which
  facts count as evidence.
- Confidence, analysis mode and evidence statuses: what the result claims to
  know.
- Frontend code that builds requests, picks the web app or the connector, or
  uses browser storage.
- Packaging and release: `release/`, the PyInstaller spec, the artifact audit,
  the smoke test, `.github/workflows/`, and dependency versions, because
  `release/requirements-build.txt` must be regenerated when
  `requirements.txt` changes.

### Changes that will not be accepted

Some changes look small but remove a safety property:

- A flag, setting or environment variable that turns safety checks off.
- A way to execute submitted SQL, `EXPLAIN ANALYZE`, or a generic query
  endpoint.
- A writable local evidence connection, or plan requests for anything other
  than a verified plain `SELECT` on it.
- Reaching company databases without the connector, or a fallback to the web
  app when the connector is unavailable.
- Accepting a host, port or TLS setting for company targets from the browser,
  or approving targets over HTTP.
- Storing credentials: on disk, in the registry, in browser storage, in a
  vault or credential manager, in logs, URLs, error messages or responses.
- Reporting evidence that was not collected, or treating missing evidence as
  a sign that SQL is safe.

**Looks small, needs design first.** "Rewrite `UPDATE`/`DELETE` as `SELECT`
so local `EXPLAIN` can run" sounds like a small parser task, but it is not a
beginner task. The
rewritten statement is a different statement. Multi-table updates, `ORDER BY
... LIMIT`, subqueries on the target table and locking behaviour may not carry
over, so its plan is not evidence about the SQL under review. Whether and how
to approximate a write's plan is an evidence-model decision. Raise it in an
issue.

## Testing

Each behaviour change needs:

1. tests for the new behaviour;
2. a regression test for any bug it fixes, where practical;
3. a passing full test suite;
4. security or DBA review, if it falls into one of the review areas above.

```powershell
python -m pytest tests/test_parser.py -q   # focused run while you work
python -m pytest -q                        # full suite before opening a PR
pip check
```

- New SQL handling needs tests that show what is classified and what is
  flagged.
- Score changes need tests that assert the exact score or adjustment, so a
  later change can't shift it silently.
- Evidence changes need both sides tested: what is collected when allowed,
  and what is refused, not sent, or reported unavailable when not allowed.
- Credential and connector changes need explicit tests that values do not
  leak into responses, errors, logs or persisted files. The existing tests in
  `tests/test_validation_error_redaction.py`,
  `tests/test_connector_credentials.py` and
  `tests/test_security_hardening.py` show the pattern.
- The suite needs no MySQL server and no real credentials. Tests use fake
  connections and mocked DNS lookups. New tests should do the same.
- `tests_mysql/live_mysql_checks.py` is the exception: Local Mode evidence
  against a real MySQL 8.4 server. It runs only in CI (the MySQL 8.4
  integration job in `ci.yml`, in a throwaway Docker service container on
  Ubuntu) and fails anywhere else. Do not run it against your own server.
- Never weaken a safety check, or edit an existing test, only to make a test
  pass. If a requirement has really changed, replace the old test with one for
  the new contract and explain why in the PR.

Be precise about what was verified. A unit or `TestClient` test, a test
against a running process, and a run against real MySQL or company
infrastructure are different levels. [VERIFICATION.md](VERIFICATION.md)
tracks them. Do not claim a level you did not reach.

If you change `release/`, also run the build and smoke test described in
[RELEASING.md](RELEASING.md).

## Workflow and pull requests

Changes reach `main` only through pull requests:

1. Fork the repository on GitHub. Every contribution comes from a fork; you
   do not need, and will not be given, access to this repository
   ([.github/ACCESS.md](.github/ACCESS.md)). In your fork, branch
   from `main` and give the branch a short descriptive name, for example
   `test/parser-join-cases` or `docs/connector-options`.
2. Make the change and its tests on that branch.
3. Run the full test suite and `pip check` locally. The CI workflow
   (`.github/workflows/ci.yml`) runs them again on Windows, macOS and Linux
   for every pull request to `main`, plus the MySQL 8.4 integration tests on
   Linux. `.github/workflows/package-check.yml` also builds and smoke-tests the
   Windows and unsigned macOS packages. Check both results on the pull request. Until you
   have had a pull request merged here, a maintainer has to approve each CI
   run before it starts.
4. Push the branch to your fork and open a pull request from it against
   `main`, leaving "Allow edits by maintainers" ticked. Answer the questions
   below. To update the pull request, push more commits to the same branch.
5. A maintainer reviews it, requests DBA or security review if needed, and
   decides whether and when to merge it.

Do not push directly to `main`. Merging is the maintainers' decision.
Releases are made only by maintainers, following
[RELEASING.md](RELEASING.md). Contributors do not tag versions, publish
releases or change `APP_VERSION`.

Keep each pull request focused on one change. Recent commits use short
prefixes such as `feat:`, `test:` and `docs:`. In the description, answer:

- **What changed, and why?**
- **Tests:** what was added or updated, and what level of verification it
  reaches.
- **Behaviour change:** could an existing result change, such as a score,
  finding, confidence or evidence status?
- **Does it affect any of these?** Mark yes or no and explain each yes:
  SQL parsing · risk scoring · confidence/evidence · SQL sent to a database ·
  database connections · credentials · connector/remote targets ·
  packaging/release.

A pull request that touches a review area may need more discussion before it
is merged.

## Security boundaries for contributors

[SECURITY.md](SECURITY.md) is the authoritative model. When you write code,
the rules that matter most are:

- Credentials are per request and never persisted, logged, put in URLs or
  returned in responses, including validation errors.
- Company database access goes only through the connector, only to
  operator-approved targets, and only with verified TLS. Without a CA, company
  operations fail closed.
- No endpoint runs user-supplied SQL. Evidence queries are fixed or bounded
  and built by the application.
- Local evidence uses a read-only connection, and only a verified plain
  `SELECT` gets a plan request.
- MySQL privileges are the authorization boundary. The application's
  restrictions are a safety layer on top, not a replacement.
- Results never claim more evidence than was collected.

To report a vulnerability, follow [SECURITY.md](SECURITY.md). Do not open a
public issue.

## What not to commit

`.gitignore` already excludes most of these. Do not work around it.

- `.env` files. Only `.env.example` is tracked, and it holds no secrets.
- Credentials, keys and certificates (`*.pem`, `*.key`, `*.pfx`, `*.p12`).
- Connector target registries (`connector-targets*.json`) and logs (`*.log`).
- Build output (`build/`, `dist/`, `*.sha256`). Release binaries are
  published as GitHub Release assets only.
- Virtual environments, editor settings (`.vscode/`, `.idea/`), `scratch/`
  and local AI-assistant context files (`.claude/`, `CLAUDE.md`,
  `AGENTS.md`).
- Real hostnames, company names, customer SQL, schema names or data in tests,
  fixtures, screenshots or issues. Use made-up values, as the existing tests
  do.

## Questions and good first issues

Use [GitHub Issues](https://github.com/realspinn/mysql-dba-validator/issues)
for bug reports, proposals and other specific, actionable work. For
open-ended DBA feedback and anonymised examples of risky SQL, use
[Discussions → General](https://github.com/realspinn/mysql-dba-validator/discussions/categories/general).
Both are public: never post company SQL, credentials, hostnames or other
details of a real database. Security vulnerabilities are the exception and go
through [SECURITY.md](SECURITY.md). If you are unsure whether a change falls into a review area, ask
before you write the code. Mention the area in the issue and describe what
you plan to change.

A good first issue for this project is:

- **bounded:** one file or one behaviour;
- **deterministic:** the result is the same on every run;
- **testable:** a test can show it works;
- **low risk:** it cannot change what is sent to a database or what a result
  claims;
- **away from security boundaries:** no credentials, connector,
  evidence-collection or scoring changes.

Test cases for existing parser behaviour, documentation corrections and
readable error messages meet all five. A new evidence query, a scoring tweak
or a SQL rewrite does not, however small the diff.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
