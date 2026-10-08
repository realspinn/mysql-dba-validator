MySQL DBA Validator - macOS (Apple Silicon)
===========================================

Deterministic risk review for MySQL ticket SQL. Paste SQL into the page and get
the statement classification, a risk score, confidence, findings and a DBA
checklist. Python is not required; everything needed is in the app.

This build is for Macs with Apple Silicon (M-series). It does not run on Intel
Macs. The minimum supported macOS version has not yet been independently
verified (the app declares macOS 11).


RUN
---
1. Drag "MySQL DBA Validator" to the Applications folder.
2. Open "MySQL DBA Validator" from Applications.
3. Your browser opens http://127.0.0.1:8420. The app has no window and no Dock
   icon; the page is its user interface.
4. To stop, close the page. The app stops on its own about 15 minutes after the
   last page of it was closed.

If the app is already running, browse to http://127.0.0.1:8420.

Signing: check the release notes of the build you downloaded. A build from the
project's release page says there whether it is signed with a Developer ID and
notarized by Apple. Test builds from the project's CI are unsigned: macOS will
not open them normally, and they are not meant for general use.

Verify a download against the SHA256 checksum published with it (Terminal):

    shasum -a 256 <downloaded file>

and compare with the .sha256 file published next to it.


WHAT RUNS
---------
- MySQL DBA Validator (the app): the web page and validation API on
  http://127.0.0.1:8420 (this machine only). No connector: static validation
  and local MySQL evidence.
- MySQL-DBA-Validator Console, inside the app: the same web page and API, plus
  the local connector on 127.0.0.1:8765 (this machine only), used only for
  approved remote/company MySQL targets. Run it from Terminal (see COMPANY
  TARGETS below); Terminal is the connector's operator console: it shows the
  pairing code and accepts operator commands. Type "help" there. Press Ctrl+C
  or close the Terminal window to stop it.

Static SQL validation needs no database and no configuration.


YOUR DATA
---------
The app is never written to. Per-user data lives in:

    ~/.local/state/MySQLDBAValidator/
        connector-targets.json   approved company targets (no credentials)
        logs/backend.log         web app log, replaced on each start

(or $XDG_STATE_HOME/MySQLDBAValidator/ if XDG_STATE_HOME is set). MySQL
usernames and passwords typed into the page are used for that request only.
They are not written to disk, the target registry, the logs or browser storage.


LOCAL MYSQL EVIDENCE
--------------------
For EXPLAIN/metadata evidence against MySQL on this Mac (127.0.0.1:3306),
enter your MySQL login in the page, choose "Discover databases", select a
database and validate. Evidence uses that login, on one connection to the
selected database that is closed when the validation ends. The login is not
saved.

Your MySQL account's privileges decide what evidence is possible. The validator
never runs the SQL you submit; it only runs EXPLAIN of a plain SELECT and fixed
metadata queries, on a read-only connection. UPDATE and DELETE get no
execution plan (MySQL refuses EXPLAIN of writes on a read-only connection);
static analysis and table metadata still apply. Each result says which
evidence it has, and if evidence is missing, why.


OPTIONAL: COMPANY TARGETS THROUGH THE CONNECTOR
-----------------------------------------------
Company MySQL targets require the CA certificate bundle that signs the company
servers. In Terminal:

    "/Applications/MySQL DBA Validator.app/Contents/MacOS/MySQL-DBA-Validator Console" --tls-ca /path/to/company-ca.pem

Without a CA bundle the connector still starts, but every company operation is
refused. Then, in that Terminal window: "register <id> <host> <port> <name>",
"approve <id>". In the page: paste the pairing code, choose the target, enter
your own MySQL login, discover databases, validate. The page can never
register or approve targets; only this console can.

Other options: --registry <file>, --session-ttl <seconds>,
--allow-origin <origin>, --no-browser. Run it with --help for details.

Note: company-infrastructure end-to-end use (real company MySQL, VPN, company
TLS) has not yet been verified for this release.


TROUBLESHOOTING
---------------
- "port ... already in use": the validator is already running (close its page
  and wait, or stop the Console in its Terminal window), or another program
  uses port 8420 or 8765.
- The page does not open: browse to http://127.0.0.1:8420 yourself.
- "cannot load target registry": the registry file is damaged. The program
  refuses to start rather than overwrite it. Move or fix the file named in the
  message.
- The web app did not start: read ~/.local/state/MySQLDBAValidator/logs/backend.log.
