# MySQL DBA Validator

**How risky is this SQL, and how sure are we about that?**

MySQL DBA Validator helps DBAs review MySQL SQL from tickets before it is run. Paste up to 16 statements to get deterministic risk scores, confidence, findings and a review checklist. Optionally, collect limited read-only evidence from a database.

It supports DBA judgement; it does **not** guarantee that SQL is safe.

**Current release: [v0.8.0](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.8.0)** · Pre-1.0 · Portable Windows x64 and unsigned macOS Apple Silicon builds.

![Validation results for a batch of four statements: overall risk CRITICAL, score 100 of 100, static analysis, batch confidence LIMITED; the top finding (statement 2, a DELETE with no WHERE clause), execution-plan, filter, limitation and unknown rows; and a table listing each statement with its risk, confidence and key finding](docs/images/validator-batch-review.png)

*Example results from made-up SQL, using static analysis without a database connection.*

## What it does

- **Risk:** classifies and scores each statement using explicit rules. A batch takes the highest statement score; scores are not added together.
- **Confidence:** reported separately as `HIGH`, `LIMITED` or `LOW`. It describes how well the available evidence supports the assessment, not whether the SQL is safe.
- **Findings:** explains score reasons, known write scope, unknowns and DBA checks.
- **Database evidence (optional):** collects limited evidence such as an eligible plain-`SELECT` execution plan and table metadata. Results distinguish collected, unavailable and failed evidence.
- **Optimization advice:** flags selected SQL patterns using fixed advisory rules. It does not rewrite SQL, change risk scores or measure performance.
- **No submitted SQL execution:** the validator never executes the SQL being reviewed. Risk decisions are deterministic; no AI or LLM makes them.

## Important limitations

- Risk and confidence are assessments, not guarantees. Review the SQL yourself before running it.
- SQL the parser cannot fully read is reported as **not analysed**, not treated as low risk. Some valid MySQL syntax is unsupported; see the [evidence contract](docs/v2-database-evidence-contract.md).
- A `SELECT` can still have effects, for example through locking clauses or functions. The validator applies restrictions, but cannot establish what an arbitrary stored function does.
- Optimization advice identifies patterns in SQL text. It cannot see the data, indexes or column types, and does not prove that a suggested change is equivalent or faster.
- **Live validation is limited.** The v0.8.0 adviser rules and `FROM DUAL` handling have not been checked against a real MySQL server. The Host check is covered by automated tests. Company infrastructure, VPN and company TLS are not verified. See [VERIFICATION.md](VERIFICATION.md).
- The Windows build is not code-signed. The macOS build is ad hoc signed, not notarized, and has not been tested on a real Mac.
- The app is designed for one user on one machine; it has no user authentication.

## Download and run

Get the ZIP and its matching `.sha256` file from the [v0.8.0 release page](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.8.0). Verify the checksum before running the application.

**Windows x64**

1. Download `MySQL-DBA-Validator-v0.8.0-windows-x64.zip` and its checksum file.
2. In PowerShell, verify the ZIP:
   ```powershell
   Get-FileHash .\\MySQL-DBA-Validator-v0.8.0-windows-x64.zip -Algorithm SHA256
   ```
   Compare the result with the SHA-256 value in the release.
3. Extract the ZIP and run `MySQL-DBA-Validator.exe`. Your browser opens `http://127.0.0.1:8420`. No installer or Python is needed.

For approved company targets, run `MySQL-DBA-Validator Console.exe` instead; it provides the local connector's operator console. See [connector/README.md](connector/README.md).

**macOS (Apple Silicon only)**

Download `MySQL-DBA-Validator-v0.8.0-macos-arm64-unsigned.zip`, verify it with its checksum file, then follow the opening instructions in the [release notes](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.8.0). The app is not notarized.

**Update notice:** the app can check GitHub for a newer release at startup and show a dismissible notice. It does not download or install updates. GitHub receives your IP address and the installed version; no SQL, login or connection details are sent. Set `MDV_UPDATE_CHECK=off` to disable the check. Details: [SECURITY.md](SECURITY.md#security-model-in-brief).

## Quick start

1. Open the app and paste the SQL from the ticket.
2. Choose **Validate SQL**.
3. Review the risk and confidence, then inspect each statement's findings, evidence and checklist.
4. Verify the change independently before running it.

Without a database login, analysis is based on SQL text only. Optional database evidence uses read-only connections. Company databases are accessed through the local connector and operator-approved targets; see [connector/README.md](connector/README.md).

## Documentation

| Document | What it covers |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Components, request flows, scoring and evidence model |
| [Evidence contract](docs/v2-database-evidence-contract.md) | Result fields, evidence rules and known analysis limitations |
| [Security policy](SECURITY.md) | Security boundaries and private vulnerability reporting |
| [Verification](VERIFICATION.md) | What has and has not been tested |
| [Local connector](connector/README.md) | Company-target setup and operator console |
| [Contributing](CONTRIBUTING.md) | Development setup, tests and pull requests |
| [Releasing](RELEASING.md) | Maintainer release procedure |
| [GitHub Releases](https://github.com/realspinn/mysql-dba-validator/releases) | Downloads, release notes and checksums |

## Run from source

Requires Python 3.14. From the repository root:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
uvicorn backend.main:app --host 127.0.0.1 --port 8420
```

Open `http://127.0.0.1:8420`. Run the test suite with `python -m pytest -q` and check dependencies with `pip check`. More detail is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Help and contributions

Report bugs and request features in [GitHub Issues](https://github.com/realspinn/mysql-dba-validator/issues). Use made-up SQL; never post credentials, company hostnames or customer data. Report security vulnerabilities privately using the process in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
