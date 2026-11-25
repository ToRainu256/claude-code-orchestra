#!/usr/bin/env python3
"""Decide which merged revision this Mac may execute and distribute.

Merging and promoting are separate. Green pull requests merge to the default
branch on their own; this machine runs only the **approved revision**, one
commit SHA in ``state/approved-rev`` under the state root. This module looks
at the range from that commit to ``refs/remotes/origin/main`` and advances the
file by itself in exactly two cases:

* **Inert range.** No changed path is one the Mac executes or ships: nothing
  under ``scripts/``, no ``VERSION``, no model override file, nothing the
  approved ``scripts/update.sh`` copies into a project (its ``SAFE_DIRS``,
  ``SAFE_FILES``, ``SETTINGS_FILES`` and every literal ``${TEMPLATE_DIR}/…``
  source), and no file git itself interprets at checkout.
* **This Mac's own model bump.** The executed-or-shipped changes are exactly
  one model bump (``bump.bump_problem_between``), **and** it is the merge of
  the pull request this Mac's model watch opened for it, from the commit it
  tested (``bump.recorded_bump_problem``). The record is used once. The same
  change merged by any other means waits.

In both cases every entry the range adds or changes must be a plain file whose
path the Mac's file system can hold. A link, a gitlink, or a name or path over
the file system's length limits is never promoted without the owner, wherever
it is: it can make the tree impossible to check out, which would leave the Mac
running an older revision than the one recorded as approved.

Everything else waits for the owner to run ``launchd.sh promote``. There is no
setting that relaxes this: the rule is code in the approved revision, and a
merged change cannot alter the approved revision.

The comparison is between the two trees, not commit by commit: what matters is
the tree the Mac would run next, and a change that was merged and reverted
inside the range never runs here.

Nothing from the unapproved revision is executed. Its files are read with
``git show`` as data, and everything of it that is shown to the owner goes
through ``common.safe_text`` first.

Exit codes: see ``common.py``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

import bump
import common

STEP = "promote"
RESULT_PROMOTED = "promoted"
RESULT_UP_TO_DATE = "up-to-date"
# Every outcome this step reports, with the exit codes it may carry. The
# README table is pinned to this by a test.
OUTCOMES: dict[str, tuple[int, ...]] = {
    "no-approved-revision": (common.EXIT_USAGE,),
    "git-failed": (common.EXIT_FAILED,),
    RESULT_UP_TO_DATE: (common.EXIT_OK,),
    "diverged": (common.EXIT_NEEDS_OWNER,),
    RESULT_PROMOTED: (common.EXIT_OK,),
    "awaiting-promotion": (common.EXIT_OK, common.EXIT_NEEDS_OWNER),
}

UPDATE_SCRIPT_REL = "scripts/update.sh"
LOCAL_OVERRIDE_REL = ".claude/models.local.json"
# Executed or shipped whatever the updater's lists say: the maintenance code
# and installers, the shared scripts the job runs, the version stamp, the two
# settings files every project receives a merge candidate of, the model
# override file the approved manifest tool reads when it is present, and the
# seed the installer alone ships (`install-project` runs the installer).
ALWAYS_RELEVANT = (
    "scripts",
    "VERSION",
    ".claude/skills",
    ".claude/settings.json",
    LOCAL_OVERRIDE_REL,
    bump.CODEX_CONFIG_REL,
    ".claude/docs/research/.gitkeep",
)
# Files git itself interprets, in any directory. They are not shipped by name,
# but they decide what a checkout writes for the files that are: an attribute
# can re-encode a script, change its line endings, or route it through a
# filter program configured on this machine.
GIT_CONTROL_NAMES = (".gitattributes", ".gitmodules", ".lfsconfig")
SHIPPED_ARRAYS = ("SAFE_DIRS", "SAFE_FILES", "SETTINGS_FILES")
TEMPLATE_SOURCE_RE = re.compile(r"\$\{TEMPLATE_DIR\}/([A-Za-z0-9._/-]+)")
# The updater reads this one from the template by a literal path. Not finding
# it means the scan above no longer understands the updater, so nothing is
# classified as inert.
SOURCE_CANARY = "VERSION"

# A change that has waited this long is the owner's to look at, not noise.
PROMOTION_OVERDUE_DAYS = 7
RELEVANT_SHOWN = 10
COMMITS_SHOWN = 50
STAT_LINES_SHOWN = 60
CONFIRM_WORD = "yes"

REASON_INERT = "inert-paths"
REASON_BUMP = "model-bump"


def parse_bash_array(text: str, name: str) -> tuple[str, ...] | None:
    """Read ``NAME=( "a" "b" )`` written one double-quoted literal per line.

    Returns None when the array is absent or holds anything but plain
    literals: a caller deciding what is safe must not guess at an expansion.
    """
    lines = text.splitlines()
    opening = f"{name}=("
    starts = [index for index, line in enumerate(lines) if line.strip() == opening]
    if len(starts) != 1:
        return None
    entries: list[str] = []
    for line in lines[starts[0] + 1 :]:
        body = line.strip()
        if body == ")":
            return tuple(entries)
        if not body or body.startswith("#"):
            continue
        match = re.fullmatch(r'"([A-Za-z0-9._/-]+)"', body)
        if match is None:
            return None
        entries.append(match.group(1))
    return None


def casefold_path(path: str) -> str:
    """A path as a case- and normalization-insensitive filesystem sees it.
    macOS would check two names differing only in case out onto one file."""
    return unicodedata.normalize("NFC", path).casefold()


def read_approved(paths: common.Paths) -> str | None:
    """The approved SHA, or None when the file is missing or is not one."""
    try:
        text = paths.approved_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if common.SHA_RE.fullmatch(text) else None


def write_approved(paths: common.Paths, sha: str) -> None:
    if not common.SHA_RE.fullmatch(sha):
        raise ValueError(f"not a commit SHA: {sha!r}")
    common.write_text_atomic(paths.approved_file, sha + "\n")


def _advance(ctx: common.Context, target: str) -> None:
    """Record ``target`` as approved, and forget the bump record it used up:
    a record is for one merge and is never good for a second."""
    write_approved(ctx.paths, target)
    slug = bump.manifest_model(ctx.git.show(target, bump.MANIFEST_REL))
    bump.drop_record(ctx.paths, slug)


# --------------------------------------------------------------------------
# What the Mac executes or ships
# --------------------------------------------------------------------------


def shipped_paths(update_script: str | None) -> tuple[str, ...] | None:
    """Every path the Mac executes or ships, read from the approved updater.

    None means the updater could not be understood, and then no range is
    inert: the caller must wait for the owner rather than guess.
    """
    if update_script is None:
        return None
    found = list(ALWAYS_RELEVANT)
    for name in SHIPPED_ARRAYS:
        entries = parse_bash_array(update_script, name)
        if not entries:
            return None
        found.extend(entries)
    sources = TEMPLATE_SOURCE_RE.findall(update_script)
    if SOURCE_CANARY not in sources:
        return None
    found.extend(source.rstrip("/") for source in sources)
    return tuple(sorted(set(found)))


def relevant_paths(changed: list[str], shipped: tuple[str, ...] | None) -> list[str]:
    """The changed paths the Mac executes or ships.

    A path counts when it is a shipped entry, lies below one, or is a parent
    of one (a directory replaced by a file or a link). Names are compared the
    way a case-insensitive filesystem would check them out, and a name that
    is not plain ASCII is never inert.
    """
    if shipped is None:
        return sorted(changed)
    folded = [casefold_path(entry) for entry in shipped]
    relevant: list[str] = []
    for path in changed:
        name = casefold_path(path)
        related = any(
            name == entry
            or name.startswith(entry + "/")
            or entry.startswith(name + "/")
            for entry in folded
        )
        controls_checkout = name.rsplit("/", 1)[-1] in GIT_CONTROL_NAMES
        plain = path.isascii() and path.isprintable()
        if related or controls_checkout or not plain:
            relevant.append(path)
    return sorted(relevant)


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """What the change from ``approved`` to ``target`` amounts to.

    ``relevant`` are the changed paths the Mac executes or ships; ``special``
    are the entries added or changed that are not plain files; ``overlong``
    are the paths added or changed that the Mac's file system cannot hold.
    """

    action: str  # up-to-date | promote | await | diverged | unconfigured | failed
    approved: str | None = None
    target: str | None = None
    reason: str = ""
    relevant: tuple[str, ...] = ()
    commits: int = 0
    oldest: float | None = None
    special: tuple[str, ...] = ()
    overlong: tuple[str, ...] = ()


@dataclass(frozen=True)
class TreeChange:
    """What differs between two trees, for the decision."""

    shipped: tuple[str, ...] | None
    relevant: tuple[str, ...]
    special: tuple[str, ...]
    overlong: tuple[str, ...]


def waiting_range(
    git: common.Git, approved: str, target: str
) -> tuple[int, float | None] | None:
    """How many changes landed on the default branch after ``approved``, and
    when the oldest of them landed. None when git cannot say.

    First parents only: one merge is one change that landed when it was
    merged, however many commits it brought and however old those are.
    """
    span = f"{approved}..{target}"
    stamps = git.text(["log", "--first-parent", "--format=%ct", span])
    if stamps is None:
        return None
    values = [int(line) for line in stamps.split() if line.isdigit()]
    return len(values), float(min(values)) if values else None


def _tree_change(ctx: common.Context, approved: str, target: str) -> TreeChange | None:
    """The change between two trees; None when git cannot compare them.
    Needs no shared history."""
    changed = bump.changed_paths(ctx.git, approved, target)
    special = bump.special_entries(ctx.git, approved, target)
    overlong = bump.overlong_entries(ctx.git, approved, target)
    if changed is None or special is None or overlong is None:
        return None
    shipped = shipped_paths(ctx.git.show(approved, UPDATE_SCRIPT_REL))
    relevant = tuple(relevant_paths(changed, shipped))
    return TreeChange(shipped, relevant, tuple(special), tuple(overlong))


def evaluate(ctx: common.Context) -> Decision:
    """Classify the range from the approved revision to the default branch.
    Reads git, and for a model bump asks GitHub about its pull request;
    changes nothing but a bump record whose pull request was closed."""
    approved = read_approved(ctx.paths)
    if approved is None:
        return Decision("unconfigured", reason="no approved revision recorded")
    target = ctx.git.commit_of(common.MAIN_REF)
    if target is None:
        return Decision("failed", approved, reason=f"cannot read {common.MAIN_REF}")
    if target == approved:
        return Decision("up-to-date", approved, target)

    ancestry = ctx.git.run(["merge-base", "--is-ancestor", approved, target])
    if ancestry.returncode not in (0, 1):
        return Decision("failed", approved, target, reason=ancestry.failure())
    change = _tree_change(ctx, approved, target)
    if change is None:
        return Decision("failed", approved, target, reason="cannot compare the trees")
    relevant, special, overlong = change.relevant, change.special, change.overlong
    if ancestry.returncode == 1:
        why = f"the approved revision is not an ancestor of {common.MAIN_REF}"
        return Decision(
            "diverged",
            approved,
            target,
            why,
            relevant,
            special=special,
            overlong=overlong,
        )

    size = waiting_range(ctx.git, approved, target)
    if size is None:
        return Decision("failed", approved, target, reason="cannot read the range")
    commits, oldest = size

    def decide(action: str, reason: str) -> Decision:
        return Decision(
            action,
            approved,
            target,
            reason,
            relevant,
            commits,
            oldest,
            special,
            overlong,
        )

    if special:
        return decide(
            "await", f"adds or changes what is not a plain file: {special[0]}"
        )
    if overlong:
        return decide(
            "await", f"adds or changes a path too long to check out: {overlong[0]}"
        )
    if not relevant:
        return decide("promote", REASON_INERT)
    if change.shipped is None:
        return decide(
            "await", f"cannot read the shipped lists from {UPDATE_SCRIPT_REL}"
        )
    if set(relevant) != bump.BUMP_PATHS:
        return decide("await", "changes what the Mac executes or ships")
    problem = bump.bump_problem_between(ctx.git, approved, target, only=False)
    problem = problem or bump.recorded_bump_problem(ctx, approved, target)
    if problem is not None:
        return decide("await", f"not a model bump this Mac tested: {problem}")
    return decide("promote", REASON_BUMP)


def _result(
    result: str, exit_code: int, detail: str = "", counts: dict[str, int] | None = None
) -> common.StepResult:
    return common.checked_result(OUTCOMES, STEP, result, exit_code, detail, counts)


def auto_promote(ctx: common.Context) -> common.StepResult:
    """The scheduled step: advance the approved revision when the rule allows."""
    decision = evaluate(ctx)
    if decision.action == "unconfigured":
        return _result("no-approved-revision", common.EXIT_USAGE, decision.reason)
    if decision.action == "failed":
        return _result("git-failed", common.EXIT_FAILED, decision.reason)
    if decision.action == "up-to-date":
        return _result(RESULT_UP_TO_DATE, common.EXIT_OK)
    if decision.action == "diverged":
        return _result("diverged", common.EXIT_NEEDS_OWNER, decision.reason)
    if decision.action == "promote" and decision.target is not None:
        # What is recorded is the commit that was just classified, and only
        # while the default branch still names it.
        if ctx.git.commit_of(common.MAIN_REF) != decision.target:
            return _result("git-failed", common.EXIT_FAILED, "the default branch moved")
        _advance(ctx, decision.target)
        moved = (
            f"{common.short_sha(decision.approved)} -> "
            f"{common.short_sha(decision.target)} ({decision.reason})"
        )
        ctx.log.line(f"promote: {moved}")
        return _result(RESULT_PROMOTED, common.EXIT_OK, moved)

    age_days = 0.0
    if decision.oldest is not None:
        age_days = max(ctx.now - decision.oldest, 0.0) / common.SECONDS_PER_DAY
    overdue = age_days > PROMOTION_OVERDUE_DAYS
    listed = [*decision.special, *decision.overlong, *decision.relevant]
    shown = ", ".join(listed[:RELEVANT_SHOWN])
    detail = (
        f"awaiting promotion: {decision.commits} changes, oldest "
        f"{age_days:.1f} days; {decision.reason}; {shown}"
    )
    ctx.log.line(f"promote: {detail}")
    return _result(
        "awaiting-promotion",
        common.EXIT_NEEDS_OWNER if overdue else common.EXIT_OK,
        detail,
        {"commits": decision.commits},
    )


def awaiting_summary(ctx: common.Context) -> tuple[str | None, str | None, int | None]:
    """(approved, main, changes between them) for the status report."""
    approved = read_approved(ctx.paths)
    head = ctx.git.commit_of(common.MAIN_REF)
    if approved is None or head is None:
        return approved, head, None
    size = waiting_range(ctx.git, approved, head)
    return approved, head, None if size is None else size[0]


# --------------------------------------------------------------------------
# The owner's command
# --------------------------------------------------------------------------

Say = Callable[[str], None]


def _show_commits(ctx: common.Context, revisions: str, say: Say) -> None:
    """List commits with a link to each. Records are split at NUL, never at a
    line break a subject could carry. The link is built from the commit's own
    SHA: a subject can claim any pull-request number it likes."""
    limit = ["-n", str(COMMITS_SHOWN)]
    listed = ctx.git.text(["log", "-z", "--format=%H%x09%s", *limit, revisions])
    total = ctx.git.text(["rev-list", "--count", revisions])
    records = [record for record in (listed or "").split("\0") if "\t" in record]
    for record in records:
        sha, subject = record.strip().split("\t", 1)
        say(f"  {common.short_sha(sha)} {subject}")
        say(f"    {common.FORK_WEB_URL}/commit/{sha}")
    count = int(total) if total is not None and total.isdigit() else len(records)
    if count > len(records):
        say(f"  ({count - len(records)} more commits are not listed)")


def _show_change(ctx: common.Context, decision: Decision, say: Say) -> None:
    """What differs between the approved tree and the target: the changed
    paths, anything that is not a plain file, and what the Mac would execute
    or ship of it. Works for any two commits, related or not."""
    approved, target = str(decision.approved), str(decision.target)
    stat = ctx.git.text(["diff", "--stat=100", approved, target]) or "(no diff stat)"
    lines = stat.split("\n")
    for line in lines[:STAT_LINES_SHOWN]:
        say(line)
    if len(lines) > STAT_LINES_SHOWN:
        say(f"  ({len(lines) - STAT_LINES_SHOWN} more lines are not shown)")
    say("")
    say(f"Inspect: git -C {ctx.paths.clone} diff {approved} {target}")
    say("")
    for entry in decision.special:
        say(f"  NOT A PLAIN FILE (a link or another repository): {entry}")
    for entry in decision.overlong:
        say(f"  PATH TOO LONG TO CHECK OUT ON THIS MAC: {entry}")
    if decision.action == "promote":
        say(f"The next scheduled run would promote this by itself ({decision.reason}).")
        return
    say(f"Not promoted automatically: {decision.reason}")
    for path in decision.relevant:
        say(f"  executed or shipped: {path}")


def manual_promote(ctx: common.Context, ask: Callable[[str], str], out: Say) -> int:
    """The owner's command: show the change, ask for a typed word, advance."""

    def say(text: str) -> None:
        out(common.safe_text(text))

    decision = evaluate(ctx)
    if decision.action in ("unconfigured", "failed"):
        say(f"promote: {decision.reason}")
        unconfigured = decision.action == "unconfigured"
        return common.EXIT_USAGE if unconfigured else common.EXIT_FAILED
    approved, target = decision.approved, decision.target
    if decision.action == "up-to-date" or approved is None or target is None:
        say(f"promote: {common.short_sha(approved)} is already the default branch")
        return common.EXIT_OK
    if decision.action == "diverged":
        say(f"WARNING: {decision.reason}.")
        say("The history of the default branch was rewritten, or the approved")
        say("revision was never on it. Its newest commits, then everything that")
        say("differs between the approved tree and it:")
        _show_commits(ctx, target, say)
    else:
        say(f"Commits in {common.short_sha(approved)}..{common.short_sha(target)}:")
        _show_commits(ctx, f"{approved}..{target}", say)
    say("")
    _show_change(ctx, decision, say)
    say("")
    answer = ask(
        f"Type {CONFIRM_WORD} to let this Mac execute and distribute {target}: "
    )
    if answer.strip() != CONFIRM_WORD:
        say("promote: not confirmed; the approved revision is unchanged")
        return common.EXIT_OK
    if ctx.git.commit_of(common.MAIN_REF) != target:
        say("promote: the default branch moved while you were reading; run it again")
        return common.EXIT_FAILED
    _advance(ctx, target)
    owner = f"{common.short_sha(approved)} -> {common.short_sha(target)} (owner)"
    ctx.log.line(f"promote: {owner}")
    say(f"promote: approved revision is now {target}")
    say("The next scheduled run executes it; `launchd.sh run-now` runs one now.")
    return common.EXIT_OK
