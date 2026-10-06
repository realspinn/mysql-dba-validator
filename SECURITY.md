# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for a suspected vulnerability.

Please include the version, from `/api/health` or the release file name, along
with steps to reproduce and the impact you expect. Never include real database
credentials, company hostnames or customer data in a report.

## Supported versions

Only the latest release receives fixes while the project is pre-1.0.

## Security model in brief

- The web app (`127.0.0.1:8420`) and the local connector (`127.0.0.1:8765`)
  bind to loopback only.
- Company MySQL credentials are supplied per request, used for that operation
  and not persisted. There is no credential store.
- The browser can use only connector targets that an operator approved in the
  connector console. It cannot supply an arbitrary remote host.
- Each company connection re-validates the approved DNS identity and requires
  TLS verified against a configured CA. Without a CA, company operations are
  refused.
- Browser sessions require a single-use pairing code shown only in the
  connector console. Sessions are held in memory and cannot manage targets.
- Evidence queries are fixed or bounded: `SELECT 1`, `EXPLAIN` and fixed
  metadata queries. There is no generic SQL endpoint, and the submitted SQL is
  never executed (never `EXPLAIN ANALYZE`).
- Local evidence uses only the MySQL login supplied with that validation, on
  one connection to the selected database that is closed when the validation
  ends. The login is not saved and there is no other credential source.
- MySQL account privileges are the authorization boundary; the validator's
  statement restrictions are the application safety boundary. Setting the
  evidence connection to read-only transactions is defense in depth only.
- On the local read-only evidence connection, a plan request is sent only when
  the exact outgoing statement is one plain `SELECT` (no `INTO`). `UPDATE`,
  `DELETE` and everything else get no plan request; there is no writable
  evidence connection and no switch to enable one.
- Each result reports its database evidence from what was actually collected
  (the per-statement `evidence` object). Missing or failed evidence never
  lowers a risk score and never changes confidence. A plan is reported only
  for a statement the validator parsed faithfully: a `SELECT … INTO` in any
  form, or a `SELECT` the parser could only read permissively, gets no plan
  request and is reported as not eligible, locally and through the connector.
- API responses, including request-validation errors, do not include submitted
  credential values.

## Known limitations

- End-to-end use against real company infrastructure has not been verified.
  That includes a hosted HTTPS frontend, company MySQL, VPN/LAN and company TLS.
- There is no user authentication. The tool is for a single user on their own
  machine, or a trusted self-hosted setup. It is not an internet-facing
  service.
- Local evidence runs with the privileges of the login you enter, so prefer
  the least-privileged account that can review the SQL. (v0.2.0 instead used a
  plain-text `.env` account for local evidence.)
- Local `UPDATE`/`DELETE` statements get no execution-plan evidence, because
  the evidence connection is read-only and MySQL refuses `EXPLAIN` of writes
  there. Static analysis and table metadata still apply.
- Release builds are not code-signed. Verify the published SHA256 checksum.
