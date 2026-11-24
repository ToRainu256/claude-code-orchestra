#!/usr/bin/env python3
"""The approved revision, laid out once per run, and the update it makes on a
private copy of one project.

``scripts/update.sh`` was written to be run by a project's owner in their own
tree. Here it is run by a job on content nobody has looked at, so it never
sees a project: it gets a copy, and the copy is built and fenced so that
nothing in it can lead a write somewhere else.

* **The copy holds regular files and directories, nothing else.** It is built
  by this module, entry by entry: directories are created here, every file is
  opened without following a link and created exclusively. A link, a FIFO, a
  device or a socket anywhere under the paths the updater manages stops the
  copy with the path named; so do two names that become one in the copy.
* **The copy has the shape the updater expects.** Before it starts, every path
  the updater treats as a file is a file or absent, every path it treats as a
  directory is a directory or absent, and nothing carries one of the names the
  updater uses for its own temporary files.
* **The updater is fenced in.** Its home, its temporary directory and its
  working directory are inside the run's scratch area. ``git`` and ``python3``
  are resolved before the home moves. Where ``sandbox-exec`` works, file
  writes anywhere else are denied by the kernel, so a path nobody thought of
  fails instead of escaping; where it does not work, the two rules above are
  the guarantee, the run says so in its log, and every such run of the
  updater is counted (``Source.unfenced``) for the step to report.
* **What comes out is checked.** The result must be the approved revision's
  files, hold regular files and directories only, and pass the manifest
  tool's own check.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import common
import promote
import targets

WORK_PREFIX = "distribute-"
SOURCE_DIR = "source"
SCRATCH_TMP_DIR, SCRATCH_HOME_DIR, EMPTY_DIR, TOOLS_DIR = "tmp", "home", "empty", "bin"
BASH = "/bin/bash"
TEMPLATE_ENV = "ORCHESTRA_TEMPLATE_REPO"
UPDATER_REL = promote.UPDATE_SCRIPT_REL
MODELS_REL = ".claude/skills/_shared/models.py"
VERSION_REL = "VERSION"
VERSION_STAMP_REL = ".claude/orchestra-version"
# Project-owned files the updater creates when they are missing.
SEED_FILES = (".claude/STATE.md", ".claude/docs/DESIGN.md")
PROJECT_FILES = (*SEED_FILES, VERSION_STAMP_REL, ".claude/models.local.json")
# Directories the updater only makes sure exist (`mkdir -p`). They are outside
# the copy, so nothing here ever reads what a project keeps in them; a link or
# a file at one of these paths is found in the project itself, before the
# updater runs (``targets.layout_problem``). A test holds the list to the
# updater's own.
MADE_DIRS = (".claude/docs/research", ".claude/logs", ".claude/checkpoints")
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
# Names the updater gives its own temporary files and directories, in any
# version: none may be in the copy before it starts.
UPDATER_TEMP_RE = re.compile(
    r"\.tmp\.[0-9]+$|\.orchestra-(staging|old)\.|^\.orchestra-tmp\."
)
UPDATER_TIMEOUT_SEC = 600
MODELS_TIMEOUT_SEC = 120
PROBE_TIMEOUT_SEC = 30
COPY_CHUNK = 1 << 20
STAGED_FILE_MODE, STAGED_EXECUTABLE_MODE = 0o600, 0o700

SANDBOX = "/usr/bin/sandbox-exec"
SANDBOX_PARAMETER = "WORK"
# Everything is allowed but writing files, and that only below the scratch
# area of this run. A later rule wins over an earlier one.
SANDBOX_PROFILE = (
    "(version 1)"
    "(allow default)"
    "(deny file-write*)"
    f'(allow file-write* (subpath (param "{SANDBOX_PARAMETER}"))'
    ' (literal "/dev/null") (literal "/dev/dtracehelper"))'
)

CREATE, CHANGE, DELETE = "create", "change", "delete"


class SourceError(Exception):
    """The approved revision could not be laid out to distribute from."""


class StageProblem(Exception):
    """The update could not be prepared for one project. ``refused`` when it
    is the project that has to change, not the job that failed."""

    def __init__(self, refused: bool, detail: str) -> None:
        super().__init__(detail)
        self.refused = refused
        self.detail = detail


class Unstageable(Exception):
    """Something in the project cannot go into the private copy."""


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
    exactly this revision's template; ``fence`` is what the updater's command
    line starts with (the sandbox, or nothing). ``unfenced`` is the one thing
    here that grows during a run: the name of every private copy the updater
    was started on without the sandbox."""

    root: Path
    work: Path
    revision: str
    shipped: Shipped
    files: tuple[str, ...]
    fingerprint: str
    version: str
    fence: tuple[str, ...]
    unfenced: list[str] = field(default_factory=list, compare=False)

    @property
    def file_paths(self) -> tuple[str, ...]:
        """Paths the updater reads or writes as one file."""
        shipped = self.shipped
        return (*shipped.files, *shipped.settings, *PROJECT_FILES, *LEGACY_FILES)

    @property
    def guarded(self) -> tuple[str, ...]:
        """Paths that must be plain directory chains inside a project."""
        shipped = self.shipped
        return (*shipped.dirs, *shipped.files, *shipped.settings, *PROJECT_FILES)

    def directory_paths(self, target: str) -> tuple[str, ...]:
        """Paths the updater treats as a directory. A canonical directory
        counts only when its legacy twin exists, because only then does the
        updater merge into it."""
        merged = tuple(
            f"{CANONICAL_ROOT}/{name}"
            for name in self.shipped.legacy
            if os.path.isdir(os.path.join(target, LEGACY_ROOT, name))
        )
        return (*self.shipped.dirs, *merged)

    def scope(self, target: str) -> tuple[str, ...]:
        """Every path of a project the updater reads or writes."""
        legacy = tuple(f"{LEGACY_ROOT}/{name}" for name in self.shipped.legacy)
        return (
            *self.directory_paths(target),
            *self.file_paths,
            *self.shipped.deprecated,
            *legacy,
        )

    def fingerprint_of(self, root: str) -> str:
        """The template-owned content below ``root`` as it compares with the
        approved template: without the per-project model lines."""
        return targets.fingerprint(
            root, self.shipped.dirs, self.files, VERSION_STAMP_REL
        )

    def left_print_of(self, root: str) -> str:
        """The template-owned content below ``root``, every byte of it: what
        the record keeps of a project right after a write, and what a later
        run compares the project with to see whether anybody edited it."""
        return targets.fingerprint(
            root, self.shipped.dirs, self.files, VERSION_STAMP_REL, exact=True
        )


