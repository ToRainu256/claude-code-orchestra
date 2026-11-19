#!/usr/bin/env python3
"""Open an auto-merging pull request when a newer model of the family exists.

The step asks the Codex CLI for its model catalog, picks a candidate with a
pure function, and if there is one prepares the bump in a scratch worktree of
the default branch: manifest edit, rendered files, a strict manifest check, a
local commit, the bump-shape check on that commit and one live call that names
the new slug. Only then is the commit pushed and a pull request opened on the
fork with auto-merge enabled. The repository's full gates run as that pull
request's required checks.

Two trees are involved and they are never mixed up:

* the **approved checkout** (``ctx.tools_root``): the only repository code
  this step executes, namely its own ``models.py`` and ``codex_consult.py``;
* the **scratch worktree** of ``refs/remotes/origin/main``: data only. The
  approved tools are pointed at it with ``--project-root``; nothing in it is
  run.

The catalog is untrusted input. A slug is used only after it matches
``common.SLUG_RE``, which is what makes it safe as an argv element, a branch
name and a configuration value. No command goes through a shell, and every
one has a deadline.

The step is a resumable state machine keyed on the branch name
``bot/models-<slug>``: whatever a previous run left on GitHub decides where
this one continues, so a crash never leaves a state that blocks later runs or
opens a second pull request. Whatever is found there is verified again by
this run before it is acted on: auto-merge is only ever asked for on a commit
this run has itself found to be exactly the bump.

The bump is always built here, by this run. A branch found on the fork is
used only when its two files are byte for byte the ones this run built; this
step never vouches for a bump someone else prepared. Every bump it does vouch
for is recorded locally (``bump.record_bump``) and tied to the pull request
opened for it; the promotion rule accepts no other.

What the default branch names now is read from the manifest file at that
commit itself. A project override file on the fork is not consulted, and the
step does nothing while one is there.

Outcomes and exit codes: ``OUTCOMES`` below, explained in
``scripts/maint/README.md``.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bump
import common
import promote

STEP = "model-watch"
BRANCH_PREFIX = "bot/models-"
WORKTREE_NAME = "model-watch"
MODELS_TOOL = ".claude/skills/_shared/models.py"
CONSULT_TOOL = ".claude/skills/_shared/codex_consult.py"
EFFORT_CONFIG_KEY = "model_reasoning_effort"

PREFLIGHT_TIMEOUT_SEC = 30
CATALOG_TIMEOUT_SEC = 120
PREPARE_TIMEOUT_SEC = 60
SMOKE_CALL_TIMEOUT_SEC = 600
# The wrapper enforces its own timeout; this is the backstop around it.
SMOKE_DEADLINE_SEC = SMOKE_CALL_TIMEOUT_SEC + 60
# A bump pull request open this long has a failing check or a conflict.
BUMP_PR_STUCK_DAYS = 2
REPLY_SHOWN = 80

VISIBLE = "list"
SMOKE_PROMPT = "Reply with exactly: OK"
SMOKE_REPLY = "OK"
SMOKE_LABEL = "model-smoke"
SMOKE_CALLER = "model-watch"

RESULT_CANDIDATE = "candidate"
RESULT_UP_TO_DATE = "up-to-date"
RESULT_NOT_LISTED = "current-not-listed"
RESULT_EFFORT = "effort-unsupported"
RESULT_UNPARSEABLE = "current-unparseable"
NOTICE_FAILED = "notice-failed"

_OK, _OWNER, _FAILED = common.EXIT_OK, common.EXIT_NEEDS_OWNER, common.EXIT_FAILED
# Every outcome this step reports, with the exit codes it may carry. The
# README table is pinned to this by a test.
OUTCOMES: dict[str, tuple[int, ...]] = {
    "auth-or-tool": (_FAILED,),
    "catalog-fetch-failed": (_FAILED,),
    "git-failed": (_FAILED,),
    "github-failed": (_FAILED,),
    "manifest-unreadable": (_OWNER,),
    "override-in-tree": (_OWNER,),
    RESULT_UNPARSEABLE: (_OWNER,),
    RESULT_UP_TO_DATE: (_OK,),
    RESULT_NOT_LISTED: (_OK, _OWNER),
    RESULT_EFFORT: (_OK, _OWNER),
    NOTICE_FAILED: (_FAILED,),
    "already-merged": (_OK,),
    "rejected-by-owner": (_OK,),
    "pending": (_OK, _OWNER),
    "unknown-branch": (_OWNER,),
    "apply-failed": (_FAILED,),
    "unexpected-diff": (_FAILED,),
    "smoke-failed": (_FAILED,),
    "push-failed": (_FAILED,),
    "pr-create-failed": (_FAILED,),
    "pr-url-outside-fork": (_FAILED,),
    "auto-merge-not-enabled": (_OWNER,),
    "pr-opened": (_OK,),
}


class CatalogError(Exception):
    """The catalog is not the shape the candidate rule reads."""


@dataclass(frozen=True)
class Selection:
    """What the catalog says about the current model.

    ``slug`` is the candidate for ``candidate`` and the newest same-family
    slug for ``effort-unsupported``. ``family_behind`` names another family's
    newest version when it is ahead of every listed version of the current
    family: a notice for the owner, never a switch.
    """

    result: str
    slug: str | None = None
    family_behind: common.Slug | None = None


def _supports(entry: dict[str, Any], effort: str) -> bool:
    levels = entry.get("supported_reasoning_levels")
    if not isinstance(levels, list):
        return False
    return any(
        isinstance(level, dict) and level.get("effort") == effort for level in levels
    )


def _listed(catalog: object) -> list[tuple[common.Slug, dict[str, Any]]]:
    """Visible catalog entries whose slug parses. Everything else is ignored.

    A catalog with no such entry at all is not a catalog this rule can read:
    a format change must not be reported as "the model was retired".
    """
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        raise CatalogError("the catalog has no list of models")
    found: list[tuple[common.Slug, dict[str, Any]]] = []
    for entry in models:
        if not isinstance(entry, dict) or entry.get("visibility") != VISIBLE:
            continue
        slug = common.parse_slug(entry.get("slug"))
        if slug is not None:
            found.append((slug, dict(entry)))
    if not found:
        raise CatalogError("the catalog lists no visible model slug")
    return found


def pick_candidate(catalog: object, current: str, effort: str) -> Selection:
    """Choose the slug to bump to. Pure: no I/O, no clock.

    The candidate is the highest version of the current slug's own vendor and
    family that is strictly newer than the current one. It is never a lower
    version and never another family. If that newest slug does not support
    the configured effort there is no candidate: an older one is not chosen
    in its place. Raises :class:`CatalogError` for a catalog it cannot read.
    """
    now = common.parse_slug(current)
    if now is None:
        return Selection(RESULT_UNPARSEABLE)
    listed = _listed(catalog)
    if not any(slug.text == now.text for slug, _ in listed):
        return Selection(RESULT_NOT_LISTED)

    same = [(slug, entry) for slug, entry in listed if slug.same_line(now)]
    newest_same = max(slug.version for slug, _ in same)
    others = [
        slug
        for slug, _ in listed
        if slug.vendor == now.vendor
        and slug.family != now.family
        and slug.version > newest_same
    ]
    behind = max(others, key=lambda slug: (slug.version, slug.family), default=None)

    newer = [(slug, entry) for slug, entry in same if slug.version > now.version]
    if not newer:
        return Selection(RESULT_UP_TO_DATE, family_behind=behind)
    best, entry = max(newer, key=lambda pair: pair[0].version)
    if not _supports(entry, effort):
        return Selection(RESULT_EFFORT, best.text, behind)
    return Selection(RESULT_CANDIDATE, best.text, behind)


@dataclass(frozen=True)
class Outcome:
    result: str
    exit_code: int
    detail: str = ""


@dataclass(frozen=True)
class Bump:
    """One bump being worked on: where the default branch is, what it names
    now, and what it would name next."""

    main: str
    old: str
    slug: str
    effort: str

    @property
    def branch(self) -> str:
        return BRANCH_PREFIX + self.slug


def tool_command(ctx: common.Context, rel: str, args: list[str]) -> list[str]:
    """argv for one of the approved checkout's own scripts, run by the
    interpreter that is running this one."""
    return [sys.executable, str(ctx.tools_root / rel), *args]


def _worktree(ctx: common.Context) -> Path:
    return ctx.paths.run_dir / WORKTREE_NAME


def _remove_worktree(ctx: common.Context) -> None:
    """Drop the scratch worktree and forget it. Safe when there is none."""
    tree = common.require_scratch(ctx.paths, _worktree(ctx))
    if tree.exists():
        ctx.git.run(["worktree", "remove", "--force", str(tree)])
    if tree.exists():
        shutil.rmtree(str(tree))
    ctx.git.run(["worktree", "prune"])


def _models_tool(
    ctx: common.Context, tree: Path, args: list[str], timeout: float
) -> common.CommandResult:
    """The approved checkout's manifest tool, pointed at the scratch tree."""
    argv = tool_command(ctx, MODELS_TOOL, ["--project-root", str(tree), *args])
    result = common.run_command(argv, timeout, env=ctx.env)
    ctx.log.command(result)
    return result


