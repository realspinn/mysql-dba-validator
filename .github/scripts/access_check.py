"""Compare who has direct (collaborator) access to this repository with the owner's allowlist.

The normal way to contribute is a fork and a pull request, which needs no access
to this repository. Direct access is an exception that the owner approves for a
named GitHub account in .github/access-policy.json. See .github/ACCESS.md.

Check (the default): read-only. Run every hour by
.github/workflows/access-check.yml with the job's own read-only token, and by
the maintainer by hand. It lists the repository's collaborators and fails, so
the maintainer is alerted, when:

  * an account other than the owner has access and is not in the allowlist;
  * an approved account has a different permission than the one approved;
  * the owner is not listed with admin access (the listing cannot be trusted);
  * the policy file is malformed, or any GitHub API call fails.

An approved account that is no longer a collaborator is reported, not failed.
Nothing is ever written: not to GitHub, not to the policy file.

GitHub has no workflow trigger for "invitation accepted" (the `member` event is
webhook-only), and the job's token cannot read pending invitations (that needs
Administration access). So an invitation is only checked once it is accepted,
when the account appears as a collaborator. --invitations lists pending
invitations as information; it needs the owner's own token and changes nothing.

--revoke LOGIN: run only by the owner, with the owner's own token, for one
account named on the command line. It removes that account's collaborator access
only if the account is not the owner, not in the allowlist, not an admin, and is
a collaborator right now; otherwise it removes nothing. Running it again for an
account already removed makes no change. Pending invitations are never touched.

Standard library only; the workflow runs it with the runner's own python3.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "access-policy.json"
POLICY_NAME = ".github/access-policy.json"
PAGE_SIZE = 100
MAX_PAGES = 50
# A personal-account repository has only one collaborator level, shown by the API as "write".
APPROVABLE_PERMISSIONS = {"write"}
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENTRY_KEYS = {"login", "permission", "approved_on", "reason"}


class AccessError(Exception):
    """Access could not be checked or changed safely; the run fails."""


# ----------------------------------------------------------------------------- GitHub API

class GitHubAPI:
    """Minimal REST client. Tests replace it with a fake that has the same methods."""

    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self._token = token
        self._api_url = api_url.rstrip("/")

    def _request(self, method: str, path: str, params: dict | None = None):
        url = self._api_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mdv-access-check",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return response.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            raise AccessError(f"GitHub API {method} {path} returned HTTP {exc.code}") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise AccessError(f"GitHub API {method} {path} failed: {type(exc).__name__}") from None

    def get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params)[1]

    def delete(self, path: str) -> int:
        return self._request("DELETE", path)[0]


def get_pages(api, path: str) -> list:
    items, page = [], 1
    while True:
        batch = api.get(path, {"per_page": PAGE_SIZE, "page": page})
        if not isinstance(batch, list):
            raise AccessError(f"GitHub API GET {path} did not return a list")
        items.extend(batch)
        if len(batch) < PAGE_SIZE:
            return items
        if page >= MAX_PAGES:
            raise AccessError(f"GitHub API GET {path} returned more than {MAX_PAGES} pages")
        page += 1


# ----------------------------------------------------------------------------- policy

def _no_duplicate_keys(pairs):
    # json keeps only the last of two equal keys, which could hide an entry from a reviewer.
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def load_policy(path: Path, repo: str) -> dict:
    """{"owner": login, "approved": {lowercase login: entry}} from the policy file, strictly checked."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise AccessError(f"cannot read {POLICY_NAME}: {type(exc).__name__}") from None
    if not isinstance(data, dict) or set(data) != {"owner", "approved_collaborators"}:
        raise AccessError(f"{POLICY_NAME} must have exactly the keys 'owner' and 'approved_collaborators'")
    owner = data["owner"]
    repo_owner = repo.split("/", 1)[0]
    if not isinstance(owner, str) or owner.lower() != repo_owner.lower():
        raise AccessError(f"{POLICY_NAME} owner {owner!r} is not the repository owner {repo_owner!r}")
    entries = data["approved_collaborators"]
    if not isinstance(entries, list):
        raise AccessError(f"{POLICY_NAME} approved_collaborators must be a list")
    approved = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise AccessError(f"each approved collaborator needs exactly the keys {sorted(ENTRY_KEYS)}")
        login = entry["login"]
        if not isinstance(login, str) or not LOGIN_RE.match(login):
            raise AccessError(f"approved collaborator {login!r} is not a valid GitHub login")
        if login.lower() == owner.lower():
            raise AccessError("the owner must not be listed as an approved collaborator")
        if login.lower() in approved:
            raise AccessError(f"approved collaborator {login!r} is listed twice")
        if not isinstance(entry["permission"], str) or entry["permission"] not in APPROVABLE_PERMISSIONS:
            raise AccessError(f"{login}: permission must be one of {sorted(APPROVABLE_PERMISSIONS)}")
        try:
            if not isinstance(entry["approved_on"], str) or not DATE_RE.match(entry["approved_on"]):
                raise ValueError
            date.fromisoformat(entry["approved_on"])
        except ValueError:
            raise AccessError(f"{login}: approved_on must be a YYYY-MM-DD date") from None
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise AccessError(f"{login}: reason must say what the access is for")
        approved[login.lower()] = entry
    return {"owner": owner, "approved": approved}


