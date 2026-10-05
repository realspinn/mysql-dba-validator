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
  metadata queries. There is no generic SQL endpoint.

## Known limitations

- End-to-end use against real company infrastructure has not been verified.
  That includes a hosted HTTPS frontend, company MySQL, VPN/LAN and company TLS.
- There is no user authentication. The tool is for a single user on their own
  machine, or a trusted self-hosted setup. It is not an internet-facing
  service.
- Local evidence uses `MYSQL_USER`/`MYSQL_PASSWORD` from a `.env` file that
  the user creates and stores in plain text. Use a read-only account.
- Release builds are not code-signed. Verify the published SHA256 checksum.