def _manifest_values(ctx: common.Context, main: str) -> tuple[str, str] | None:
    """(model, effort) as the manifest file at the default branch names them.

    Read from that one blob, not through the manifest tool: the tool resolves
    a project's own override file, and a file of that name on the fork must
    not decide what this step believes the current model is. The values come
    from the fork, so they are held to a strict shape before any use.
    """
    entry = bump.tree_entry(ctx.git, main, bump.MANIFEST_REL)
    if entry is None or entry[:2] != bump.REGULAR_ENTRY:
        return None
    document = bump.parse_manifest(ctx.git.show(main, bump.MANIFEST_REL) or "")
    section = document.get(bump.MODEL_SECTION) if document else None
    if not isinstance(section, dict):
        return None
    model, effort = section.get(bump.MODEL_LEAF), section.get(bump.EFFORT_LEAF)
    if not isinstance(model, str) or not common.MODEL_VALUE_RE.fullmatch(model):
        return None
    if not isinstance(effort, str) or not common.EFFORT_RE.fullmatch(effort):
        return None
    return model, effort


def preflight(ctx: common.Context) -> str | None:
    """Why the step cannot start, or None. Fails closed on a wrong account."""
    if not ctx.github.is_owner(PREFLIGHT_TIMEOUT_SEC):
        return "gh is not authenticated as the fork owner"
    version = common.run_command(
        ["codex", "--version"], PREFLIGHT_TIMEOUT_SEC, env=ctx.env
    )
    ctx.log.command(version)
    return None if version.ok else version.failure()