# ----------------------------------------------------------------------------- checks

def list_collaborators(api, repo: str) -> dict:
    """{lowercase login: (login, role_name)} for every collaborator, including the owner."""
    collaborators = {}
    for user in get_pages(api, f"/repos/{repo}/collaborators"):
        login = user.get("login") if isinstance(user, dict) else None
        role = user.get("role_name") if isinstance(user, dict) else None
        if not isinstance(login, str) or not isinstance(role, str):
            raise AccessError("a collaborator entry has no login or role")
        collaborators[login.lower()] = (login, role)
    return collaborators


def check(collaborators: dict, policy: dict) -> list:
    """[(is_error, message)] comparing live collaborators with the policy."""
    owner = policy["owner"].lower()
    findings = []
    if collaborators.get(owner, (None, None))[1] != "admin":
        findings.append((True, f"the owner {policy['owner']} is not listed with admin access, "
                               "so the collaborator list cannot be trusted"))
    for key, (login, role) in sorted(collaborators.items()):
        if key == owner:
            continue
        entry = policy["approved"].get(key)
        if entry is None:
            findings.append((True, f"{login} has {role} access but is not approved in {POLICY_NAME}"))
        elif role != entry["permission"]:
            findings.append((True, f"{login} has {role} access but was approved for {entry['permission']}"))
    for key, entry in sorted(policy["approved"].items()):
        if key not in collaborators:
            findings.append((False, f"{entry['login']} is approved but is not a collaborator "
                                    "(invitation not accepted, or access already removed)"))
    return findings


def invitation_notes(api, repo: str, policy: dict) -> list:
    """Information lines about pending invitations. Needs the owner's token; changes nothing."""
    notes = []
    for invitation in get_pages(api, f"/repos/{repo}/invitations"):
        invitee = invitation.get("invitee") if isinstance(invitation, dict) else None
        login = invitee.get("login") if isinstance(invitee, dict) else None
        if not isinstance(login, str):
            raise AccessError("a pending invitation has no invitee login")
        state = "approved" if login.lower() in policy["approved"] else "NOT approved"
        notes.append(f"pending invitation: {login} ({invitation.get('permissions')}), {state}; "
                     "checked only once accepted")
    return notes


def revoke(api, repo: str, policy: dict, login: str) -> str:
    """Remove one unapproved collaborator. Refuses rather than guesses; repeats are no-ops."""
    if not LOGIN_RE.match(login):
        raise AccessError(f"{login!r} is not a valid GitHub login")
    key = login.lower()
    if key == policy["owner"].lower():
        raise AccessError("refusing to remove the repository owner")
    if key in policy["approved"]:
        raise AccessError(f"{login} is approved in {POLICY_NAME}; remove the approval there first")
    collaborators = list_collaborators(api, repo)
    if key not in collaborators:
        return f"{login} is not a collaborator; nothing removed"
    name, role = collaborators[key]
    if role == "admin":
        raise AccessError(f"refusing to remove {name}: admin access is changed only in GitHub settings")
    status = api.delete(f"/repos/{repo}/collaborators/{urllib.parse.quote(name, safe='')}")
    if status != 204:
        raise AccessError(f"removing {name} returned HTTP {status}, expected 204")
    if key in list_collaborators(api, repo):
        raise AccessError(f"{name} is still a collaborator after the removal request")
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"removed {name} ({role} access) from {repo} at {when}; verified no longer a collaborator"


# ----------------------------------------------------------------------------- entry point

def main(argv=None, environ=os.environ, policy_path: Path = POLICY_PATH) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--invitations", action="store_true",
                        help="also list pending invitations (owner's token; read-only)")
    parser.add_argument("--revoke", metavar="LOGIN",
                        help="remove this one unapproved collaborator (owner's token)")
    args = parser.parse_args(argv)
    token = environ.get("GITHUB_TOKEN", "")
    repo = environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("::error::GITHUB_TOKEN and GITHUB_REPOSITORY must both be set; nothing was checked")
        return 1
    api = GitHubAPI(token, environ.get("GITHUB_API_URL", "https://api.github.com"))
    try:
        policy = load_policy(policy_path, repo)
        if args.revoke:
            print(revoke(api, repo, policy, args.revoke))
            return 0
        findings = check(list_collaborators(api, repo), policy)
        notes = invitation_notes(api, repo, policy) if args.invitations else []
    except AccessError as exc:
        print(f"::error::{exc}")
        return 1
    for is_error, message in findings:
        print(f"{'::error::' if is_error else '::notice::'}{message}")
    for note in notes:
        print(note)
    if any(is_error for is_error, _ in findings):
        print(f"Collaborator access does not match {POLICY_NAME}; see .github/ACCESS.md")
        return 1
    print(f"Collaborator access matches {POLICY_NAME}: owner {policy['owner']} (admin) and "
          f"{len(policy['approved'])} approved exception(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
