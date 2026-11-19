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
  ignored change is left alone and looked at again on the next run. Since an
  update is itself left uncommitted, a project that tracks the template waits
  until its owner commits: that is the state ``waiting-dirty``, reported with
  the revision that waits and since when. It is not an error.
* **Never a commit.** The updater runs on a private copy of the paths it
  manages (``run/distribute-<pid>/``), inside a scratch repository of the
  job's own. The difference between that copy before and after is carried
  into the project with plain file operations (``targets.apply_changes``).
  No git command that writes ever runs in a project, and ``HEAD``, the refs
  and the index are compared before and after.
* **Adopted first.** A project is touched only after the owner ran ``adopt``
  for it at a terminal, saw every path that would change, and typed ``yes``.
  The adoption is recorded outside the project, with the file system's own
  numbers for the directory and its repository.
* **Its own output only.** A scheduled run replaces template-owned files only
  while they are byte for byte what the job left there, and changes nothing
  else but the model keys of the two settings files. Anything more (a
  migration, a deletion outside the template's paths, a local edit) goes back
  to ``adopt``.

Nothing of a project is executed: see ``targets.py``. The only code that runs
is the approved revision's ``scripts/update.sh`` and what it calls, on the
private copy.

Outcomes and exit codes: ``OUTCOMES`` and ``TARGET_STATES`` below, explained
in ``scripts/maint/README.md``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import common
import promote
import targets

STEP = "distribute"
# Every outcome this step reports, with the exit codes it may carry. The
# README table is pinned to this by a test.
OUTCOMES: dict[str, tuple[int, ...]] = {
    "no-approved-revision": (common.EXIT_USAGE,),
    "no-targets": (common.EXIT_OK,),
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
# What one project can be found in, with the exit code it stands for. These
# words are the step's counts, and the second README table is pinned to them.
TARGET_STATES: dict[str, tuple[int, ...]] = {
    UP_TO_DATE: (common.EXIT_OK,),
    UNCOMMITTED: (common.EXIT_OK,),
    UPDATED: (common.EXIT_OK,),
    WAITING_DIRTY: (common.EXIT_OK,),
    BUSY: (common.EXIT_OK,),
    NOT_ADOPTED: (common.EXIT_NEEDS_OWNER,),
    LOCAL_CHANGES: (common.EXIT_NEEDS_OWNER,),
    REFUSED: (common.EXIT_NEEDS_OWNER,),
    FAILED: (common.EXIT_FAILED,),
}
COUNT_TARGETS = "targets"

LEDGER_FILE = "distribution.json"
WORK_PREFIX = "distribute-"
SOURCE_DIR = "source"
SCRATCH_TMP_DIR = "tmp"
EMPTY_DIR = "empty"
BASH = "/bin/bash"
TEMPLATE_ENV = "ORCHESTRA_TEMPLATE_REPO"
UPDATER_REL = promote.UPDATE_SCRIPT_REL
MODELS_REL = ".claude/skills/_shared/models.py"
VERSION_REL = "VERSION"
VERSION_STAMP_REL = ".claude/orchestra-version"
# Project-owned files the updater creates when they are missing.
SEED_FILES = (".claude/STATE.md", ".claude/docs/DESIGN.md")
PROJECT_FILES = (*SEED_FILES, VERSION_STAMP_REL, ".claude/models.local.json")
# What only an old installation still has; the updater moves or retires it.
LEGACY_ROOT, CANONICAL_ROOT = ".agents", ".claude"
LEGACY_FILES = (".agents/STATE.md", "CLAUDE.md", "CLAUDE.md.pre-agents-md.bak")
UPDATER_ARRAYS = (
    "SAFE_DIRS",
    "SAFE_FILES",
    "SETTINGS_FILES",
    "DEPRECATED_PATHS",
    "LEGACY_PROJECT_DIRS",
)
UPDATER_TIMEOUT_SEC = 600
MODELS_TIMEOUT_SEC = 120

CREATE, CHANGE, DELETE = "create", "change", "delete"


class SourceError(Exception):
    """The approved revision could not be laid out to distribute from."""


class StageProblem(Exception):
    """The update could not be prepared for one project."""

    def __init__(self, state: str, detail: str) -> None:
        super().__init__(detail)
        self.state = state
        self.detail = detail


# --------------------------------------------------------------------------
# The approved revision, laid out once per run
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Shipped:
    """The path lists of the approved updater."""

    dirs: tuple[str, ...]
    files: tuple[str, ...]
    settings: tuple[str, ...]
    deprecated: tuple[str, ...]
    legacy: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    """A scratch checkout of the approved revision. ``files`` are the shipped
    files it really has; ``fingerprint`` is what a project holds when it has
    exactly this revision's template."""

    root: Path
    work: Path
    revision: str
    shipped: Shipped
    files: tuple[str, ...]
    fingerprint: str
    version: str

    @property
    def guarded(self) -> tuple[str, ...]:
        """Paths that must be plain directory chains inside a project."""
        shipped = self.shipped
        return (*shipped.dirs, *shipped.files, *shipped.settings, *PROJECT_FILES)

    def fingerprint_of(self, root: str) -> str:
        return targets.fingerprint(
            root, self.shipped.dirs, self.files, VERSION_STAMP_REL
        )


def _plain(rel: str) -> bool:
    parts = rel.split("/")
    return not rel.startswith("/") and all(
        part not in ("", ".", "..") for part in parts
    )


def read_shipped(updater: str) -> Shipped | None:
    """The updater's five lists, or None when one cannot be read as plain
    relative paths. The step does not guess at what an updater writes."""
    lists = [promote.parse_bash_array(updater, name) for name in UPDATER_ARRAYS]
    if any(entries is None for entries in lists):
        return None
    found = [tuple(entries or ()) for entries in lists]
    if not all(found[:3]) or not all(_plain(rel) for group in found for rel in group):
        return None
    return Shipped(*found)


def _force_remove(work: Path) -> None:
    """Delete a scratch directory. A copied directory keeps the mode it had
    in the project, so each one is made writable on the way down first."""
    for directory, dirnames, _ in os.walk(str(work)):
        for name in dirnames:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRWXU)
    shutil.rmtree(str(work))


def _remove_work(ctx: common.Context, work: Path) -> None:
    tree = common.require_scratch(ctx.paths, work / SOURCE_DIR)
    if tree.exists():
        ctx.git.run(["worktree", "remove", "--force", str(tree)])
    if work.exists():
        _force_remove(common.require_scratch(ctx.paths, work))
    ctx.git.run(["worktree", "prune"])


def _read_source(ctx: common.Context, work: Path, approved: str) -> Source:
    tree = work / SOURCE_DIR
    if ctx.git.text(["rev-parse", "--verify", "HEAD^{commit}"], cwd=tree) != approved:
        raise SourceError("the scratch checkout is not the approved revision")
    try:
        updater = (tree / UPDATER_REL).read_text(encoding="utf-8")
        version = (tree / VERSION_REL).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        raise SourceError("the approved revision has no updater or version") from None
    shipped = read_shipped(updater)
    if shipped is None:
        raise SourceError(f"cannot read the path lists of {UPDATER_REL}")
    # The updater replaces a directory only when the template has it. Every
    # one must be there, so that what it later runs from the private copy (the
    # manifest tool) is always the approved revision's and never the project's.
    if not all((tree / top).is_dir() for top in shipped.dirs):
        raise SourceError("the approved template lacks a directory it replaces")
    files = tuple(rel for rel in shipped.files if (tree / rel).is_file())
    fingerprint = targets.fingerprint(str(tree), shipped.dirs, files, VERSION_REL)
    return Source(tree, work, approved, shipped, files, fingerprint, version)


@contextlib.contextmanager
def open_source(ctx: common.Context, approved: str) -> Iterator[Source]:
    """Lay the approved revision out in a scratch worktree, named by its SHA
    and by nothing else, and remove it afterwards."""
    common.ensure_private_dir(ctx.paths.run_dir)
    for stale in sorted(ctx.paths.run_dir.glob(f"{WORK_PREFIX}*")):
        _remove_work(ctx, stale)
    work = ctx.paths.run_dir / f"{WORK_PREFIX}{os.getpid()}"
    # Private by its own mode: the copies below hold projects' settings.
    for scratch in (work, work / SCRATCH_TMP_DIR, work / EMPTY_DIR):
        common.ensure_private_dir(scratch)
    try:
        tree = str(work / SOURCE_DIR)
        if not ctx.git.run(
            ["worktree", "add", "--quiet", "--detach", tree, approved]
        ).ok:
            raise SourceError("cannot check out the approved revision")
        yield _read_source(ctx, work, approved)
    finally:
        _remove_work(ctx, work)


# --------------------------------------------------------------------------
# The update, prepared on a private copy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Staged:
    """A private copy of a project's managed paths, as it was (``before``)
    and as the approved updater left it (``after``)."""

    stage: str
    before: dict[str, targets.Entry]
    after: dict[str, targets.Entry]

    def changes(self) -> list[tuple[str, str]]:
        """(what happens, path) for every file or link that would change."""
        found: list[tuple[str, str]] = []
        for rel in targets.changed_paths(self.before, self.after):
            old, new = self.before.get(rel), self.after.get(rel)
            gone = new is None or new.kind == targets.KIND_DIR
            absent = old is None or old.kind == targets.KIND_DIR
            if gone and absent:
                continue
            found.append((CREATE if absent else DELETE if gone else CHANGE, rel))
        return found


def scope_paths(source: Source, target: str) -> tuple[str, ...]:
    """Every path of a project the updater reads or writes. A canonical
    directory is included only when its legacy twin exists, because only then
    does the updater merge into it."""
    shipped = source.shipped
    rels = [*shipped.dirs, *shipped.files, *shipped.settings, *shipped.deprecated]
    rels += [*PROJECT_FILES, *LEGACY_FILES]
    for name in shipped.legacy:
        rels.append(f"{LEGACY_ROOT}/{name}")
        if os.path.isdir(os.path.join(target, LEGACY_ROOT, name)):
            rels.append(f"{CANONICAL_ROOT}/{name}")
    return tuple(rels)


def _first_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return common.one_line(lines[0]) if lines else ""


def stage_update(ctx: common.Context, source: Source, target: str, name: str) -> Staged:
    """Copy the project's managed paths, run the approved updater on the
    copy, and check that what came out is the approved revision.

    The updater's standard output is dropped: it prints the difference
    between the project's settings and the template's, and settings can hold
    secrets.
    """
    stage = source.work / name
    stage.mkdir()
    try:
        targets.copy_scope(target, str(stage), scope_paths(source, target))
    except (OSError, shutil.Error) as exc:
        ctx.log.line(f"distribute: cannot copy from the project: {exc}")
        raise StageProblem(
            REFUSED, "holds something under the updater's paths that cannot be copied"
        ) from None
    before = targets.snapshot(str(stage), skip_top=())
    # The updater insists on a git work tree. This one is the job's own.
    updater = stage / UPDATER_REL
    updater.parent.mkdir(parents=True, exist_ok=True)
    if not ctx.git.run(["init", "--quiet", str(stage)]).ok:
        raise StageProblem(FAILED, "cannot prepare the private copy")
    # The approved updater, placed where it expects to be installed. Whatever
    # the project had there is removed first, never written through.
    if os.path.lexists(str(updater)):
        updater.unlink()
    shutil.copyfile(str(source.root / UPDATER_REL), str(updater))

    env = {
        **ctx.env,
        TEMPLATE_ENV: str(source.root),
        "TMPDIR": str(source.work / SCRATCH_TMP_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Started from an empty directory of the job's own: an interpreter looks
    # for modules where it is started, and the copy holds the project's files.
    nowhere = source.work / EMPTY_DIR
    done = common.run_command(
        [BASH, str(updater), "--yes"], UPDATER_TIMEOUT_SEC, cwd=nowhere, env=env
    )
    if not done.ok:
        ctx.log.line(f"distribute: updater failed -- {done.failure()}")
        raise StageProblem(FAILED, "the updater failed on the private copy")
    if source.fingerprint_of(str(stage)) != source.fingerprint:
        raise StageProblem(
            FAILED, "the updater did not produce the approved revision's files"
        )
    models = stage / MODELS_REL
    if models.is_file():
        check = common.run_command(
            ["python3", str(models), "--project-root", str(stage), "check"],
            MODELS_TIMEOUT_SEC,
            cwd=nowhere,
            env=env,
        )
        if not check.ok:
            ctx.log.line(f"distribute: model check failed -- {check.failure()}")
            why = _first_line(check.stderr) or "the model check did not pass"
            raise StageProblem(REFUSED, f"has model settings to fix first: {why}")
    return Staged(str(stage), before, targets.snapshot(str(stage)))


def unattended_problem(source: Source, staged: Staged) -> str | None:
    """The first change a scheduled run must not make by itself; None when
    every change is to the template's own paths, a model key in a settings
    file, or a missing seed."""
    shipped = source.shipped
    owned = {*shipped.files, VERSION_STAMP_REL}
    for what, rel in staged.changes():
        inside = any(rel.startswith(f"{top}/") for top in shipped.dirs)
        patched = rel in shipped.settings and what == CHANGE
        seeded = rel in SEED_FILES and what == CREATE
        if not (inside or rel in owned or patched or seeded):
            return f"the update would also {what} {rel}; adopt it again to see and allow it"
    return None


# --------------------------------------------------------------------------
# The record: what was adopted, and what the last run found
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Adoption:
    """One project the owner allowed the job to maintain.

    ``identity`` is the directory and repository that were adopted;
    ``source_print`` and ``target_print`` are the approved template and the
    project's template-owned content at the job's last write there.
    ``interrupted`` names a revision whose write did not finish.
    """

    identity: tuple[int, ...]
    adopted_at: float
    revision: str
    source_print: str
    target_print: str
    updated_at: float
    waiting_revision: str = ""
    waiting_since: float = 0.0
    interrupted: str = ""


def _adoption_from(raw: object) -> Adoption | None:
    """An adoption read from the record, or None when it is not one."""
    if not isinstance(raw, dict):
        return None
    try:
        adoption = Adoption(**{**raw, "identity": tuple(raw["identity"])})
    except (KeyError, TypeError):
        return None
    numbers = (*adoption.identity, adoption.adopted_at, adoption.updated_at)
    texts = (adoption.revision, adoption.source_print, adoption.target_print)
    well_formed = (
        len(adoption.identity) == targets.IDENTITY_LENGTH
        and all(isinstance(value, (int, float)) for value in numbers)
        and all(isinstance(value, str) for value in texts)
    )
    return adoption if well_formed else None


@dataclass(frozen=True)
class Visit:
    """What one run found for one project. ``pending`` and ``since`` are set
    while a revision waits for the project."""

    target: str
    state: str
    detail: str = ""
    pending: str = ""
    since: float = 0.0


class Ledger:
    """``state/distribution.json``: the adoptions and the last run's table.
    Kept outside every project, private, replaced in one rename."""

    def __init__(self, paths: common.Paths) -> None:
        self.path = paths.state_dir / LEDGER_FILE
        if self.path.is_symlink():
            raise common.Refused(f"{self.path} is a link; it must be a real file")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        raw = data.get("adopted")
        found = {
            target: _adoption_from(record)
            for target, record in (raw.items() if isinstance(raw, dict) else ())
        }
        self.adoptions = {t: a for t, a in found.items() if a is not None}
        last = data.get("last")
        self.last: list[dict[str, object]] = last if isinstance(last, list) else []

    def _write(self) -> None:
        adopted = {target: asdict(a) for target, a in self.adoptions.items()}
        body = {"version": 1, "adopted": adopted, "last": self.last}
        common.write_text_atomic(self.path, json.dumps(body, indent=2) + "\n")

    def put(self, target: str, adoption: Adoption | None) -> None:
        """Record one adoption; None takes the project out of the record."""
        others = {t: a for t, a in self.adoptions.items() if t != target}
        self.adoptions = others if adoption is None else {**others, target: adoption}
        self._write()

    def close(self, visits: list[Visit]) -> None:
        self.last = [asdict(visit) for visit in visits]
        self._write()


# --------------------------------------------------------------------------
# One project
# --------------------------------------------------------------------------


def _path_shown(entry: str) -> str:
    """A git status record as one safe word-like piece of text."""
    return common.one_line(entry, common.DISPLAY_LIMIT // 4)


def write_update(
    ctx: common.Context,
    source: Source,
    ledger: Ledger,
    target: str,
    adoption: Adoption,
    staged: Staged,
) -> Visit:
    """Carry a prepared update into the project and record it. The record
    says ``interrupted`` from just before the first write until every path
    and the repository's own state have been checked."""
    git = targets.TargetGit(ctx, target)
    state = targets.git_state(git)
    if state is None:
        return Visit(target, FAILED, "cannot read the repository's state")
    changed = len(staged.changes())
    previous = ledger.adoptions.get(target)
    ledger.put(target, replace(adoption, interrupted=source.revision))
    try:
        targets.apply_changes(target, staged.stage, staged.before, staged.after)
        problem = targets.applied_problem(target, staged.before, staged.after)
    except targets.Conflict as exc:
        # Raised before the first write: the project is as it was, and so is
        # the record.
        ledger.put(target, previous)
        return Visit(target, FAILED, f"not written: {exc}")
    except OSError as exc:
        ctx.log.line(f"distribute: stopped while writing: {exc}")
        problem = f"writing stopped ({type(exc).__name__})"
    if problem is None and targets.git_state(git) != state:
        problem = "the repository's HEAD, refs or index changed during the update"
    if problem is not None:
        return Visit(target, FAILED, f"written, but {problem}")
    done = replace(
        adoption,
        revision=source.revision,
        source_print=source.fingerprint,
        target_print=source.fingerprint_of(target),
        updated_at=ctx.now,
        waiting_revision="",
        waiting_since=0.0,
        interrupted="",
    )
    ledger.put(target, done)
    if not changed:
        return Visit(target, UP_TO_DATE, "nothing to write")
    return Visit(target, UPDATED, f"{changed} paths changed, left uncommitted")


def visit_target(
    ctx: common.Context, source: Source, ledger: Ledger, target: str, name: str
) -> Visit:
    """Decide what an adopted project needs, and do it when every guard
    holds. Each early return leaves the project exactly as it was found."""
    adoption = ledger.adoptions[target]
    problem = targets.path_problem(target, ctx.paths)
    if problem is not None:
        return Visit(target, REFUSED, problem)
    if tuple(targets.identity(target)) != adoption.identity:
        why = "another directory or repository is at this path now"
        return Visit(target, NOT_ADOPTED, why)
    git = targets.TargetGit(ctx, target)
    problem = targets.repository_problem(git)
    problem = problem or targets.layout_problem(target, source.guarded)
    if problem is not None:
        return Visit(target, REFUSED, problem)
    if adoption.interrupted:
        short = common.short_sha(adoption.interrupted)
        return Visit(target, LOCAL_CHANGES, f"an update to {short} stopped half-way")
    dirty = targets.uncommitted(git)
    if dirty is None:
        return Visit(target, FAILED, "git status failed")

    have = source.fingerprint_of(target)
    if adoption.source_print == source.fingerprint and have == adoption.target_print:
        # The project holds what this revision ships: nothing to write, even
        # when the approved SHA moved (a promotion that changed nothing here).
        settled = replace(
            adoption, revision=source.revision, waiting_revision="", waiting_since=0.0
        )
        if settled != adoption:
            ledger.put(target, settled)
        return Visit(target, UNCOMMITTED if dirty else UP_TO_DATE)
    if dirty:
        same = adoption.waiting_revision == source.revision
        since = adoption.waiting_since if same else ctx.now
        if not same:
            waiting = replace(
                adoption, waiting_revision=source.revision, waiting_since=since
            )
            ledger.put(target, waiting)
        detail = f"{len(dirty)} uncommitted paths, first: {_path_shown(dirty[0])}"
        return Visit(target, WAITING_DIRTY, detail, source.revision, since)
    marker = targets.operation_in_progress(target)
    if marker is not None:
        return Visit(target, BUSY, f"git is in the middle of something ({marker})")
    if targets.recent_session(target, ctx.env.get("HOME", ""), ctx.now):
        return Visit(target, BUSY, "a Claude Code session wrote here within the hour")
    if have != adoption.target_print:
        why = "template-owned files are not what the job left there"
        return Visit(target, LOCAL_CHANGES, why)
    try:
        staged = stage_update(ctx, source, target, name)
    except StageProblem as exc:
        return Visit(target, exc.state, exc.detail)
    problem = unattended_problem(source, staged)
    if problem is not None:
        return Visit(target, LOCAL_CHANGES, problem)
    return write_update(ctx, source, ledger, target, adoption, staged)


def _guarded_visit(
    ctx: common.Context, source: Source, ledger: Ledger, target: str, name: str
) -> Visit:
    """One project failing, however it fails, is that project's result."""
    try:
        return visit_target(ctx, source, ledger, target, name)
    except Exception as exc:
        ctx.log.line(f"distribute: {type(exc).__name__}: {exc}")
        return Visit(target, FAILED, f"stopped by {type(exc).__name__}")


# --------------------------------------------------------------------------
# The scheduled step
# --------------------------------------------------------------------------


def registry_path(paths: common.Paths) -> Path:
    return paths.state_root / targets.REGISTRY_FILE


def _result(
    result: str, exit_code: int, detail: str = "", counts: dict[str, int] | None = None
) -> common.StepResult:
    return common.checked_result(OUTCOMES, STEP, result, exit_code, detail, counts)


def _summary(visits: list[Visit]) -> common.StepResult:
    counts = {COUNT_TARGETS: len(visits)}
    for visit in visits:
        counts = {**counts, visit.state: counts.get(visit.state, 0) + 1}
    worst = max(TARGET_STATES[visit.state][0] for visit in visits)
    if worst == common.EXIT_FAILED:
        result = "target-failed"
    elif worst == common.EXIT_NEEDS_OWNER:
        result = "needs-owner"
    elif counts.get(UPDATED):
        result = "distributed"
    elif counts.get(WAITING_DIRTY) or counts.get(BUSY):
        result = "waiting"
    else:
        result = "up-to-date"
    detail = "; ".join(
        f"{os.path.basename(visit.target)}: {visit.state}"
        + (f" ({visit.detail})" if visit.detail else "")
        for visit in visits
    )
    return _result(result, worst, detail, counts)


def run(ctx: common.Context) -> common.StepResult:
    """The scheduled step: visit every listed project once."""
    approved = promote.read_approved(ctx.paths)
    if approved is None:
        why = "no approved revision recorded"
        return _result("no-approved-revision", common.EXIT_USAGE, why)
    ledger = Ledger(ctx.paths)
    entries = targets.read_registry(registry_path(ctx.paths))
    if not entries:
        # No registry, no trace: the record is only emptied when there is one.
        if ledger.path.exists():
            ledger.close([])
        hint = f"no project is listed in {registry_path(ctx.paths)}"
        return _result("no-targets", common.EXIT_OK, hint)

    # A project that was not adopted is not looked at, not even its path.
    found: dict[str, Visit] = {}
    for entry in entries:
        if not targets.is_absolute(entry):
            found[entry] = Visit(entry, REFUSED, "is not an absolute path")
        elif entry not in ledger.adoptions:
            found[entry] = Visit(entry, NOT_ADOPTED, "run launchd.sh adopt for it")
    adopted = [entry for entry in entries if entry not in found]
    if adopted:
        try:
            with open_source(ctx, approved) as source:
                for index, entry in enumerate(adopted):
                    name = f"stage-{index}"
                    found[entry] = _guarded_visit(ctx, source, ledger, entry, name)
        except SourceError as exc:
            return _result("source-failed", common.EXIT_FAILED, str(exc))
    visits = [found[entry] for entry in entries]
    ledger.close(visits)
    for visit in visits:
        ctx.log.line(f"distribute: {visit.state}: {visit.target} {visit.detail}")
    return _summary(visits)


def status_lines(paths: common.Paths) -> list[str]:
    """The last run's table for ``status``: one line per listed project."""
    try:
        rows = Ledger(paths).last
    except common.Refused:
        return []
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        line = f"  target {row.get('state')}  {row.get('target')}"
        since = row.get("since")
        if row.get("pending") and isinstance(since, (int, float)):
            waiting = common.short_sha(row.get("pending"))
            line += f"  waits for {waiting} since {common.utc_display(since)}"
        lines.append(f"{line}  {row.get('detail') or ''}")
    return lines