def fetch_catalog(ctx: common.Context, scratch: Path) -> tuple[object, str | None]:
    """The live catalog as parsed JSON, or the reason it could not be had.

    Run from a scratch directory: the CLI must not pick up a project's own
    configuration from wherever the job happens to stand.
    """
    result = common.run_command(
        ["codex", "debug", "models"], CATALOG_TIMEOUT_SEC, cwd=scratch, env=ctx.env
    )
    ctx.log.command(result)
    if not result.ok:
        return None, result.failure()
    try:
        return json.loads(result.stdout), None
    except ValueError:
        return None, "the catalog is not JSON"


def _notice(ctx: common.Context, marker: str, title: str, body: str) -> str:
    """Make sure the owner was asked exactly once. Returns ``open`` (already
    asked or asked now), ``closed`` (the owner answered) or ``failed``.

    Issues are matched by a marker in their body among the labeled issues of
    any state, read as a list: the search index lags, and a rerun minutes
    after the first must not ask twice.
    """
    existing = ctx.github.issues_with_label(common.LABEL_NEEDS_OWNER)
    if existing is None:
        return "failed"
    states = [
        str(issue.get("state", "")).upper()
        for issue in existing
        if marker in str(issue.get("body", ""))
    ]
    if "OPEN" in states:
        return "open"
    if states:
        return "closed"
    if not ctx.github.ensure_label(common.LABEL_NEEDS_OWNER):
        return "failed"
    created = ctx.github.create_issue(
        title, f"{body}\n\n{marker}\n", common.LABEL_NEEDS_OWNER
    )
    if not created.ok or not common.ISSUE_URL_RE.fullmatch(created.stdout.strip()):
        return "failed"
    return "open"