def _plain(rel: str) -> bool:
    parts = rel.split("/")
    return not rel.startswith("/") and all(
        part not in ("", ".", "..") for part in parts
    )


def read_shipped(updater: str) -> Shipped | None:
    """The updater's path lists, or None when one cannot be read as plain
    relative paths. The step does not guess at what an updater writes."""
    lists = [promote.parse_bash_array(updater, name) for name in UPDATER_ARRAYS]
    if any(entries is None for entries in lists):
        return None
    found = [tuple(entries or ()) for entries in lists]
    if not all(found[:3]) or not all(_plain(rel) for group in found for rel in group):
        return None
    return Shipped(*found)


def _force_remove(work: Path) -> None:
    """Delete a scratch directory, whatever modes its directories have."""
    for directory, dirnames, _ in os.walk(str(work)):
        for name in dirnames:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRWXU)
    shutil.rmtree(str(work))


def _remove_work(ctx: common.Context, work: Path) -> None:
    common.remove_scratch_worktree(ctx.git, work / SOURCE_DIR)
    if work.exists():
        _force_remove(common.require_scratch(ctx.paths, work))


def _resolved_tool(ctx: common.Context, argv: list[str]) -> str:
    """The absolute path one probing command prints; ``SourceError`` when it
    prints none."""
    done = common.run_command(argv, PROBE_TIMEOUT_SEC, env=ctx.env)
    path = done.stdout.strip()
    if not done.ok or not os.path.isabs(path):
        raise SourceError(f"cannot resolve {argv[0]} for the updater")
    return path


