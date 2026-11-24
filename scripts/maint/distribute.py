#!/usr/bin/env python3
"""Bring the approved revision, and nothing newer, into the installed projects.

The projects are the paths listed in ``targets.txt`` under the state root.
For each one the job has been allowed to maintain, this step works out what
the approved revision's ``scripts/update.sh`` would change and writes exactly
that into the project, as uncommitted changes.

* **Approved only.** The source is a scratch worktree created from the SHA in
  ``state/approved-rev``. The updater clones that worktree, and what it
  produced is compared with the approved tree before any of it is written.
  The default branch is never read here.
* **Clean only.** A project with a staged, unstaged or untracked-and-not-
  ignored change, or with an index entry git was told not to look at, is left
  alone and looked at again on the next run. Since an update is itself left
  uncommitted, a project that tracks the template waits until its owner
  commits: that is the state ``waiting-dirty``, reported with the revision
  that waits and since when. It is not an error, until it has lasted
  ``WAITING_OVERDUE_DAYS``. A project that is ``busy`` (git in the middle of
  something, a session writing there) waits by the same clock.
* **Never a commit.** The updater runs on a private copy of the paths it
  manages (``staging.py``). The difference between that copy before and after
  is carried into the project with plain file operations
  (``targets.apply_plan``). No git command that writes ever runs in a
  project, and ``HEAD``, the refs and the index are compared before and after.
* **Adopted first.** A project is touched only after the owner ran ``adopt``
  for it at a terminal, saw every path that would change, and typed ``yes``.
  The adoption is recorded outside the project (``ledger.py``), with the file
  system's own numbers for the directory and its repository.
* **Its own output only.** A scheduled run replaces template-owned files only
  while they are byte for byte what the job left there: the record keeps a
  digest of every byte, the rendered ``model:`` lines included. Beyond them
  it creates a missing seed file, and changes a settings file only when the
  documents before and after differ in the manifest's model keys and in
  nothing else (:func:`only_model_keys_differ`). Anything more (a migration,
  a deletion outside the template's paths, a local edit) goes back to
  ``adopt``.

Nothing of a project is executed: see ``target_git.py`` and ``staging.py``.

Outcomes and exit codes: ``OUTCOMES`` and ``TARGET_STATES`` below, explained
in ``scripts/maint/README.md``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

import common
import ledger
import promote
import staging
import target_git
import targets
from ledger import Adoption, Visit

STEP = "distribute"
# Every outcome this step reports, with the exit codes it may carry. The
# README table is pinned to this by a test.
OUTCOMES: dict[str, tuple[int, ...]] = {
    "no-approved-revision": (common.EXIT_USAGE,),
    "no-targets": (common.EXIT_OK,),
    "none-adopted": (common.EXIT_OK,),
    "up-to-date": (common.EXIT_OK,),
    "distributed": (common.EXIT_OK,),
    "waiting": (common.EXIT_OK,),
    "needs-owner": (common.EXIT_NEEDS_OWNER,),
    "target-failed": (common.EXIT_FAILED,),
    "source-failed": (common.EXIT_FAILED,),
}
UP_TO_DATE, UNCOMMITTED, UPDATED = "up-to-date", "uncommitted", "updated"
WAITING_DIRTY, BUSY = "waiting-dirty", "busy"
NOT_ADOPTED, LOCAL_CHANGES, REFUSED, FAILED = (
    "not-adopted",
    "local-changes",
    "refused",
    "failed",
)
# What one project can be found in, with the exit codes it stands for. These
# words are the step's counts, and the second README table is pinned to them.
TARGET_STATES: dict[str, tuple[int, ...]] = {
    UP_TO_DATE: (common.EXIT_OK,),
    UNCOMMITTED: (common.EXIT_OK,),
    UPDATED: (common.EXIT_OK,),
    WAITING_DIRTY: (common.EXIT_OK, common.EXIT_NEEDS_OWNER),
    BUSY: (common.EXIT_OK, common.EXIT_NEEDS_OWNER),
    NOT_ADOPTED: (common.EXIT_OK,),
    LOCAL_CHANGES: (common.EXIT_NEEDS_OWNER,),
    REFUSED: (common.EXIT_NEEDS_OWNER,),
    FAILED: (common.EXIT_FAILED,),
}
COUNT_TARGETS, COUNT_OVERDUE = "targets", "overdue"
LEDGER_FILE = ledger.LEDGER_FILE
# A revision that has waited this long for a project is the owner's to look
# at, by the same measure promotion uses for a change that waits.
WAITING_OVERDUE_DAYS = promote.PROMOTION_OVERDUE_DAYS

# The values the manifest tool owns in a project's settings files. A
# scheduled run lets a settings file change in these and in nothing else; a
# test holds the names to the tool's own.
JSON_MODEL_KEYS = ("model",)
JSON_MODEL_ENV_KEYS = ("CLAUDE_CODE_SUBAGENT_MODEL", "CODEX_MODEL")
TOML_MODEL_KEYS = ("model", "model_reasoning_effort")
TOML_MODEL_NAMES = "|".join(TOML_MODEL_KEYS)
# A line ends at a line feed and nowhere else: a form feed or a Unicode line
# separator is legal inside a string and must not start a "line" here.
TOML_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")
TOML_TABLE_RE = re.compile(r"[ \t]*\[")
# The one spelling of a model line the manifest tool edits, and so the only
# one that is left out of the comparison.
TOML_MODEL_LINE_RE = re.compile(
    rf'({TOML_MODEL_NAMES})[ \t]*=[ \t]*"[^"\\]*"[ \t]*(#.*)?'
)
# Any other way a line can name a model key: quoted, dotted, indented.
TOML_LOOSE_MODEL_RE = re.compile(rf"""\s*["']?({TOML_MODEL_NAMES})["']?\s*[.=]""")
# One name of a key: bare, a basic string, or a literal string.
TOML_NAME = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')"""
# A whole line that is a table header, with its first name. A line that only
# starts like one (inside a string further down) names no table.
TOML_HEADER_RE = re.compile(
    rf"[ \t]*\[\[?[ \t]*(?P<first>{TOML_NAME})(?:[ \t]*\.[ \t]*{TOML_NAME})*"
    r"[ \t]*\]\]?[ \t]*(#.*)?"
)
TOML_KEY_NOISE_RE = re.compile(r"""[\s"']""")
TOML_MULTILINE_QUOTES = ('"""', "'''")
JSON_SUFFIX, TOML_SUFFIX = ".json", ".toml"

HALF_WRITTEN_HELP = (
    "discard or commit the half-written changes there (git status lists "
    "them), then run launchd.sh adopt"
)
ADOPT_HINT = "run launchd.sh adopt for it"
NOT_LEFT_WHY = "template-owned files are not what the job left there"
JSON_NUMBER = "number"
MOVED_WHY = "the project changed while the update was being prepared"
# What to do about a wait that has lasted too long, by what the wait is for.
DIRTY_ADVICE = "commit or clean the tree"
MARKER_ADVICE = "finish or abort what git is doing there, or delete a stale .git/{}"
SESSION_ADVICE = "it is never quiet when the job runs; run launchd.sh adopt for it"
MOVED_ADVICE = "something keeps changing it; run launchd.sh adopt for it"
COUNT_UNSANDBOXED = "unsandboxed"

CREATE, CHANGE, DELETE = staging.CREATE, staging.CHANGE, staging.DELETE


# --------------------------------------------------------------------------
# What a scheduled run may change by itself
# --------------------------------------------------------------------------


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object, refusing a key that is there twice. The usual parser
    keeps the last of them, so the others could be rewritten unseen."""
    found: dict[str, object] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError(f"duplicate key {key!r}")
        found = {**found, key: value}
    return found


def _as_written(text: str) -> tuple[str, str]:
    """A JSON number as the document spells it. Parsed, ``1`` equals ``true``
    and two long fractions round to one float; spelled, neither does."""
    return JSON_NUMBER, text


def _json_without_models(text: str) -> object:
    """A settings document with the manifest's model values taken out; None
    when it is not a JSON object, or holds a key twice at any depth. Numbers
    stay as they are written."""
    try:
        data = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_int=_as_written,
            parse_float=_as_written,
            parse_constant=_as_written,
        )
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kept = {key: value for key, value in data.items() if key not in JSON_MODEL_KEYS}
    env = kept.get("env")
    if isinstance(env, dict):
        rest = {k: v for k, v in env.items() if k not in JSON_MODEL_ENV_KEYS}
        kept = {key: value for key, value in kept.items() if key != "env"}
        kept = {**kept, "env": rest} if rest else kept
    return kept


def _toml_names_a_model_table(body: str) -> bool:
    """Whether a table header's first name is a model key, or cannot be
    compared with one (a quoted name with an escape in it)."""
    header = TOML_HEADER_RE.fullmatch(body)
    if header is None:
        return False
    first = header.group("first")
    if first.startswith('"') and "\\" in first:
        return True
    return first.strip("\"'") in TOML_MODEL_KEYS


def _toml_without_models(text: str) -> str | None:
    """A configuration file without its top-level model lines; every other
    byte stays, so any other difference shows.

    None when a line cannot be told from a model line with certainty, by the
    manifest tool's own rules and without a parser (Python 3.9 has none): a
    multi-line string before the first table, which could hold a line that
    only looks like one; a top-level key that is there twice, or spelled with
    an escape, which could hide that it is; a model key spelled any other way
    than ``key = "value"``; or a later table that carries a model key's name.
    """
    kept: list[str] = []
    seen: set[str] = set()
    top_level = True
    for line in TOML_LINE_RE.findall(text):
        body = line.rstrip("\r\n")
        if TOML_TABLE_RE.match(body):
            top_level = False
            if _toml_names_a_model_table(body):
                return None
        if not top_level or not body.strip() or body.lstrip().startswith("#"):
            kept.append(line)
            continue
        if any(quote in body for quote in TOML_MULTILINE_QUOTES):
            return None
        if "=" in body:
            name = body.split("=", 1)[0]
            key = TOML_KEY_NOISE_RE.sub("", name)
            if key in seen or "\\" in name:
                return None
            seen.add(key)
        if TOML_MODEL_LINE_RE.fullmatch(body):
            continue
        if TOML_LOOSE_MODEL_RE.match(body):
            return None
        kept.append(line)
    return "".join(kept)


def only_model_keys_differ(rel: str, old: str, new: str) -> bool:
    """Whether two versions of a settings file differ in the manifest's model
    keys and in nothing else. False for a file of a kind not known here, and
    for one that cannot be read with certainty: not parsable, or holding a
    key twice."""
    if rel.endswith(JSON_SUFFIX):
        before, after = _json_without_models(old), _json_without_models(new)
        return before is not None and before == after
    if rel.endswith(TOML_SUFFIX):
        was, now = _toml_without_models(old), _toml_without_models(new)
        return was is not None and was == now
    return False


def _settings_change_is_models_only(
    staged: staging.Staged, target: str, rel: str
) -> bool:
    try:
        old = Path(target, rel).read_text(encoding="utf-8")
        new = Path(staged.stage, rel).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return only_model_keys_differ(rel, old, new)


def unattended_problem(
    source: staging.Source, staged: staging.Staged, target: str
) -> str | None:
    """The first change a scheduled run must not make by itself; None when
    every change is to the template's own paths, to the model keys of a
    settings file, or the creation of a missing seed."""
    shipped = source.shipped
    owned = {*shipped.files, staging.VERSION_STAMP_REL}
    for what, rel in staged.changes():
        inside = any(rel.startswith(f"{top}/") for top in shipped.dirs)
        seeded = rel in staging.SEED_FILES and what == CREATE
        if inside or rel in owned or seeded:
            continue
        if rel in shipped.settings and what == CHANGE:
            if _settings_change_is_models_only(staged, target, rel):
                continue
            return f"the update would change more than the model keys of {rel}"
        return f"the update would also {what} {rel}"
    return None


# --------------------------------------------------------------------------
# One project
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    """What git said about a project when the decision to write was taken:
    the digest of ``HEAD``, refs and index, and what kept the tree from being
    clean. Both are asked again right before the first write."""

    state: str
    dirt: target_git.Dirt


def write_update(
    ctx: common.Context,
    source: staging.Source,
    book: ledger.Ledger,
    git: target_git.TargetGit,
    adoption: Adoption,
    staged: staging.Staged,
    seen: Seen,
) -> Visit:
    """Carry a prepared update into the project and record it.

    The whole change is planned and proven possible first; then git is asked
    once more whether the project is still what ``seen`` describes. Only then
    is the record marked ``interrupted``, together with the name of every
    temporary file the write is going to create, and the first change made.
    From that point on nothing reports "not written": the mark stays until
    every path and the repository's own state have been checked.

    What the record keeps of the project afterwards is a digest of every byte
    the job wrote, taken from the private copy after the manifest tool
    rendered the model lines, and only once every path in the project has
    been compared with that copy. A model bump the job itself wrote is its
    own output on the next run; an edit saved in the project at any moment,
    even right after the comparison, is not in the copy and stays the owner's.
    """
    target = git.target
    try:
        plan = targets.plan_changes(target, staged.stage, staged.before, staged.after)
    except targets.Conflict as exc:
        return Visit(target, FAILED, f"not written: {exc}")
    if target_git.git_state(git) != seen.state or target_git.dirt(git) != seen.dirt:
        return Visit(target, BUSY, f"{MOVED_WHY}; nothing was written")
    changed = len(staged.changes())
    recorded = (*adoption.temporaries, *plan.temporaries)
    marked = replace(adoption, interrupted=source.revision, temporaries=recorded)
    book.put(target, marked)
    try:
        targets.apply_plan(plan)
        problem = targets.applied_problem(target, staged.before, staged.after)
    except (targets.Interrupted, OSError) as exc:
        ctx.log.line(f"distribute: stopped while writing: {exc}")
        problem = "the write stopped part-way"
    if problem is None and target_git.git_state(git) != seen.state:
        problem = "the repository's HEAD, refs or index changed during the update"
    if problem is not None:
        return Visit(target, FAILED, f"written in part: {problem}; {HALF_WRITTEN_HELP}")
    done = replace(
        adoption,
        revision=source.revision,
        source_print=source.fingerprint,
        target_print=staged.after_print,
        updated_at=ctx.now,
        waiting_revision="",
        waiting_since=0.0,
        interrupted="",
    )
    book.put(target, done)
    if not changed:
        return Visit(target, UP_TO_DATE, "nothing to write")
    return Visit(target, UPDATED, f"{changed} paths changed, left uncommitted")


def _held(
    ctx: common.Context,
    source: staging.Source,
    book: ledger.Ledger,
    target: str,
    adoption: Adoption,
    found: tuple[str, str, str],
) -> Visit:
    """A revision waits for this project: ``found`` is the state, why, and
    what to do once it has lasted too long. The wait is counted from the
    first revision that had to wait, not from the newest, and by one clock
    whatever the project was waiting for on each run: a tree that is not
    clean, git in the middle of something, a session writing there."""
    state, detail, advice = found
    since = adoption.waiting_since if adoption.waiting_revision else ctx.now
    waiting = replace(adoption, waiting_revision=source.revision, waiting_since=since)
    if waiting != adoption:
        book.put(target, waiting)
    days = max(ctx.now - since, 0.0) / common.SECONDS_PER_DAY
    overdue = days > WAITING_OVERDUE_DAYS
    if overdue:
        detail = f"waiting {days:.1f} days: {advice}; {detail}"
    return Visit(target, state, detail, source.revision, since, overdue)


def _dirty(dirt: target_git.Dirt) -> tuple[str, str, str]:
    """What a tree that is not clean is reported as."""
    first = [*dirt.visible, *dirt.hidden][0]
    detail = f"{dirt.summary()}; first: {target_git.shown(first)}"
    return WAITING_DIRTY, detail, DIRTY_ADVICE


def visit_target(
    ctx: common.Context,
    source: staging.Source,
    book: ledger.Ledger,
    target: str,
    name: str,
) -> Visit:
    """Decide what an adopted project needs, and do it when every guard
    holds. Each early return leaves the project exactly as it was found (the
    temporary files a killed write of the job's own left there apart, and of
    those only the ones whose paths were recorded before they existed)."""
    adoption = book.adoptions[target]
    problem = targets.path_problem(target, ctx.paths)
    if problem is not None:
        return Visit(target, REFUSED, problem)
    if tuple(targets.identity(target)) != adoption.identity:
        why = "another directory or repository is at this path now; adopt it again"
        return Visit(target, REFUSED, why)
    git = target_git.TargetGit(ctx, target)
    problem = target_git.repository_problem(git)
    problem = problem or targets.layout_problem(
        target, source.guarded, staging.MADE_DIRS
    )
    if problem is not None:
        return Visit(target, REFUSED, problem)
    if adoption.temporaries:
        removed, left = targets.remove_temporaries(target, adoption.temporaries)
        ctx.log.line(
            f"distribute: removed {removed} temporary files of its own; "
            f"{len(left)} could not be removed and stay recorded"
        )
        adoption = replace(adoption, temporaries=left)
        book.put(target, adoption)
    if adoption.interrupted:
        short = common.short_sha(adoption.interrupted)
        why = f"an update to {short} stopped half-way: {HALF_WRITTEN_HELP}"
        return Visit(target, LOCAL_CHANGES, why)
    state, dirt = target_git.git_state(git), target_git.dirt(git)
    if state is None or dirt is None:
        return Visit(target, FAILED, "git could not report on the project")

    have = source.left_print_of(target)
    if adoption.source_print == source.fingerprint:
        # Nothing new to write: the job last wrote this very template here
        # (the approved SHA may still have moved, by a promotion that ships
        # nothing new). What the owner did with it since is the owner's.
        settled = replace(
            adoption, revision=source.revision, waiting_revision="", waiting_since=0.0
        )
        if settled != adoption:
            book.put(target, settled)
        differs = have != adoption.target_print
        note = "template files differ from what the job left" if differs else ""
        return Visit(target, UNCOMMITTED if dirt else UP_TO_DATE, note)
    if dirt:
        return _held(ctx, source, book, target, adoption, _dirty(dirt))
    marker = targets.operation_in_progress(target)
    if marker is not None:
        why = f"git is in the middle of something (.git/{marker})"
        found = (BUSY, why, MARKER_ADVICE.format(marker))
        return _held(ctx, source, book, target, adoption, found)
    if targets.recent_session(target, ctx.env.get("HOME", ""), ctx.now):
        why = "a Claude Code session wrote here within the hour"
        return _held(ctx, source, book, target, adoption, (BUSY, why, SESSION_ADVICE))
    if have != adoption.target_print:
        return Visit(target, LOCAL_CHANGES, f"{NOT_LEFT_WHY}; {ADOPT_HINT}")
    try:
        staged = staging.stage_update(ctx, source, target, name)
    except staging.StageProblem as exc:
        return Visit(target, REFUSED if exc.refused else FAILED, exc.detail)
    if staged.before_print != adoption.target_print:
        # Saved between the look above and the copy: the copy is what the
        # update is prepared from, so the copy is what has to be the job's.
        return Visit(target, LOCAL_CHANGES, f"{NOT_LEFT_WHY}; {ADOPT_HINT}")
    problem = unattended_problem(source, staged, target)
    if problem is not None:
        return Visit(target, LOCAL_CHANGES, f"{problem}; {ADOPT_HINT}")
    visit = write_update(ctx, source, book, git, adoption, staged, Seen(state, dirt))
    if visit.state == BUSY:
        found = (BUSY, visit.detail, MOVED_ADVICE)
        return _held(ctx, source, book, target, adoption, found)
    return visit


def _guarded_visit(
    ctx: common.Context,
    source: staging.Source,
    book: ledger.Ledger,
    target: str,
    name: str,
) -> Visit:
    """One project failing, however it fails, is that project's result."""
    try:
        return visit_target(ctx, source, book, target, name)
    except Exception as exc:
        ctx.log.line(f"distribute: {type(exc).__name__}: {exc}")
        return Visit(target, FAILED, f"stopped by {type(exc).__name__}")


# --------------------------------------------------------------------------
# The scheduled step
# --------------------------------------------------------------------------


def registry_path(paths: common.Paths) -> Path:
    return paths.state_root / targets.REGISTRY_FILE


def exit_code(visit: Visit) -> int:
    """What one project's finding means for the run."""
    codes = TARGET_STATES[visit.state]
    return codes[-1] if visit.overdue else codes[0]


def _result(
    result: str, code: int, detail: str = "", counts: dict[str, int] | None = None
) -> common.StepResult:
    return common.checked_result(OUTCOMES, STEP, result, code, detail, counts)


def _aside_note(asides: list[Path]) -> str:
    if not asides:
        return ""
    return (
        f"the record of adoptions could not be read and was set aside as "
        f"{asides[-1].name}: adopt the projects again, then delete that file"
    )


def _unsandboxed_note(unsandboxed: int) -> str:
    """The first words of the step's detail when the updater ran without the
    kernel sandbox: on how many private copies. ``status`` prints the detail,
    and a line that is cut is cut at its end."""
    return f"{COUNT_UNSANDBOXED} {unsandboxed}" if unsandboxed else ""


def _summary(
    visits: list[Visit], asides: list[Path], unsandboxed: int = 0
) -> common.StepResult:
    counts = {COUNT_TARGETS: len(visits)}
    for visit in visits:
        counts = {**counts, visit.state: counts.get(visit.state, 0) + 1}
    overdue = sum(1 for visit in visits if visit.overdue)
    counts = {**counts, COUNT_OVERDUE: overdue} if overdue else counts
    worst = max([exit_code(visit) for visit in visits], default=common.EXIT_OK)
    worst = max(worst, common.EXIT_NEEDS_OWNER) if asides else worst
    if worst == common.EXIT_FAILED:
        result = "target-failed"
    elif worst == common.EXIT_NEEDS_OWNER:
        result = "needs-owner"
    elif counts.get(UPDATED):
        result = "distributed"
    elif counts.get(WAITING_DIRTY) or counts.get(BUSY):
        result = "waiting"
    elif counts.get(NOT_ADOPTED) == len(visits):
        result = "none-adopted"
    else:
        result = "up-to-date"
    lines = [
        f"{os.path.basename(visit.target)}: {visit.state}"
        + (f" ({visit.detail})" if visit.detail else "")
        for visit in visits
    ]
    notes = [_unsandboxed_note(unsandboxed), _aside_note(asides), *lines]
    detail = "; ".join([line for line in notes if line])
    return _result(result, worst, detail, counts)


def run(ctx: common.Context) -> common.StepResult:
    """The scheduled step: visit every listed project once."""
    approved = promote.read_approved(ctx.paths)
    if approved is None:
        why = "no approved revision recorded"
        return _result("no-approved-revision", common.EXIT_USAGE, why)
    try:
        book = ledger.Ledger(ctx.paths)
    except ledger.Unreadable as exc:
        # Never read as "nothing adopted", and never overwritten: the file is
        # kept under another name, and no project is touched in this run.
        aside = ledger.set_aside(ctx.paths, ctx.now)
        ctx.log.line(f"distribute: unreadable record ({exc}); moved to {aside}")
        return _result("needs-owner", common.EXIT_NEEDS_OWNER, _aside_note([aside]))
    asides = ledger.set_aside_files(ctx.paths)
    entries = targets.read_registry(registry_path(ctx.paths))
    if not entries and not asides:
        # No registry, no trace: the record is only emptied when there is one.
        if book.path.exists():
            book.close([], ctx.now)
        hint = f"no project is listed in {registry_path(ctx.paths)}"
        return _result("no-targets", common.EXIT_OK, hint)

    # A project that was not adopted is not looked at, not even its path.
    found: dict[str, Visit] = {}
    for entry in entries:
        if not targets.is_absolute(entry):
            found[entry] = Visit(entry, REFUSED, "is not an absolute path")
        elif entry not in book.adoptions:
            found[entry] = Visit(entry, NOT_ADOPTED, ADOPT_HINT)
    adopted = [entry for entry in entries if entry not in found]
    failure = ""
    unsandboxed = 0
    if adopted:
        try:
            with staging.open_source(ctx, approved) as source:
                for index, entry in enumerate(adopted):
                    name = f"stage-{index}"
                    found[entry] = _guarded_visit(ctx, source, book, entry, name)
                unsandboxed = len(source.unfenced)
        except staging.SourceError as exc:
            failure = str(exc)
    unvisited = Visit("", FAILED, f"not visited: {failure}")
    visits = [found.get(entry, replace(unvisited, target=entry)) for entry in entries]
    book.close(visits, ctx.now)
    for visit in visits:
        ctx.log.line(f"distribute: {visit.state}: {visit.target} {visit.detail}")
    if failure:
        return _result("source-failed", common.EXIT_FAILED, failure)
    return _summary(visits, asides, unsandboxed)
