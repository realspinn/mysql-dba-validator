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
        trust\                   CA bundle files added from the page
        logs\backend.log         web app log, replaced on each start
MySQL usernames and passwords typed into the page are used for that request
only. They are not written to disk, the target registry, the logs or browser
storage.

Update notice: once per start, the web app asks GitHub
(api.github.com) for the latest official release, and the page shows a
dismissible notice with a link to its release notes when it is newer. GitHub
sees your IP address and the installed version; no SQL, login or connection
details are sent. To turn it off, add the line MDV_UPDATE_CHECK=off to a file
named .env in the folder above (or set it as an environment variable).

Updating: choose Update in the notice. The app downloads the new release ZIP
into the updates folder inside the folder above and verifies it (size, the
SHA-256 digest published by GitHub, archive contents). It does not install or
run anything. When it says Ready to install: choose Show in folder, close
MySQL DBA Validator, right-click the ZIP > Extract All, and open
MySQL-DBA-Validator.exe in the new folder. Your saved settings stay in the
folder above. Keep the old folder until the new version works, then delete it.

Updating from v0.8.0 or earlier: those versions only link to the release
page, so install this version by hand once. Download the windows-x64 ZIP from
the release page, check it against its .sha256 file, and extract it to a new
folder. Close the old version first: close its console window, or close its
browser page and wait up to 15 minutes for it to stop (if it is still running,
this version says so instead of starting). Then open MySQL-DBA-Validator.exe in
the new folder. Saved targets, the .env file and logs in the folder above, and
the theme in your browser, are kept; logins were never saved.


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
static analysis and table metadata still apply. Each result says which
evidence it has: whether table metadata was collected (and whether each table
was found), whether an execution plan was collected, and if not, why.

If evidence is missing, the result says why (for example, no login entered,
login rejected, database not found, or missing privileges).

Earlier versions (v0.2.0) read a local evidence account from a .env file in
the data folder. That file is no longer used for validation evidence.


OPTIONAL: COMPANY TARGETS THROUGH THE CONNECTOR
-----------------------------------------------
Company MySQL connections always use TLS, verified against the certificates
that sign the company servers. Start the Console version from a terminal in
this folder:

    & ".\MySQL-DBA-Validator Console.exe" --tls-ca C:\path\to\company-ca.pem

--tls-ca is the connector's CA bundle, used by targets registered in the
console window. Without it the connector still starts, but those targets are
refused.

Paste the pairing code from the console window into the page. Then add a
target in one of two ways; both need the console window:
- From the page: under "Add a remote connection", enter a name, host name and
  port, and choose the trusted certificates: the connector CA bundle
  (--tls-ca), a CA bundle file, or the Windows certificate store. The console
  window then shows the request (host, port, resolved addresses, certificates)
  and a one-time approval code. Check it, then enter the code in the page
  within 5 minutes.
- In the console window: "register <id> <host> <port> <name>", then
  "approve <id>". These targets use the --tls-ca bundle.
Then in the page: choose the target, enter your own MySQL login, discover
databases, validate. The page can only ask for a target; nothing is approved
without the code shown in the console window.

A CA bundle must be PEM text ("-----BEGIN CERTIFICATE-----" blocks, CA
certificates only, no private key, up to 256 KiB). A binary (DER) certificate
is refused, and renaming it to .pem does not convert it. A .cer file is often
DER (PowerShell's Export-Certificate writes .cer files as DER), so export the
CA certificates as PEM, then open the file in Notepad and check that it holds
only such blocks.

Other Console options: --registry <file>, --session-ttl <seconds>,
--allow-origin <origin>, --no-browser. Run it with --help for details.

Note: company-infrastructure end-to-end use (real company MySQL, VPN, company
TLS) has not yet been verified for this release.


TROUBLESHOOTING
---------------
- "port(s) ... already in use": the validator is already running (close its
  page and wait, or close the Console window), or another program uses port
  8420 or 8765.
- "... is still running, so this copy cannot start yet": another version of
  the validator (usually the one you updated from) is still running. Close it
  as the message says, then open this copy again.
- The page does not open: browse to http://127.0.0.1:8420 yourself.
- "cannot load target registry": the registry file named in the message could
  not be loaded. The program refuses to start rather than change it. After an
  upgrade, one cause is a saved target whose host name the current, stricter
  rules refuse (a non-ASCII name, a last part that does not start with a
  letter such as db.1corp, or more than 253 characters); the message then ends
  in invalid_target:targets[N], the entry's position counting from 0. Keep a
  copy of the file and the trust folder next to it, remove only that entry,
  start again and add the target again with a name the current rules accept
  (it needs approval again): the xn-- form of a non-ASCII name, or, when the
  old name has no valid form, another DNS name or alias that resolves to the
  same server, if your DNS provides one. Otherwise the file is damaged: move
  or fix it.
- The web app did not start: read %LOCALAPPDATA%\MySQLDBAValidator\logs\backend.log.