def _write_tools(ctx: common.Context, work: Path) -> None:
    """Put ``git`` and ``python3`` in the scratch area as two-line scripts that
    start the real programs. The updater runs with another home directory, and
    a version manager's shim would look for its configuration there."""
    python = _resolved_tool(ctx, ["python3", "-c", "import sys; print(sys.executable)"])
    git = os.path.join(_resolved_tool(ctx, ["git", "--exec-path"]), "git")
    for name, real in (("python3", python), ("git", git)):
        if not os.path.isfile(real):
            raise SourceError(f"cannot resolve {name} for the updater")
        script = work / TOOLS_DIR / name
        script.write_text(f'#!/bin/sh\nexec {shlex.quote(real)} "$@"\n', "utf-8")
        script.chmod(stat.S_IRWXU)


def _fence(ctx: common.Context, work: Path) -> tuple[str, ...]:
    """The sandbox prefix for the updater's command line, proven to work by
    one write inside the scratch area; empty when it does not work here (not
    macOS, or this process is itself sandboxed)."""
    real = os.path.realpath(str(work))
    prefix = (SANDBOX, "-D", f"{SANDBOX_PARAMETER}={real}", "-p", SANDBOX_PROFILE)
    probe = os.path.join(real, EMPTY_DIR, "probe")
    if os.path.isfile(SANDBOX):
        argv = [*prefix, "/bin/sh", "-c", ': > "$1"', "probe", probe]
        done = common.run_command(argv, PROBE_TIMEOUT_SEC, env=ctx.env)
        if done.ok and os.path.isfile(probe):
            os.unlink(probe)
            return prefix
    ctx.log.line("distribute: no sandbox here; the updater runs without one")
    return ()


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
    _write_tools(ctx, work)
    fence = _fence(ctx, work)
    return Source(tree, work, approved, shipped, files, fingerprint, version, fence)


@contextlib.contextmanager
def open_source(ctx: common.Context, approved: str) -> Iterator[Source]:
    """Lay the approved revision out in a scratch worktree, named by its SHA
    and by nothing else, and remove it afterwards."""
    common.ensure_private_dir(ctx.paths.run_dir)
    for stale in sorted(ctx.paths.run_dir.glob(f"{WORK_PREFIX}*")):
        _remove_work(ctx, stale)
    work = ctx.paths.run_dir / f"{WORK_PREFIX}{os.getpid()}"
    # Private by its own mode: the copies below hold projects' settings.
    for name in ("", SCRATCH_TMP_DIR, SCRATCH_HOME_DIR, EMPTY_DIR, TOOLS_DIR):
        common.ensure_private_dir(work / name)
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
# The private copy
# --------------------------------------------------------------------------


def _make_dirs(stage: str, rel: str, made: set[str]) -> None:
    """Create ``rel`` and its parents in the copy. A name that is already
    there and was not created for this very path is two names becoming one."""
    current = ""
    for part in rel.split("/") if rel else ():
        current = f"{current}/{part}" if current else part
        if current in made:
            continue
        try:
            os.mkdir(os.path.join(stage, current))
        except FileExistsError:
            raise Unstageable(f"has two names that become one at {current}") from None
        made.add(current)


def _copy_file(source: str, destination: str, rel: str) -> None:
    """Copy one regular file. Neither end follows a link, and the destination
    must not exist: nothing is ever written onto something already there."""
    reading = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    writing = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        origin = os.open(source, reading)
    except OSError:
        raise Unstageable(f"has a file that cannot be opened at {rel}") from None
    try:
        status = os.fstat(origin)
        if not stat.S_ISREG(status.st_mode):
            raise Unstageable(f"has something that is not a regular file at {rel}")
        try:
            copy = os.open(destination, writing, STAGED_FILE_MODE)
        except FileExistsError:
            raise Unstageable(f"has two names that become one at {rel}") from None
        try:
            for chunk in iter(lambda: os.read(origin, COPY_CHUNK), b""):
                os.write(copy, chunk)
            executable = bool(status.st_mode & targets.EXEC_BITS)
            os.fchmod(copy, STAGED_EXECUTABLE_MODE if executable else STAGED_FILE_MODE)
        finally:
            os.close(copy)
    finally:
        os.close(origin)