def _family_notice(ctx: common.Context, now: common.Slug, other: common.Slug) -> str:
    return _notice(
        ctx,
        f"<!-- family-decision:{other.family}:{other.version_text()} -->",
        f"Model watch: family `{other.family}` is at {other.version_text()}",
        f"The catalog lists `{other.text}`, which is newer than every listed "
        f"version of the family in use (`{now.family}`, current `{now.text}`).\n\n"
        "This is information only. The model watch never switches family; a "
        "version number does not show that a switch is right. Close this "
        "issue when you have decided.",
    )


def _blocking_notice(
    ctx: common.Context, selection: Selection, current: str, effort: str
) -> Outcome:
    """The two results that need the owner before any bump can happen."""
    if selection.result == RESULT_NOT_LISTED:
        state = _notice(
            ctx,
            f"<!-- model-not-listed:{current} -->",
            f"Model watch: `{current}` is not in the catalog",
            f"The configured model `{current}` is not listed by the catalog. "
            "Either the catalog is stale or the model was retired. No bump "
            "is attempted while this is the case. Close this issue when it "
            "is resolved or intended.",
        )
    else:
        state = _notice(
            ctx,
            f"<!-- effort-unsupported:{selection.slug}:{effort} -->",
            f"Model watch: `{selection.slug}` does not support effort `{effort}`",
            f"`{selection.slug}` is the newest model of the family in use, "
            f"but it does not list the configured reasoning effort `{effort}`. "
            "An older model is never chosen instead. Close this issue when "
            "you have decided.",
        )
    if state == "failed":
        return Outcome(NOTICE_FAILED, _FAILED, selection.result)
    if state == "closed":
        return Outcome(selection.result, _OK, "the owner closed the notice")
    return Outcome(selection.result, _OWNER, "notice is open")


def _pull_number(url: object) -> int | None:
    """The pull-request number, only for a URL under the fork."""
    if not isinstance(url, str):
        return None
    match = common.PULL_URL_RE.fullmatch(url.strip())
    return int(match.group(1)) if match else None


def _auto_merge(ctx: common.Context, url: object, sha: str, done: str) -> Outcome:
    number = _pull_number(url)
    if number is None:
        return Outcome("pr-url-outside-fork", _FAILED, common.one_line(str(url)))
    enabled = ctx.github.enable_auto_merge(number, sha)
    if not enabled.ok:
        return Outcome("auto-merge-not-enabled", _OWNER, enabled.failure())
    return Outcome(done, _OK, f"pull request {number}")


def _verified_branch(ctx: common.Context, bump_: Bump, built: str) -> tuple[str, str]:
    """What the fork's bump branch is, checked by this run against the bump
    this run built (``built``).

    Returns ``("ok", commit)`` when the branch is exactly one commit on top of
    the default branch's history, that commit is exactly this bump, and its
    two files are the very objects this run built; ``("absent", "")`` when
    there is no such branch; ``("other", why)`` when it is anything else,
    including a well-formed bump with other bytes; ``("failed", why)`` when
    git could not tell, which is a failure to retry and not a statement about
    the branch.
    """
    branch_ref = f"refs/heads/{bump_.branch}"
    tracking = f"{common.TRACKING_PREFIX}{bump_.branch}"
    heads = ctx.git.run(["ls-remote", "--heads", common.REMOTE, branch_ref])
    if not heads.ok:
        return "failed", heads.failure()
    if not heads.stdout.strip():
        return "absent", ""
    fetched = ctx.git.run(
        ["fetch", "--quiet", "--no-tags", common.REMOTE, f"+{branch_ref}:{tracking}"]
    )
    head = ctx.git.commit_of(tracking) if fetched.ok else None
    if head is None:
        return "failed", fetched.failure()
    base = ctx.git.text(["merge-base", bump_.main, head])
    if base is None or ctx.git.commit_of(f"{head}^") != base:
        return "other", f"not one commit on top of {common.DEFAULT_BRANCH}"
    problem = bump.bump_problem_between(ctx.git, base, head, bump_.slug)
    if problem is not None:
        return "other", problem
    if bump.blobs_of(ctx.git, head) != bump.blobs_of(ctx.git, built):
        return "other", "a bump, but not with the files this Mac builds"
    return "ok", head


