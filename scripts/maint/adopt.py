#!/usr/bin/env python3
"""The owner's side of distribution: let the job maintain one project.

The scheduled run never touches a project that was not adopted. Adopting is
typed at a terminal (``launchd.sh adopt <project>``), once per project and
again whenever a scheduled run sends a project back (``local-changes``, or a
``refused`` that says so). It is also the migration an old installation needs.

What the screen shows is not a prediction. The approved updater is run on a
private copy of the paths it manages, and the screen lists the difference
between that copy before and after: counts per directory, every path in a
private file, and one line for each file that would be deleted or overwritten
while git could not bring its present content back, because git does not
track it or was told not to look at it. After a typed ``yes`` exactly that
difference is written into the project, and only while the project still is
what the copy was taken from.

The same rules as the scheduled step hold here: the approved revision only, a
clean tree only, nothing committed or staged, nothing of the project executed.

Exit codes: 0 adopted, or declined · 1 not listed, or no approved revision ·
2 the project must be fixed first (not clean, a link, a configuration key),
or it changed while the screen was up · 3 the update could not be prepared or
written.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import common
import distribute
import ledger
import promote
import staging
import target_git
import targets

CONFIRM_WORD = "yes"
SCREEN_WIDTH = common.DISPLAY_LIMIT
GROUPS_SHOWN = 40
PATHS_SHOWN = 30
GROUP_DEPTH = 2
VERSION_STAMP = staging.VERSION_STAMP_REL
TRACKED, UNTRACKED, HIDDEN = "tracked", "untracked", "hidden"

Say = Callable[..., None]
Ask = Callable[[str], str]


def _group(rel: str) -> str:
    return "/".join(rel.split("/")[:GROUP_DEPTH])


def _standing(rel: str, tracked: set[str], hidden: set[str]) -> str:
    """How git stands to a path: it tracks it, it does not, or it tracks it
    and was told not to look at its changes."""
    if rel in hidden:
        return HIDDEN
    return TRACKED if rel in tracked else UNTRACKED


def _show_plan(
    ctx: common.Context,
    staged: staging.Staged,
    tracked: set[str],
    hidden: set[str],
    say: Say,
) -> None:
    """What typing ``yes`` would do: counts per directory, every path in a
    private file, and each file whose present content git could not restore."""
    changes = staged.changes()
    if not changes:
        say("The project already holds exactly what this revision would leave;")
        say("nothing would change. Adopting it only lets scheduled runs keep it so.")
        return
    listing = ctx.paths.logs_dir / f"{common.utc_stamp(ctx.now)}.adopt.log"
    rows = [
        f"{what}\t{_standing(rel, tracked, hidden)}\t"
        f"{common.safe_text(rel, SCREEN_WIDTH)}\n"
        for what, rel in changes
    ]
    common.write_text_atomic(listing, "".join(rows))

    say("Rehearsed on a private copy of the paths the updater manages. Nothing in")
    say("the project has been touched yet. Typing yes would:")
    groups: dict[tuple[str, str], int] = {}
    for what, rel in changes:
        key = (what, _group(rel))
        groups = {**groups, key: groups.get(key, 0) + 1}
    ordered = sorted(groups.items(), key=lambda item: (item[0][1], item[0][0]))
    for (what, group), count in ordered[:GROUPS_SHOWN]:
        say(f"  {what} {count:>5}  {group}")
    if len(ordered) > GROUPS_SHOWN:
        say(f"  ({len(ordered) - GROUPS_SHOWN} more groups are not shown)")
    junk = sum(1 for _, rel in changes if targets.is_junk(rel))
    if junk:
        say(f"  ({junk} of these are bytecode caches or Finder index files)")
    if any(rel.endswith("/") for _, rel in changes):
        say("  (a path ending in / is a directory that holds no file)")
    say(f"Every path, one per line: {listing}")

    lost = [
        (what, rel, _standing(rel, tracked, hidden))
        for what, rel in changes
        if what != staging.CREATE and not rel.endswith("/") and not targets.is_junk(rel)
    ]
    lost = [entry for entry in lost if entry[2] != TRACKED]
    if not lost:
        return
    say()
    say(f"Git cannot bring back the present content of {len(lost)} of the files that")
    say("would be deleted or overwritten: it does not track them, or it was told")
    say("not to look at them (assume-unchanged, skip-worktree):")
    for what, rel, standing in lost[:PATHS_SHOWN]:
        say(f"  {what}  {rel}  ({standing})")
    if len(lost) > PATHS_SHOWN:
        more = len(lost) - PATHS_SHOWN
        say(f"  ({more} more, marked {UNTRACKED} or {HIDDEN} in the file above)")


def _stamp(target: str) -> str:
    """The template version the project says it has."""
    try:
        text = Path(target, VERSION_STAMP).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return "none"
    return text or "none"


def run(ctx: common.Context, raw: str, ask: Ask, out: Callable[[str], None]) -> int:
    """Show what updating one project to the approved revision would change,
    ask for a typed word, update it, and record that scheduled runs may keep
    it updated."""

    def say(text: str = "") -> None:
        out(common.safe_text(text, SCREEN_WIDTH))

    approved = promote.read_approved(ctx.paths)
    if approved is None:
        say("adopt: no approved revision recorded; run launchd.sh install")
        return common.EXIT_USAGE
    target = os.path.normpath(raw) if targets.is_absolute(raw) else raw
    # The project is named once, on its own line: a long path must not push
    # the reason that follows off the end of a line.
    say(f"Project:   {target}")
    registry = distribute.registry_path(ctx.paths)
    if target not in targets.read_registry(registry):
        say("adopt: it is not listed in the registry of projects the job may look at:")
        say(f"  {registry}")
        say("Add its absolute path there, on a line of its own, then run adopt again.")
        return common.EXIT_USAGE
    problem = targets.path_problem(target, ctx.paths)
    if problem is not None:
        say(f"adopt: this path {problem}")
        return common.EXIT_NEEDS_OWNER
    stamp = os.path.join(target, VERSION_STAMP)
    if os.path.islink(stamp) or not os.path.isfile(stamp):
        # Updating is not installing: the installer asks before it replaces a
        # project's own Claude and Codex configuration, the updater does not.
        say(f"adopt: the template is not installed there (no {VERSION_STAMP}).")
        say("Install it first with scripts/install.sh; adopt only updates.")
        return common.EXIT_NEEDS_OWNER
    try:
        book = ledger.Ledger(ctx.paths)
    except ledger.Unreadable as exc:
        aside = ledger.set_aside(ctx.paths, ctx.now)
        ctx.log.line(f"adopt: unreadable record ({exc}); moved to {aside}")
        say("adopt: the record of adoptions could not be read. It was set aside as")
        say(f"  {aside}")
        say("Nothing was changed. Run adopt again, for every project that was")
        say("adopted before; delete that file when you are done.")
        return common.EXIT_NEEDS_OWNER
    git = target_git.TargetGit(ctx, target)
    try:
        with staging.open_source(ctx, approved) as source:
            return _adopt_from(ctx, source, git, book, ask, say)
    except staging.SourceError as exc:
        say(f"adopt: {exc}")
        return common.EXIT_FAILED


def _fixable_problem(source: staging.Source, git: target_git.TargetGit) -> str | None:
    """What the owner has to put right in the project before it can be
    adopted; None when nothing."""
    target = git.target
    problem = target_git.repository_problem(git)
    problem = problem or targets.layout_problem(
        target, source.guarded, staging.MADE_DIRS
    )
    if problem is not None:
        return f"the project {problem}"
    marker = targets.operation_in_progress(target)
    if marker is not None:
        return f"git is in the middle of something there ({marker}); finish it first"
    return None


def _adopt_from(
    ctx: common.Context,
    source: staging.Source,
    git: target_git.TargetGit,
    book: ledger.Ledger,
    ask: Ask,
    say: Say,
) -> int:
    target = git.target
    problem = _fixable_problem(source, git)
    if problem is not None:
        say(f"adopt: {problem}")
        return common.EXIT_NEEDS_OWNER
    state, dirt = target_git.git_state(git), target_git.dirt(git)
    index = target_git.index_paths(git)
    if state is None or dirt is None or index is None:
        say("adopt: git could not report on the project")
        return common.EXIT_FAILED
    if dirt.visible:
        say(f"adopt: the project has {len(dirt.visible)} uncommitted paths:")
        for entry in dirt.visible[:PATHS_SHOWN]:
            say(f"  {target_git.shown(entry)}")
        say("An update is only ever written into a clean tree, and it is left")
        say("uncommitted. Commit or clean these first, then run adopt again.")
        return common.EXIT_NEEDS_OWNER
    try:
        staged = staging.stage_update(ctx, source, target, "stage")
    except staging.StageProblem as exc:
        if exc.refused:
            say(f"adopt: the project {exc.detail}")
            return common.EXIT_NEEDS_OWNER
        say(f"adopt: {exc.detail}; see the newest file in {ctx.paths.logs_dir}")
        return common.EXIT_FAILED

    say(f"Revision:  {source.revision} (template {source.version})")
    say(f"Installed: template {_stamp(target)}")
    say(f"Tree:      clean, at {common.short_sha(git.text(['rev-parse', 'HEAD']))}")
    say()
    _show_plan(ctx, staged, index[0], index[1], say)
    say()
    if dirt.hidden:
        say(
            f"{len(dirt.hidden)} paths here are hidden from git status (assume-unchanged"
        )
        say(
            "or skip-worktree). Scheduled runs treat the tree as not clean while they are."
        )
    say("Nothing is committed, staged or stashed. The result stays as uncommitted")
    say("changes for you to review and commit; until you do, the revisions that")
    say("follow wait for this project.")
    short = common.short_sha(source.revision)
    answer = ask(
        f"Type {CONFIRM_WORD} to update this project to {short} and let scheduled "
        "runs keep it updated: "
    )
    if answer.strip() != CONFIRM_WORD:
        say("adopt: not confirmed; nothing was changed")
        return common.EXIT_OK
    # The screen was true when it was drawn. The approved revision can have
    # moved since; whether the project did is asked again by the write itself.
    if promote.read_approved(ctx.paths) != source.revision:
        say("adopt: the approved revision moved while you were reading; run it again")
        return common.EXIT_FAILED
    # A new record, but not a forgetful one: the temporary files an earlier
    # write was killed over stay recorded, so the next scheduled run can
    # still remove them. Nothing is deleted here that the screen did not
    # show, and nothing is carried over to another directory or repository
    # that is at this path now.
    identity = tuple(targets.identity(target))
    earlier = book.adoptions.get(target)
    carried: tuple[str, ...] = ()
    if earlier is not None and earlier.identity == identity:
        carried = earlier.temporaries
    adoption = ledger.Adoption(
        identity=identity,
        adopted_at=ctx.now,
        revision=source.revision,
        source_print=source.fingerprint,
        target_print="",
        updated_at=ctx.now,
        temporaries=carried,
    )
    seen = distribute.Seen(state, dirt)
    visit = distribute.write_update(ctx, source, book, git, adoption, staged, seen)
    ctx.log.line(f"adopt: {visit.state}: {target} {visit.detail}")
    if visit.state == distribute.BUSY:
        say(f"adopt: {visit.detail}. Run it again.")
        return common.EXIT_NEEDS_OWNER
    if visit.state == distribute.FAILED:
        say(f"adopt: {visit.detail}")
        return common.EXIT_FAILED
    say(f"adopt: the project now holds {source.revision} ({visit.detail}).")
    say("Review and commit when ready.")
    return common.EXIT_OK