def _copy_entry(target: str, stage: str, rel: str, made: set[str]) -> None:
    source = os.path.join(target, rel)
    try:
        status = os.lstat(source)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(status.st_mode):
        _make_dirs(stage, rel, made)
        for name in sorted(os.listdir(source)):
            _copy_entry(target, stage, f"{rel}/{name}", made)
    elif stat.S_ISREG(status.st_mode):
        if rel in made:
            return
        _make_dirs(stage, os.path.dirname(rel), made)
        _copy_file(source, os.path.join(stage, rel), rel)
        made.add(rel)
    elif stat.S_ISLNK(status.st_mode):
        raise Unstageable(f"has a link at {rel}")
    else:
        raise Unstageable(f"has something that is not a file or a directory at {rel}")


def copy_scope(target: str, stage: str, rels: tuple[str, ...]) -> None:
    """Build the private copy of ``rels`` in the empty directory ``stage``.

    Regular files and directories only: anything else, anywhere below one of
    the paths or on the way to it, raises :class:`Unstageable` with the path.
    Nothing is followed, and nothing is written onto an existing name.
    """
    made: set[str] = set()
    for rel in rels:
        current = target
        for part in rel.split("/")[:-1]:
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise Unstageable(f"has a link on the way to {rel}")
        _copy_entry(target, stage, rel, made)


def shape_problem(
    source: Source, target: str, staged: dict[str, targets.Entry]
) -> str | None:
    """Why the updater must not be started on this copy; None when every path
    it will treat as a file or as a directory is one or is absent, and nothing
    is named like one of its temporary files."""
    for rel in source.file_paths:
        entry = staged.get(rel)
        if entry is not None and entry.kind != targets.KIND_FILE:
            return f"has a directory where the file {rel} belongs"
    for rel in source.directory_paths(target):
        entry = staged.get(rel)
        if entry is not None and entry.kind != targets.KIND_DIR:
            return f"has a file where the directory {rel} belongs"
    for rel in sorted(staged):
        if UPDATER_TEMP_RE.search(rel.rsplit("/", 1)[-1]) is not None:
            return f"has a name the updater keeps for its temporary files at {rel}"
    return None


@dataclass(frozen=True)
class Staged:
    """A private copy of a project's managed paths, as it was (``before``)
    and as the approved updater left it (``after``).

    ``before_print`` and ``after_print`` are the template-owned content of
    the copy at those two moments, every byte of it. They are taken from the
    copy, not from the project: what was compared with the record is then
    exactly what the update was prepared from, and what the record keeps is
    exactly what was written, whatever is saved in the project meanwhile."""

    stage: str
    before: dict[str, targets.Entry]
    after: dict[str, targets.Entry]
    before_print: str = ""
    after_print: str = ""

    def changes(self) -> list[tuple[str, str]]:
        """(what happens, path) for every file that would be created, changed
        or deleted, and for every directory that would be removed although it
        holds no file (written with a trailing slash)."""
        found: list[tuple[str, str]] = []
        directory = targets.KIND_DIR
        for rel in targets.changed_paths(self.before, self.after):
            old, new = self.before.get(rel), self.after.get(rel)
            gone = new is None or new.kind == directory
            absent = old is None or old.kind == directory
            if not (gone and absent):
                found.append((CREATE if absent else DELETE if gone else CHANGE, rel))
            elif new is None and self._empty_and_topmost(rel):
                found.append((DELETE, f"{rel}/"))
        return found

    def _empty_and_topmost(self, rel: str) -> bool:
        """Whether a removed directory held no file, and is not inside another
        removed directory that held none either."""

        def holds_no_file(top: str) -> bool:
            return not any(
                entry.kind != targets.KIND_DIR
                for path, entry in self.before.items()
                if path.startswith(f"{top}/")
            )

        parent = rel.rpartition("/")[0]
        nested = bool(parent) and parent not in self.after and holds_no_file(parent)
        return holds_no_file(rel) and not nested


