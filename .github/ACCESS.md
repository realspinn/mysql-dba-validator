# Repository access

This repository is owned by the personal account `realspinn`. The steps below
are for the owner.

## Policy

- **Default:** contributors fork the repository and open a pull request. That
  needs no access to this repository, and no one is invited just because they
  want to contribute.
- **Exception:** direct (collaborator) access only for a GitHub account the
  owner names in [access-policy.json](access-policy.json), with the permission,
  the date and what the access is for.
- A personal-account repository has one collaborator level, shown as `write`.
  There is no read-only or triage role, so an exception is always write access.
  Repository rulesets still let only the owner merge to `main` and create,
  move or delete `v*` tags, so no collaborator can change, delete or disable
  a workflow file on `main`. Write access still lets a collaborator push other
  branches (including workflow changes that run there), push tags not matching
  `v*`, create and edit releases, disable or re-enable any workflow (including
  the access check), delete workflow runs, and create or overwrite repository
  Actions secrets and variables. No repository setting or ruleset limits these
  on a personal-account repository; only removing direct access does.

## What is checked

[access-check.yml](workflows/access-check.yml) runs every hour on GitHub's
machines. It lists the collaborators with the job's own read-only token and
fails if anyone other than the owner is not in the allowlist, or has a different
permission than approved, or if anything cannot be read. A failed scheduled run
sends GitHub's failed-workflow notification to the account that last changed the
cron line in that file.

It detects; it does not remove anyone. Removal is a manual step (below).

GitHub has no workflow trigger for an accepted invitation, and the job's token
cannot read pending invitations. So an invitation is checked once it is
accepted, within about an hour. Sending an invitation never adds anyone to the
allowlist.

## Approve an exception

1. In a pull request, add an entry to `approved_collaborators`:

   ```json
   {"login": "their-login", "permission": "write", "approved_on": "2026-10-28",
    "reason": "what they need direct access for"}
   ```

   Use the exact login from Settings → Collaborators, not a display name.
2. Merge it (only the owner can). CODEOWNERS makes the owner's review required
   on every change to this file.
3. Invite the account in Settings → Collaborators. The next hourly run, or
   **Actions → Access check → Run workflow** on `main`, should pass.

## Withdraw an exception

1. Remove the entry from `access-policy.json` in a pull request and merge it.
2. Remove the access: Settings → Collaborators → **Remove**, or the command
   under "Respond to an alert".

## Respond to an alert

1. Open the failed run. Each `::error::` line names an account and why it
   failed.
2. If the access was intended, approve it as above.
3. If not, remove it in Settings → Collaborators, or from an up-to-date `main`
   checkout with your own GitHub CLI login:

   ```bash
   GITHUB_TOKEN="$(gh auth token)" GITHUB_REPOSITORY=realspinn/mysql-dba-validator \
     python .github/scripts/access_check.py --revoke THEIR-LOGIN
   ```

   ```powershell
   $env:GITHUB_TOKEN = gh auth token; $env:GITHUB_REPOSITORY = "realspinn/mysql-dba-validator"
   python .github/scripts/access_check.py --revoke THEIR-LOGIN
   Remove-Item Env:GITHUB_TOKEN
   ```

   It removes only that one account, and only if it is not the owner, not
   approved, not an admin and is a collaborator now; it then reads the list
   again to confirm. Running it twice changes nothing. It never cancels
   invitations; do that in Settings → Collaborators.
4. To see pending invitations with the same login, run the script with
   `--invitations` instead of `--revoke`. It changes nothing.
5. GitHub's security log (Settings → Security log on your account) records who
   was added and removed, and by whom.

## If the check itself fails or stops

- **An API error, or "cannot be trusted":** re-run it from the Actions tab. If
  it keeps failing, check [githubstatus.com](https://www.githubstatus.com) and
  compare with Settings → Collaborators by hand.
- **No runs for a while:** GitHub disables scheduled workflows in a public
  repository after 60 days without activity, and anyone with write access can
  disable a workflow. Check **Actions → Access check**; enable it if needed.
- **No email:** check that Settings → Notifications → Actions sends failed
  workflow notifications.

No credential is stored for this check. It uses only the job's own token, which
GitHub issues per run and expires when the job ends, so there is nothing to
rotate. The `--revoke` command uses your own CLI login for that one run.
