"""Tests for the access check workflow (.github/workflows/access-check.yml) and its script.

The GitHub API is replaced by FakeGitHub, so nothing here contacts GitHub or
changes anyone's access.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "access-check.yml"
SCRIPT = ROOT / ".github" / "scripts" / "access_check.py"
POLICY = ROOT / ".github" / "access-policy.json"

# .github is not an importable package name, so load the script by path.
_spec = importlib.util.spec_from_file_location("access_check", SCRIPT)
access_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(access_check)

REPO = "realspinn/mysql-dba-validator"
ENV = {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": REPO}
OWNER = {"login": "realspinn", "role_name": "admin"}


def _user(login, role="write"):
    return {"login": login, "role_name": role}


def _entry(login, permission="write"):
    return {"login": login, "permission": permission, "approved_on": "2026-10-28",
            "reason": "maintains the parser tests"}


class FakeGitHub:
    """In-memory stand-in for access_check.GitHubAPI."""

    def __init__(self, collaborators=None, invitations=()):
        self.collaborators = list(collaborators if collaborators is not None else [OWNER])
        self.invitations = list(invitations)
        self.fail = set()       # paths whose GET raises an API error
        self.delete_status = 204
        self.ignore_delete = False
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        if path in self.fail:
            raise access_check.AccessError(f"GitHub API GET {path} returned HTTP 500")
        source = {f"/repos/{REPO}/collaborators": self.collaborators,
                  f"/repos/{REPO}/invitations": self.invitations}[path]
        page, size = params["page"], params["per_page"]
        return source[(page - 1) * size:page * size]

    def delete(self, path):
        self.calls.append(("DELETE", path))
        login = path.rsplit("/", 1)[1]
        if self.delete_status == 204 and not self.ignore_delete:
            self.collaborators = [u for u in self.collaborators if u["login"] != login]
        return self.delete_status

    def deletes(self):
        return [path for method, path in self.calls if method == "DELETE"]


@pytest.fixture
def policy_file(tmp_path):
    def write(approved=(), owner="realspinn"):
        path = tmp_path / "access-policy.json"
        path.write_text(json.dumps({"owner": owner, "approved_collaborators": list(approved)}),
                        encoding="utf-8")
        return path
    return write


def _main(monkeypatch, fake, policy_path, *argv, env=ENV):
    monkeypatch.setattr(access_check, "GitHubAPI", lambda token, url: fake)
    return access_check.main(list(argv), environ=env, policy_path=policy_path)


# ----------------------------------------------------------------------------- check

def test_only_the_owner_passes(monkeypatch, policy_file, capsys):
    assert _main(monkeypatch, FakeGitHub(), policy_file()) == 0
    assert "matches .github/access-policy.json" in capsys.readouterr().out


def test_an_approved_collaborator_stays_approved(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER, _user("Helper1")])
    assert _main(monkeypatch, fake, policy_file([_entry("helper1")])) == 0
    assert "1 approved exception" in capsys.readouterr().out


def test_an_unknown_collaborator_fails_the_run(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER, _user("Stranger")])
    assert _main(monkeypatch, fake, policy_file()) == 1
    out = capsys.readouterr().out
    assert "::error::Stranger has write access but is not approved" in out
    assert "matches" not in out


def test_an_unknown_collaborator_beyond_the_first_page_is_found(monkeypatch, policy_file):
    approved = [_entry(f"user{i}") for i in range(120)]
    fake = FakeGitHub([OWNER] + [_user(f"user{i}") for i in range(120)] + [_user("late")])
    assert _main(monkeypatch, fake, policy_file(approved)) == 1


def test_a_collaborator_passes_only_once_the_policy_names_them(monkeypatch, policy_file):
    fake = FakeGitHub([OWNER, _user("G3TECH1")])
    assert _main(monkeypatch, fake, policy_file()) == 1
    assert _main(monkeypatch, fake, policy_file([_entry("G3TECH1")])) == 0


def test_approving_one_collaborator_does_not_approve_another(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER, _user("G3TECH1"), _user("Newcomer")])
    assert _main(monkeypatch, fake, policy_file([_entry("G3TECH1")])) == 1
    out = capsys.readouterr().out
    assert "::error::Newcomer has write access but is not approved" in out
    assert "G3TECH1 has" not in out


def test_too_many_pages_fail_rather_than_loop(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER] + [_user(f"user{i}") for i in range(access_check.PAGE_SIZE * access_check.MAX_PAGES)])
    assert _main(monkeypatch, fake, policy_file()) == 1
    assert "more than 50 pages" in capsys.readouterr().out


def test_wrong_permission_for_an_approved_collaborator_fails(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER, _user("helper1", role="admin")])
    assert _main(monkeypatch, fake, policy_file([_entry("helper1")])) == 1
    assert "has admin access but was approved for write" in capsys.readouterr().out


def test_an_approval_never_accepted_is_a_notice_not_a_failure(monkeypatch, policy_file, capsys):
    assert _main(monkeypatch, FakeGitHub(), policy_file([_entry("helper1")])) == 0
    assert "::notice::helper1 is approved but is not a collaborator" in capsys.readouterr().out


def test_check_never_writes_to_github_or_the_policy(monkeypatch, policy_file):
    fake = FakeGitHub([OWNER, _user("Stranger")])
    path = policy_file()
    before = path.read_bytes()
    _main(monkeypatch, fake, path, "--invitations")
    assert all(method == "GET" for method, _ in fake.calls)
    assert path.read_bytes() == before


def test_an_unknown_account_is_not_added_to_the_allowlist(monkeypatch, policy_file):
    fake = FakeGitHub([OWNER, _user("Stranger")])
    path = policy_file()
    assert _main(monkeypatch, fake, path) == 1
    assert _main(monkeypatch, fake, path) == 1  # still unapproved on the next run
    assert json.loads(path.read_text(encoding="utf-8"))["approved_collaborators"] == []


# ----------------------------------------------------------------------------- invitations

def test_pending_invitations_are_listed_but_not_treated_as_collaborators(monkeypatch, policy_file, capsys):
    fake = FakeGitHub(invitations=[{"invitee": {"login": "invitee1"}, "permissions": "write"}])
    assert _main(monkeypatch, fake, policy_file(), "--invitations") == 0
    out = capsys.readouterr().out
    assert "pending invitation: invitee1 (write), NOT approved" in out
    assert "::error::" not in out


def test_an_accepted_invitation_is_checked_as_a_collaborator(monkeypatch, policy_file):
    fake = FakeGitHub(invitations=[{"invitee": {"login": "invitee1"}, "permissions": "write"}])
    assert _main(monkeypatch, fake, policy_file(), "--invitations") == 0
    fake.invitations, fake.collaborators = [], [OWNER, _user("invitee1")]
    assert _main(monkeypatch, fake, policy_file(), "--invitations") == 1


def test_invitations_are_not_read_unless_asked(monkeypatch, policy_file):
    fake = FakeGitHub()
    _main(monkeypatch, fake, policy_file())
    assert ("GET", f"/repos/{REPO}/invitations") not in fake.calls


def test_a_malformed_invitation_fails_rather_than_crashes(monkeypatch, policy_file, capsys):
    fake = FakeGitHub(invitations=[["not", "an", "invitation"]])
    assert _main(monkeypatch, fake, policy_file(), "--invitations") == 1
    assert "::error::a pending invitation has no invitee login" in capsys.readouterr().out


def test_unreadable_invitations_fail_rather_than_pass(monkeypatch, policy_file, capsys):
    fake = FakeGitHub()
    fake.fail.add(f"/repos/{REPO}/invitations")
    assert _main(monkeypatch, fake, policy_file(), "--invitations") == 1
    assert "matches" not in capsys.readouterr().out


# ----------------------------------------------------------------------------- failures

def test_api_error_is_a_failure_not_a_pass(monkeypatch, policy_file, capsys):
    fake = FakeGitHub()
    fake.fail.add(f"/repos/{REPO}/collaborators")
    assert _main(monkeypatch, fake, policy_file()) == 1
    out = capsys.readouterr().out
    assert "::error::GitHub API GET" in out and "matches" not in out


def test_an_empty_or_ownerless_listing_is_not_trusted(monkeypatch, policy_file, capsys):
    assert _main(monkeypatch, FakeGitHub([]), policy_file()) == 1
    assert "cannot be trusted" in capsys.readouterr().out
    assert _main(monkeypatch, FakeGitHub([_user("realspinn", role="write")]), policy_file()) == 1


@pytest.mark.parametrize("env", [{}, {"GITHUB_REPOSITORY": REPO}, {"GITHUB_TOKEN": "", "GITHUB_REPOSITORY": REPO}])
def test_missing_credentials_fail_safely(monkeypatch, policy_file, capsys, env):
    fake = FakeGitHub()
    assert _main(monkeypatch, fake, policy_file(), env=env) == 1
    assert fake.calls == []
    assert "nothing was checked" in capsys.readouterr().out


def test_invalid_credentials_fail_without_printing_the_token(monkeypatch, policy_file, capsys):
    fake = FakeGitHub()
    fake.fail.add(f"/repos/{REPO}/collaborators")  # what a 401 turns into
    assert _main(monkeypatch, fake, policy_file()) == 1
    assert "test-token" not in capsys.readouterr().out


def test_http_errors_from_the_real_client_become_access_errors(monkeypatch):
    def refuse(request, timeout):
        raise access_check.urllib.error.HTTPError(request.full_url, 401, "Bad credentials", {}, None)
    monkeypatch.setattr(access_check.urllib.request, "urlopen", refuse)
    with pytest.raises(access_check.AccessError, match="HTTP 401"):
        access_check.GitHubAPI("secret-value").get(f"/repos/{REPO}/collaborators")


@pytest.mark.parametrize("policy", [
    "not json",
    {"owner": "realspinn"},
    {"owner": "someone-else", "approved_collaborators": []},
    {"owner": "realspinn", "approved_collaborators": [_entry("realspinn")]},
    {"owner": "realspinn", "approved_collaborators": [_entry("a"), _entry("A")]},
    {"owner": "realspinn", "approved_collaborators": [_entry("helper1", permission="admin")]},
    {"owner": "realspinn", "approved_collaborators": [{**_entry("helper1"), "approved_on": "soon"}]},
    {"owner": "realspinn", "approved_collaborators": [{**_entry("helper1"), "reason": " "}]},
    {"owner": "realspinn", "approved_collaborators": [{"login": "helper1"}]},
    {"owner": "realspinn", "approved_collaborators": [_entry("bad login")]},
    {"owner": "realspinn", "approved_collaborators": [], "extra": True},
    {"owner": "realspinn", "approved_collaborators": [{**_entry("helper1"), "permission": ["write"]}]},
    {"owner": "realspinn", "approved_collaborators": [{**_entry("helper1"), "approved_on": "20261028"}]},
    {"owner": "realspinn", "approved_collaborators": [{**_entry("helper1"), "approved_on": 20261028}]},
    # A repeated key would make json keep only the last list, hiding entries from a reviewer.
    '{"owner": "realspinn", "approved_collaborators": [], "approved_collaborators": []}',
    '{"owner": "realspinn", "approved_collaborators": [{"login": "a", "login": "b", '
    '"permission": "write", "approved_on": "2026-10-28", "reason": "x"}]}',
])
def test_a_malformed_policy_fails_and_checks_nothing(monkeypatch, tmp_path, capsys, policy):
    path = tmp_path / "access-policy.json"
    path.write_text(policy if isinstance(policy, str) else json.dumps(policy), encoding="utf-8")
    fake = FakeGitHub()
    assert _main(monkeypatch, fake, path) == 1
    assert fake.calls == []
    assert "::error::" in capsys.readouterr().out


# ----------------------------------------------------------------------------- revoke

def test_revoke_removes_only_the_named_unapproved_collaborator(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER, _user("helper1"), _user("Stranger")])
    assert _main(monkeypatch, fake, policy_file([_entry("helper1")]), "--revoke", "stranger") == 0
    assert fake.deletes() == [f"/repos/{REPO}/collaborators/Stranger"]
    assert [u["login"] for u in fake.collaborators] == ["realspinn", "helper1"]
    assert "removed Stranger (write access)" in capsys.readouterr().out


def test_revoking_twice_deletes_once(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER, _user("Stranger")])
    assert _main(monkeypatch, fake, policy_file(), "--revoke", "Stranger") == 0
    assert _main(monkeypatch, fake, policy_file(), "--revoke", "Stranger") == 0
    assert len(fake.deletes()) == 1
    assert "not a collaborator; nothing removed" in capsys.readouterr().out


@pytest.mark.parametrize("login", ["realspinn", "REALSPINN"])
def test_the_owner_is_never_removed(monkeypatch, policy_file, capsys, login):
    fake = FakeGitHub()
    assert _main(monkeypatch, fake, policy_file(), "--revoke", login) == 1
    assert fake.deletes() == []
    assert "refusing to remove the repository owner" in capsys.readouterr().out


def test_an_approved_collaborator_is_not_revoked(monkeypatch, policy_file):
    fake = FakeGitHub([OWNER, _user("helper1")])
    assert _main(monkeypatch, fake, policy_file([_entry("helper1")]), "--revoke", "Helper1") == 1
    assert fake.deletes() == []


def test_an_admin_is_not_revoked(monkeypatch, policy_file):
    fake = FakeGitHub([OWNER, _user("other-admin", role="admin")])
    assert _main(monkeypatch, fake, policy_file(), "--revoke", "other-admin") == 1
    assert fake.deletes() == []


def test_revoke_never_touches_a_pending_invitation(monkeypatch, policy_file, capsys):
    fake = FakeGitHub(invitations=[{"invitee": {"login": "invitee1"}, "permissions": "write"}])
    assert _main(monkeypatch, fake, policy_file(), "--revoke", "invitee1") == 0
    assert fake.deletes() == [] and len(fake.invitations) == 1
    assert "nothing removed" in capsys.readouterr().out


def test_revoke_with_a_malformed_login_does_nothing(monkeypatch, policy_file):
    fake = FakeGitHub([OWNER, _user("Stranger")])
    assert _main(monkeypatch, fake, policy_file(), "--revoke", "../collaborators") == 1
    assert fake.calls == []


@pytest.mark.parametrize("status", [403, 404, 422])
def test_a_refused_removal_is_reported(monkeypatch, policy_file, capsys, status):
    fake = FakeGitHub([OWNER, _user("Stranger")])
    fake.delete_status = status
    assert _main(monkeypatch, fake, policy_file(), "--revoke", "Stranger") == 1
    assert f"HTTP {status}" in capsys.readouterr().out


def test_a_removal_that_did_not_take_effect_is_reported(monkeypatch, policy_file, capsys):
    fake = FakeGitHub([OWNER, _user("Stranger")])
    fake.ignore_delete = True
    assert _main(monkeypatch, fake, policy_file(), "--revoke", "Stranger") == 1
    assert "still a collaborator" in capsys.readouterr().out


# ----------------------------------------------------------------------------- repository files

def test_the_committed_policy_approves_no_collaborators():
    # Contributors work from forks; only the owner has direct access (.github/ACCESS.md).
    # An exception is the owner's own reviewed change, and this test changes with it.
    policy = access_check.load_policy(POLICY, REPO)
    assert policy == {"owner": "realspinn", "approved": {}}


def test_a_removed_collaborator_is_not_flagged_and_others_still_are(monkeypatch, policy_file, capsys):
    # After a contributor moves to a fork, only the owner is listed; anyone else is flagged.
    assert _main(monkeypatch, FakeGitHub([OWNER]), policy_file()) == 0
    assert _main(monkeypatch, FakeGitHub([OWNER, _user("G3TECH1")]), policy_file()) == 1
    assert "::error::G3TECH1 has write access but is not approved" in capsys.readouterr().out


def test_the_owner_reviews_every_change_to_the_policy():
    lines = [line.split() for line in (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert ["*", "@realspinn"] in lines


def test_workflow_is_read_only_and_uses_no_secret():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["access"]
    assert job["permissions"] == {"contents": "read"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GITHUB_TOKEN"}
    assert "--revoke" not in text


def test_workflow_runs_hourly_and_never_from_pull_request_target():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]  # YAML reads the key "on" as True
    assert set(triggers) == {"schedule", "workflow_dispatch", "pull_request"}
    assert triggers["schedule"] == [{"cron": "41 * * * *"}]
    checkout = workflow["jobs"]["access"]["steps"][0]
    assert checkout["with"] == {"persist-credentials": False}


def test_checkout_is_pinned_to_the_same_commit_as_the_release_workflow():
    pin = "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09  # v5.1.0"
    assert pin in (ROOT / ".github" / "workflows" / "release-macos.yml").read_text(encoding="utf-8")
    assert f"- uses: {pin}" in WORKFLOW.read_text(encoding="utf-8")
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["access"]["steps"]
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", s["uses"]) for s in steps if "uses" in s)
