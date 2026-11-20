#!/usr/bin/env python3
"""``gh``, with the fork as the only repository it can name.

Every GitHub call the maintenance job makes is built here. The fork comes
from one constant in ``common.py`` and is put into each command by two
private builders; no method takes a repository, so no caller and no value
read from GitHub can aim a call anywhere else. The host is always named too,
so an inherited ``GH_HOST`` cannot move the same owner and name elsewhere.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import json
from typing import Any

import common
from common import (
    DEFAULT_BRANCH,
    FORK_OWNER,
    FORK_REPO,
    FORK_SLUG,
    GITHUB_HOST,
    LABELS,
    CommandResult,
    Log,
)

GH_TIMEOUT_SEC = 60
# `gh` pages through results up to these limits. They are set far above
# anything the fork holds: a list cut short reads as "no such issue" or "no
# such pull request", which is how a duplicate would be opened.
LABEL_LIST_LIMIT = 1000
ISSUE_LIST_LIMIT = 5000
PULL_LIST_LIMIT = 1000
PULL_FIELDS = (
    "number,state,url,createdAt,headRefOid,baseRefName,author,isCrossRepository"
)
ISSUE_FIELDS = "number,state,body"
COMMENT_JQ = ".[] | {id: .id, body: .body, author: .user.login}"


class GitHub:
    """``gh``, with the fork as the only repository it can name.

    Every call but the login probe goes through :meth:`_repo` or :meth:`_api`,
    which put the fork into the command themselves. No method takes a
    repository, so no caller and no value read from GitHub can aim a call
    anywhere else.
    """

    def __init__(self, env: dict[str, str], log: Log) -> None:
        self.env = env
        self.log = log
        self._login: str | None = None

    def _gh(self, args: list[str], timeout: float | None = None) -> CommandResult:
        deadline = GH_TIMEOUT_SEC if timeout is None else timeout
        result = common.run_command(["gh", *args], deadline, env=self.env)
        self.log.command(result)
        return result

    def _repo(self, group: str, verb: str, *rest: str) -> CommandResult:
        """``gh <group> <verb> --repo <fork> ...``"""
        return self._gh([group, verb, "--repo", FORK_REPO, *rest])

    def _api(self, path: str, *rest: str) -> CommandResult:
        """``gh api repos/<fork>/<path> ...``"""
        return self._gh(
            ["api", "--hostname", GITHUB_HOST, f"repos/{FORK_SLUG}/{path}", *rest]
        )

    def _listing(
        self, group: str, fields: str, limit: int, *filters: str
    ) -> list[dict[str, Any]] | None:
        """``gh <group> list`` on the fork, as parsed objects; None on failure."""
        options = (*filters, "--json", fields, "--limit", str(limit))
        result = self._repo(group, "list", *options)
        if not result.ok:
            return None
        try:
            parsed = json.loads(result.stdout)
        except ValueError:
            return None
        if not isinstance(parsed, list):
            return None
        return [item for item in parsed if isinstance(item, dict)]

    def login(self, timeout: float | None = None) -> str | None:
        """The authenticated account, asked once per run. The one call that
        names no repository: it is how a wrong account is caught."""
        if self._login is None:
            probe = ["api", "--hostname", GITHUB_HOST, "user", "--jq", ".login"]
            result = self._gh(probe, timeout)
            self._login = result.stdout.strip() if result.ok else None
        return self._login

    def is_owner(self, timeout: float | None = None) -> bool:
        return self.login(timeout) == FORK_OWNER

    def pulls_for_head(self, branch: str) -> list[dict[str, Any]] | None:
        """The owner's pull requests from the fork's own branch into the
        default branch, in every state. The filters are sent and then checked
        again on what comes back."""
        filters = (
            "--head", branch, "--base", DEFAULT_BRANCH,
            "--author", FORK_OWNER, "--state", "all",
        )  # fmt: skip
        found = self._listing("pr", PULL_FIELDS, PULL_LIST_LIMIT, *filters)
        if found is None:
            return None
        return [pull for pull in found if self._is_own_pull(pull)]

    def pull(self, number: int) -> dict[str, Any] | None:
        """One pull request of the fork by number: its state, the commit it
        was opened from, and its merge commit. None when it cannot be read or
        is not the owner's own pull request into the default branch."""
        fields = f"{PULL_FIELDS},mergeCommit"
        result = self._repo("pr", "view", str(number), "--json", fields)
        if not result.ok:
            return None
        try:
            found = json.loads(result.stdout)
        except ValueError:
            return None
        if not isinstance(found, dict) or not self._is_own_pull(found):
            return None
        return found

    @staticmethod
    def _is_own_pull(pull: dict[str, Any]) -> bool:
        return (
            pull.get("isCrossRepository") is False
            and pull.get("baseRefName") == DEFAULT_BRANCH
            and isinstance(pull.get("author"), dict)
            and pull["author"].get("login") == FORK_OWNER
        )

    def create_pull(self, branch: str, title: str, body: str) -> CommandResult:
        target = ("--base", DEFAULT_BRANCH, "--head", branch)
        return self._repo("pr", "create", *target, "--title", title, "--body", body)

    def enable_auto_merge(self, number: int, sha: str) -> CommandResult:
        """Idempotent: asking again for a pull request that already has it on
        changes nothing. The head commit is pinned, so a branch that moved
        after the checks here is not merged."""
        how = ("--auto", "--squash", "--match-head-commit", sha)
        return self._repo("pr", "merge", str(number), *how)

    def disable_auto_merge(self, number: int) -> CommandResult:
        return self._repo("pr", "merge", str(number), "--disable-auto")

    def issues_with_label(
        self, label: str, state: str = "all"
    ) -> list[dict[str, Any]] | None:
        filters = ("--label", label, "--state", state)
        return self._listing("issue", ISSUE_FIELDS, ISSUE_LIST_LIMIT, *filters)

    def ensure_label(self, name: str) -> bool:
        """Create one of the job's labels when the fork does not have it."""
        color, description = LABELS[name]
        existing = self._listing("label", "name", LABEL_LIST_LIMIT)
        if existing is None:
            return False
        if any(label.get("name") == name for label in existing):
            return True
        look = ("--color", color, "--description", description)
        return self._repo("label", "create", name, *look).ok

    def create_issue(self, title: str, body: str, label: str) -> CommandResult:
        # Marked as a job's words: the issue is created under the owner's
        # account and quotes text the owner did not write.
        marked = f"{common.job_marker(common.LOCAL_JOB)}\n{body}"
        text = ("--title", title, "--body", marked)
        return self._repo("issue", "create", *text, "--label", label)

    def comments(self, issue: int) -> list[dict[str, Any]] | None:
        """Every comment of an issue with its author: one object per line."""
        path = f"issues/{issue}/comments?per_page=100"
        result = self._api(path, "--paginate", "--jq", COMMENT_JQ)
        if not result.ok:
            return None
        found: list[dict[str, Any]] = []
        for line in result.stdout.split("\n"):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                return None
            if isinstance(item, dict):
                found.append(item)
        return found

    # `-f` sends the value as a raw string; `-F` would read a leading `@` as a
    # file name.
    def update_comment(self, comment_id: int, body: str) -> CommandResult:
        path = f"issues/comments/{comment_id}"
        return self._api(path, "-X", "PATCH", "-f", f"body={body}")

    def create_comment(self, issue: int, body: str) -> CommandResult:
        return self._api(f"issues/{issue}/comments", "-X", "POST", "-f", f"body={body}")
