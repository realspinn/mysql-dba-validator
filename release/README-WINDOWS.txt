MySQL DBA Validator - Windows portable release
==============================================

Deterministic risk review for MySQL ticket SQL. Paste SQL into the page and get
the statement classification, a risk score, confidence, findings and a DBA
checklist. Python is not required; everything needed is in this folder.


RUN
---
1. Extract the whole zip to any folder you can write to or read from, for
   example C:\Tools\ or your Desktop. Keep the folder structure intact.
2. Double-click MySQL-DBA-Validator.exe.
3. Your browser opens http://127.0.0.1:8420. No other window opens.
4. To stop, close the page. The app stops on its own about 15 minutes after
   the last page of it was closed. Double-clicking again while it is running
   just opens the page.

For approved company/remote MySQL targets, use "MySQL-DBA-Validator Console.exe"
instead (see COMPANY TARGETS below). It opens a console window, which is the
connector's terminal; close that window or press Ctrl+C in it to stop.

Windows SmartScreen may warn about an unrecognised app because this build is
not code-signed. Verify the SHA256 checksum published with the release before
choosing "More info" > "Run anyway".

Verify the download (PowerShell):
    Get-FileHash .\MySQL-DBA-Validator-v<version>-windows-x64.zip -Algorithm SHA256
and compare with the .sha256 file from the release page.


WHAT RUNS
---------
- MySQL-DBA-Validator.exe: the web page and validation API on
  http://127.0.0.1:8420 (this machine only). No console window and no
  connector: static validation and local MySQL evidence.
- MySQL-DBA-Validator Console.exe: the same web page and API, plus the local
  connector on 127.0.0.1:8765 (this machine only), used only for approved
  remote/company MySQL targets. Its console window is the connector's
  terminal: it shows the pairing code and accepts operator commands. Type
  "help" there for the commands.

Static SQL validation needs no database and no configuration.


YOUR DATA
---------
The program folder is never written to. Per-user data lives in:

    %LOCALAPPDATA%\MySQLDBAValidator\
        connector-targets.json   approved company targets (no credentials)
        logs\backend.log         web app log, replaced on each start
MySQL usernames and passwords typed into the page are used for that request
only. They are not written to disk, the target registry, the logs or browser
storage.


LOCAL MYSQL EVIDENCE
--------------------
For EXPLAIN/metadata evidence against MySQL on this machine (127.0.0.1:3306),
enter your MySQL login in the page, choose "Discover databases", select a
database and validate. Evidence uses that login, on one connection to the
selected database that is closed when the validation ends. No configuration
file is needed and the login is not saved.

Your MySQL account's privileges decide what evidence is possible: EXPLAIN
needs the same privileges as the statement it explains. The validator never
runs the SQL you submit; it only runs EXPLAIN of a plain SELECT and fixed
metadata queries, on a read-only connection. UPDATE and DELETE get no
execution plan (MySQL refuses EXPLAIN of writes on a read-only connection);
static analysis and table metadata still apply.

If evidence is missing, the result says why (for example, no login entered,
login rejected, database not found, or missing privileges).

Earlier versions (v0.2.0) read a local evidence account from a .env file in
the data folder. That file is no longer used for validation evidence.


OPTIONAL: COMPANY TARGETS THROUGH THE CONNECTOR
-----------------------------------------------
Company MySQL targets require the CA certificate bundle that signs the
company servers. Start the Console version from a terminal in this folder:

    & ".\MySQL-DBA-Validator Console.exe" --tls-ca C:\path\to\company-ca.pem

Without a CA bundle the connector still starts, but every company operation is
refused. Then, in the console window: "register <id> <host> <port> <name>",
"approve <id>". In the page: paste the pairing code, choose the target, enter
your own MySQL login, discover databases, validate.

Other Console options: --registry <file>, --session-ttl <seconds>,
--allow-origin <origin>, --no-browser. Run it with --help for details.

Note: company-infrastructure end-to-end use (real company MySQL, VPN, company
TLS) has not yet been verified for this release.


TROUBLESHOOTING
---------------
- "port(s) ... already in use": the validator is already running (close its
  page and wait, or close the Console window), or another program uses port
  8420 or 8765.
- The page does not open: browse to http://127.0.0.1:8420 yourself.
- "cannot load target registry": the registry file is damaged. The program
  refuses to start rather than overwrite it. Move or fix the file named in the
  message.
- The web app did not start: read %LOCALAPPDATA%\MySQLDBAValidator\logs\backend.log.