def smoke_problem(ctx: common.Context, slug: str, effort: str) -> str | None:
    """One live call that names the slug and the effort. None when it worked.

    The wrapper reports the model it was asked for, not the one that
    answered, so both flags are passed explicitly and the reply itself is
    checked: ``ok`` alone is not accepted.
    """
    common.ensure_private_dir(ctx.paths.run_dir)
    scratch = Path(tempfile.mkdtemp(prefix="smoke-", dir=str(ctx.paths.run_dir)))
    try:
        prompt = scratch / "prompt.md"
        prompt.write_text(SMOKE_PROMPT + "\n", encoding="utf-8")
        argv = tool_command(
            ctx,
            CONSULT_TOOL,
            [
                "--prompt-file", str(prompt),
                "--model", slug,
                "--config", f"{EFFORT_CONFIG_KEY}={effort}",
                "--sandbox", "read-only",
                "--skip-git-repo-check",
                "--label", SMOKE_LABEL,
                "--caller", SMOKE_CALLER,
                "--timeout", str(SMOKE_CALL_TIMEOUT_SEC),
                "--project-root", str(scratch),
            ],
        )  # fmt: skip
        result = common.run_command(argv, SMOKE_DEADLINE_SEC, cwd=scratch, env=ctx.env)
        ctx.log.command(result)
        if not result.ok:
            return result.failure()
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return "the wrapper did not print JSON"
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return "the wrapper did not report ok"
        response = scratch / str(payload.get("response_file", ""))
        inside = scratch.resolve() in response.resolve().parents
        if not inside or not response.is_file():
            return "the wrapper named no response file of its own"
        reply = response.read_text(encoding="utf-8", errors="replace").strip()
        if reply != SMOKE_REPLY:
            return f"unexpected reply: {common.one_line(reply, REPLY_SHOWN)}"
        return None
    finally:
        shutil.rmtree(str(common.require_scratch(ctx.paths, scratch)))


def _pull_text(bump: Bump) -> tuple[str, str]:
    title = f"chore(models): bump Codex to {bump.slug}"
    body = (
        f"Bumps the Codex model from `{bump.old}` to `{bump.slug}` (reasoning "
        f"effort `{bump.effort}` unchanged).\n\n"
        "Opened by the local model watch after these checks passed on the "
        "maintenance machine:\n\n"
        "- the manifest check in strict mode, on the rendered tree\n"
        "- the commit touches the manifest and the Codex configuration file "
        "only, and only the model value\n"
        f"- one live call naming `{bump.slug}` at effort `{bump.effort}` "
        "returned the expected reply\n\n"
        "Auto-merge is enabled. The required checks of this pull request "
        "are the full gates; nothing else from this branch ran locally.\n\n"
        f"<!-- model-watch:{bump.slug} -->\n"
    )
    return title, body


def _vouch_and_open(
    ctx: common.Context, bump_: Bump, head: str, push: Path | None
) -> Outcome:
    """The live call, the last shape check, the local record, the push (when
    the commit is still local) and the pull request, in that order, for a
    commit already found to be exactly the bump this run built."""
    problem = smoke_problem(ctx, bump_.slug, bump_.effort)
    if problem is not None:
        return Outcome("smoke-failed", _FAILED, problem)
    if push is not None:
        # Checked again against the default branch as it is fetched right
        # now, by its full name, immediately before anything leaves the Mac.
        main = ctx.git.commit_of(common.MAIN_REF)
        problem = "the default branch moved" if main != bump_.main else None
        problem = problem or bump.bump_problem_between(
            ctx.git, bump_.main, head, bump_.slug
        )
        if problem is not None:
            return Outcome("unexpected-diff", _FAILED, problem)
    # From here on this Mac vouches for the commit: the promotion rule
    # accepts a bump only as the merge of the pull request opened below.
    if not bump.record_bump(ctx, bump_.slug, head):
        return Outcome("git-failed", _FAILED, "cannot read the bump commit")
    if push is not None:
        refspec = f"{head}:refs/heads/{bump_.branch}"
        pushed = ctx.git.run(["push", "--quiet", common.REMOTE, refspec], cwd=push)
        if not pushed.ok:
            return Outcome("push-failed", _FAILED, pushed.failure())
    title, body = _pull_text(bump_)
    created = ctx.github.create_pull(bump_.branch, title, body)
    if not created.ok:
        return Outcome("pr-create-failed", _FAILED, created.failure())
    url = created.stdout.strip().split("\n")[-1]
    number = _pull_number(url)
    if number is not None:
        bump.attach_pull(ctx.paths, bump_.slug, head, number)
    return _auto_merge(ctx, url, head, "pr-opened")