def _first_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return common.one_line(lines[0]) if lines else ""


def _updater_env(ctx: common.Context, source: Source) -> dict[str, str]:
    """The updater's environment: its home and temporary directory are in the
    scratch area, and the two tools it starts are the ones resolved for it."""
    kept = {name: value for name, value in ctx.env.items() if name[:4] != "XDG_"}
    return {
        **kept,
        "HOME": str(source.work / SCRATCH_HOME_DIR),
        "TMPDIR": str(source.work / SCRATCH_TMP_DIR),
        "PATH": f"{source.work / TOOLS_DIR}{os.pathsep}{ctx.env.get('PATH', '')}",
        TEMPLATE_ENV: str(source.root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


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
        copy_scope(target, str(stage), source.scope(target))
    except Unstageable as exc:
        raise StageProblem(True, str(exc)) from None
    except OSError as exc:
        ctx.log.line(f"distribute: cannot copy from the project: {exc}")
        detail = "holds something under the updater's paths that cannot be read"
        raise StageProblem(True, detail) from None
    before = targets.snapshot(str(stage), skip_top=())
    problem = shape_problem(source, target, before)
    if problem is not None:
        raise StageProblem(True, problem)
    before_print = source.left_print_of(str(stage))
    # The updater insists on a git work tree. This one is the job's own.
    updater = stage / UPDATER_REL
    updater.parent.mkdir(parents=True, exist_ok=True)
    if not ctx.git.run(["init", "--quiet", str(stage)]).ok:
        raise StageProblem(False, "cannot prepare the private copy")
    # The approved updater, placed where it expects to be installed. Whatever
    # the project had there is removed first, never written through.
    if os.path.lexists(str(updater)):
        updater.unlink()
    shutil.copyfile(str(source.root / UPDATER_REL), str(updater))

    env = _updater_env(ctx, source)
    # Started from an empty directory of the job's own: an interpreter looks
    # for modules where it is started, and the copy holds the project's files.
    nowhere = source.work / EMPTY_DIR
    if not source.fence:
        source.unfenced.append(name)
    done = common.run_command(
        [*source.fence, BASH, str(updater), "--yes"],
        UPDATER_TIMEOUT_SEC,
        cwd=nowhere,
        env=env,
    )
    if not done.ok:
        ctx.log.line(f"distribute: updater failed -- {done.failure()}")
        raise StageProblem(False, "the updater failed on the private copy")
    after = targets.snapshot(str(stage))
    plain = (targets.KIND_FILE, targets.KIND_DIR)
    odd = sorted(rel for rel, entry in after.items() if entry.kind not in plain)
    if odd:
        raise StageProblem(False, f"the updater left a link or worse at {odd[0]}")
    if source.fingerprint_of(str(stage)) != source.fingerprint:
        raise StageProblem(
            False, "the updater did not produce the approved revision's files"
        )
    models = stage / MODELS_REL
    if models.is_file():
        check = common.run_command(
            [
                *source.fence,
                "python3",
                str(models),
                "--project-root",
                str(stage),
                "check",
            ],
            MODELS_TIMEOUT_SEC,
            cwd=nowhere,
            env=env,
        )
        if not check.ok:
            ctx.log.line(f"distribute: model check failed -- {check.failure()}")
            why = _first_line(check.stderr) or "the model check did not pass"
            raise StageProblem(True, f"has model settings to fix first: {why}")
    return Staged(
        str(stage), before, after, before_print, source.left_print_of(str(stage))
    )
