# Repository access

This repository is owned by the personal account `realspinn`. The steps below
are for the owner.

## Policy

- **Everyone contributes from a fork.** Contributors fork the repository, push
  to their fork and open a pull request against `main` (see
  [CONTRIBUTING.md](../CONTRIBUTING.md#workflow-and-pull-requests)). That needs
  no access to this repository. No one is invited to contribute code.
- **Only the owner has direct access.** The owner alone controls `main`,
  settings, collaborators, secrets, workflows, releases and tags.
  [access-policy.json](access-policy.json) lists no collaborators.
- **Why not direct access:** a personal-account repository has one collaborator
  level, shown as `write`; there is no read-only or triage role. Rulesets keep
  `main` and `v*` tags owner-only even for collaborators, but write access
  still lets a collaborator disable or re-enable any workflow (including the
  access check), delete workflow runs, create or overwrite Actions secrets and
  variables, push other branches and non-`v*` tags, and create and edit
  releases. No setting or ruleset limits these on a personal-account
  repository; only having no collaborators does.

## Reviewing pull requests from forks

- A fork pull request runs CI with a read-only token and no secrets. Workflows
  that use secrets or write access (`ai-review.yml`, `pr-summary.yml`) run from
  `main` and never check out or run the pull request's code.
- GitHub holds the workflow runs of anyone who has never had a pull request
  merged here until the owner clicks **Approve and run**. Read the diff first,
  especially any change under `.github/`, then approve.
- The contributor keeps updating the pull request by pushing to their fork.
  With "Allow edits by maintainers" ticked, the owner can push to it too.
- The owner's review and both CI checks are required before the owner merges.

## What is checked

[access-check.yml](workflows/access-check.yml) runs every hour on GitHub's
machines. It lists the collaborators with the job's own read-only token and
fails if anyone other than the owner is not in the allowlist, or has a different
permission than approved, or if anything cannot be read. A failed scheduled run
sends GitHub's failed-workflow notification to the account that last changed the
cron line in that file.

It detects; it does not prevent or remove anything. It is not enforcement:
anyone given write access could disable it. Removal is a manual step (below).

GitHub has no workflow trigger for an accepted invitation, and the job's token
cannot read pending invitations. So an invitation is checked once it is
accepted, within about an hour. Sending an invitation never adds anyone to the
allowlist.

## Approve an exception

Only if the owner decides someone needs direct access for a reason other than
contributing code, accepting the write-access limits above:

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
3. If not, remove it. Any open pull request from their fork stays open and
   keeps its commits; they keep updating it from the fork. Remove it in Settings → Collaborators, or from an up-to-date `main`
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
  disable a workflow (with no collaborators, only the owner can). Check
  **Actions → Access check**; enable it if needed.
- **No email:** check that Settings → Notifications → Actions sends failed
  workflow notifications.

No credential is stored for this check. It uses only the job's own token, which
GitHub issues per run and expires when the job ends, so there is nothing to
rotate. The `--revoke` command uses your own CLI login for that one run.