def _prepare(
    ctx: common.Context, tree: Path, bump_: Bump
) -> tuple[str | None, Outcome | None]:
    """Build the bump as one local commit and prove its shape. Nothing has
    left the Mac when this returns."""
    for args in (
        ["set", bump.MODEL_KEY, bump_.slug],
        ["apply"],
        ["check", "--strict"],
    ):
        done = _models_tool(ctx, tree, args, PREPARE_TIMEOUT_SEC)
        if not done.ok:
            return None, Outcome("apply-failed", _FAILED, done.failure())
    title, _ = _pull_text(bump_)
    for args in (["add", "--all"], ["commit", "--quiet", "--no-verify", "-m", title]):
        done = ctx.git.run(args, cwd=tree)
        if not done.ok:
            return None, Outcome("git-failed", _FAILED, done.failure())
    head = ctx.git.text(["rev-parse", "--verify", "HEAD^{commit}"], cwd=tree)
    if head is None or ctx.git.commit_of(f"{head}^") != bump_.main:
        return None, Outcome("git-failed", _FAILED, "cannot read the new commit")
    problem = bump.bump_problem_between(ctx.git, bump_.main, head, bump_.slug)
    if problem is not None:
        return None, Outcome("unexpected-diff", _FAILED, problem)
    return head, None


def _withdraw(ctx: common.Context, pull: dict[str, Any], why: str) -> Outcome:
    """An open pull request whose branch is not this bump: take auto-merge
    back, so a branch someone pushed to cannot merge on a request made for
    another commit."""
    number = _pull_number(pull.get("url"))
    withdrawn = number is not None and ctx.github.disable_auto_merge(number).ok
    note = "auto-merge withdrawn" if withdrawn else "auto-merge could NOT be withdrawn"
    return Outcome("unknown-branch", _OWNER, f"open pull request: {why}; {note}")


def _resume_open(
    ctx: common.Context, pull: dict[str, Any], bump_: Bump, built: str
) -> Outcome:
    """An open pull request exists: keep auto-merge on for the commit this run
    verified against its own build, and say so when it has waited too long
    for anything but a person to fix. What GitHub reports as the head is
    never trusted."""
    kind, value = _verified_branch(ctx, bump_, built)
    if kind == "failed":
        return Outcome("git-failed", _FAILED, value)
    if kind != "ok":
        return _withdraw(ctx, pull, value or "the branch is gone")
    outcome = _auto_merge(ctx, pull.get("url"), value, "pending")
    if outcome.exit_code != _OK:
        return outcome
    created = common.parse_github_time(pull.get("createdAt"))
    limit = BUMP_PR_STUCK_DAYS * common.SECONDS_PER_DAY
    if created is not None and ctx.now - created > limit:
        return Outcome("pending", _OWNER, f"{outcome.detail}: stuck")
    return outcome


def _bump(ctx: common.Context, tree: Path, bump_: Bump) -> Outcome:
    """Let what is already on GitHub decide where to continue, after building
    the bump here: whatever is found there is measured against that build."""
    pulls = ctx.github.pulls_for_head(bump_.branch)
    if pulls is None:
        return Outcome("github-failed", _FAILED, "cannot list pull requests")
    states = {str(pull.get("state", "")).upper(): pull for pull in pulls}
    if "MERGED" in states:
        return Outcome("already-merged", _OK, bump_.branch)
    if states and "OPEN" not in states:
        # The owner said no. The record made for that pull request must not
        # promote the same files when they arrive some other way.
        bump.drop_record(ctx.paths, bump_.slug)
        return Outcome("rejected-by-owner", _OK, bump_.branch)

    built, failure = _prepare(ctx, tree, bump_)
    if failure is not None or built is None:
        return failure or Outcome("git-failed", _FAILED, "no commit")
    if "OPEN" in states:
        return _resume_open(ctx, states["OPEN"], bump_, built)
    kind, value = _verified_branch(ctx, bump_, built)
    if kind == "failed":
        return Outcome("git-failed", _FAILED, value)
    if kind == "other":
        return Outcome("unknown-branch", _OWNER, f"{bump_.branch}: {value}")
    if kind == "ok":
        # The branch is there without a pull request and holds exactly what
        # this run built: a previous run stopped after its push. The live
        # call is made again before anything is recorded.
        return _vouch_and_open(ctx, bump_, value, None)
    return _vouch_and_open(ctx, bump_, built, tree)


def _watch(
    ctx: common.Context, tree: Path, main: str, catalog: object
) -> tuple[Outcome, int]:
    """The outcome, and how many informational notices could not be filed."""
    if bump.tree_entry(ctx.git, main, promote.LOCAL_OVERRIDE_REL) is not None:
        why = f"the default branch carries {promote.LOCAL_OVERRIDE_REL}; remove it"
        return Outcome("override-in-tree", _OWNER, why), 0
    values = _manifest_values(ctx, main)
    if values is None:
        why = "the manifest at the default branch could not be read"
        return Outcome("manifest-unreadable", _OWNER, why), 0
    current, effort = values
    try:
        selection = pick_candidate(catalog, current, effort)
    except CatalogError as exc:
        return Outcome("catalog-fetch-failed", _FAILED, str(exc)), 0
    ctx.log.line(f"model-watch: {selection.result} {selection.slug or ''}")
    if selection.result == RESULT_UNPARSEABLE:
        return Outcome(RESULT_UNPARSEABLE, _OWNER, current), 0

    now = common.parse_slug(current)
    unfiled = 0
    if selection.family_behind is not None and now is not None:
        # Information only: failing to file it never replaces the outcome.
        unfiled = int(_family_notice(ctx, now, selection.family_behind) == "failed")
    if selection.result in (RESULT_NOT_LISTED, RESULT_EFFORT):
        return _blocking_notice(ctx, selection, current, effort), unfiled
    if selection.result == RESULT_CANDIDATE and selection.slug is not None:
        return _bump(ctx, tree, Bump(main, current, selection.slug, effort)), unfiled
    return Outcome(RESULT_UP_TO_DATE, _OK, current), unfiled


def _result(outcome: Outcome, unfiled: int = 0) -> common.StepResult:
    counts = {NOTICE_FAILED: unfiled} if unfiled else None
    return common.checked_result(
        OUTCOMES, STEP, outcome.result, outcome.exit_code, outcome.detail, counts
    )


def run(ctx: common.Context) -> common.StepResult:
    """The scheduled step."""
    common.require_owned_clone(ctx.git)
    problem = preflight(ctx)
    if problem is not None:
        return _result(Outcome("auth-or-tool", _FAILED, problem))

    common.ensure_private_dir(ctx.paths.run_dir)
    scratch = Path(tempfile.mkdtemp(prefix="catalog-", dir=str(ctx.paths.run_dir)))
    try:
        catalog, problem = fetch_catalog(ctx, scratch)
    finally:
        shutil.rmtree(str(common.require_scratch(ctx.paths, scratch)))
    if problem is not None:
        return _result(Outcome("catalog-fetch-failed", _FAILED, problem))

    main = ctx.git.commit_of(common.MAIN_REF)
    _remove_worktree(ctx)
    tree = _worktree(ctx)
    added = (
        main is not None
        and ctx.git.run(["worktree", "add", "--quiet", "--detach", str(tree), main]).ok
    )
    if main is None or not added:
        why = "cannot create the scratch worktree"
        return _result(Outcome("git-failed", _FAILED, why))
    try:
        outcome, unfiled = _watch(ctx, tree, main, catalog)
    finally:
        _remove_worktree(ctx)
    ctx.log.line(f"model-watch: {outcome.result} exit {outcome.exit_code}")
    return _result(outcome, unfiled)
