#!/usr/bin/env python3
"""An installed project as files: where it is, what it holds, and the only
code that writes into one.

A target is somebody's working repository. Everything in it is treated as
hostile until shown otherwise (git there is ``target_git.py``'s business; the
updater never runs there at all, see ``staging.py``). What this module adds:

* **The path is vetted before anything else** (:func:`path_problem`,
  :func:`layout_problem`): a real directory with a repository of its own,
  outside the job's directory, with no link and no nested repository on the
  way to a path the updater manages.
* **Content is compared, never assumed** (:func:`snapshot`,
  :func:`fingerprint`): by bytes, without following a link.
* **A write is planned whole, then made** (:func:`plan_changes`,
  :func:`apply_plan`). The plan fails, with nothing written, unless every path
  it touches is right now what the update was prepared from and every step of
  it is possible. Files go in beside their destination under a name nobody
  can predict and are renamed into place; no git command runs. What fails
  after the first change is reported as an interrupted write, never as "not
  written".

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

import common

REGISTRY_FILE = "targets.txt"
GIT_DIR = ".git"
HASH_CHUNK = 1 << 20

# Present in the repository while git is in the middle of something.
GIT_OPERATION_MARKERS = (
    "rebase-merge", "rebase-apply", "sequencer", "MERGE_HEAD", "CHERRY_PICK_HEAD",
    "REVERT_HEAD", "BISECT_LOG", "index.lock",
)  # fmt: skip
SESSION_QUIET_SEC = 3600
SESSION_DIR_PARTS = (".claude", "projects")
SESSION_GLOB = "*.jsonl"
SLUG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9]")

# Files that appear by themselves beside the template's own: never part of a
# comparison, and never worth a line on the owner's screen.
JUNK_DIRS = ("__pycache__",)
JUNK_FILES = (".DS_Store",)
JUNK_SUFFIXES = (".pyc",)
AGENT_FILE_RE = re.compile(r"\.claude/agents/[^/]+\.md")
MODEL_LINE_PREFIX = b"model:"

KIND_FILE, KIND_LINK, KIND_DIR, KIND_OTHER = "file", "link", "dir", "other"
EXEC_BITS = 0o111
READ_BITS = 0o444
NEW_FILE_MODE = 0o644
NEW_DIR_MODE = 0o755
KEPT_MODE_BITS = 0o666
TEMP_PREFIX = ".orchestra-maint-"
TEMP_SUFFIX = ".tmp"
TEMP_TOKEN_BYTES = 8
LEFTOVER_RE = re.compile(
    re.escape(TEMP_PREFIX)
    + f"[0-9a-f]{{{2 * TEMP_TOKEN_BYTES}}}"
    + re.escape(TEMP_SUFFIX)
)


class Conflict(Exception):
    """A path in the target is not what the update was prepared from. Raised
    before anything is written."""


class Interrupted(Exception):
    """A write stopped after it had begun: the target may be half-updated."""


# --------------------------------------------------------------------------
# The registry and the path itself
# --------------------------------------------------------------------------


def read_registry(path: Path) -> list[str]:
    """Candidate targets, in file order: one path per line, ``#`` comments and
    blank lines skipped, duplicates dropped. A missing file is no targets."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    entries: list[str] = []
    for raw in data.split(b"\n"):
        line = os.fsdecode(raw).strip()
        if not line or line.startswith("#"):
            continue
        entry = os.path.normpath(line) if os.path.isabs(line) else line
        if entry not in entries:
            entries.append(entry)
    return entries


def is_absolute(target: str) -> bool:
    return "\0" not in target and os.path.isabs(target)


def _same_or_inside(inner: str, outer: str) -> bool:
    """Whether ``inner`` is ``outer`` or lies below it, by identity, so two
    spellings of one directory on a case-insensitive disk still match."""
    current = inner
    while True:
        try:
            if os.path.samefile(current, outer):
                return True
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def path_problem(target: str, paths: common.Paths) -> str | None:
    """Why this registry entry cannot be a target, read from the path alone;
    None when it can. No git runs here."""
    if not is_absolute(target):
        return "is not an absolute path"
    if os.path.realpath(target) != target:
        return "is a link, or is not written as its real path"
    if not os.path.isdir(target):
        return "does not exist or is not a directory"
    repository = os.path.join(target, GIT_DIR)
    if os.path.islink(repository) or not os.path.isdir(repository):
        return "does not hold its own git repository"
    own = os.path.realpath(str(paths.state_root))
    if _same_or_inside(target, own) or _same_or_inside(own, target):
        return "is, contains or lies inside the job's own directory"
    return None


IDENTITY_LENGTH = 4


def identity(target: str) -> list[int]:
    """Which directory and which repository this is, as the file system
    numbers them. A path that later holds another clone has other numbers."""
    root = os.lstat(target)
    repository = os.lstat(os.path.join(target, GIT_DIR))
    return [root.st_dev, root.st_ino, repository.st_dev, repository.st_ino]


# --------------------------------------------------------------------------
# Reasons to wait
# --------------------------------------------------------------------------


def operation_in_progress(target: str) -> str | None:
    """The marker of a rebase, merge, pick, revert or bisect git has not
    finished, or of a held index lock; None when there is none."""
    for name in GIT_OPERATION_MARKERS:
        if os.path.lexists(os.path.join(target, GIT_DIR, name)):
            return name
    return None


def recent_session(target: str, home: str, now: float) -> bool:
    """Whether a Claude Code session wrote in this project within the last
    hour. A reason to wait, nothing more: silence proves nothing."""
    slug = SLUG_UNSAFE_RE.sub("-", target)
    directory = Path(home).joinpath(*SESSION_DIR_PARTS, slug)
    try:
        newest = max(
            (entry.stat().st_mtime for entry in directory.glob(SESSION_GLOB)),
            default=None,
        )
    except OSError:
        return False
    return newest is not None and now - newest < SESSION_QUIET_SEC


def layout_problem(target: str, rels: tuple[str, ...]) -> str | None:
    """Why the updater's paths cannot be written here: one of them, or a
    directory on the way to it, is a link or holds a repository of its own.
    None when every one is a plain chain of directories below the target."""
    for rel in rels:
        current = target
        parts = rel.split("/")
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            shown = "/".join(parts[: index + 1])
            if os.path.islink(current):
                return f"has a link at {shown}"
            if not os.path.lexists(current):
                break
            if os.path.isdir(current):
                if os.path.lexists(os.path.join(current, GIT_DIR)):
                    return f"has a repository of its own at {shown}"
            elif index < len(parts) - 1:
                return f"has something that is not a directory at {shown}"
    return None


# --------------------------------------------------------------------------
# What is on disk
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One path as it is on disk: a file by its content and executable bit, a
    link by where it points, a directory by being one."""

    kind: str
    value: str = ""
    executable: bool = False


def is_junk(rel: str) -> bool:
    parts = rel.split("/")
    return (
        any(part in JUNK_DIRS for part in parts)
        or parts[-1] in JUNK_FILES
        or parts[-1].endswith(JUNK_SUFFIXES)
    )


def _file_digest(path: str, without_model_line: bool) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        if without_model_line:
            for line in handle:
                if not line.startswith(MODEL_LINE_PREFIX):
                    digest.update(line)
        else:
            for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                digest.update(chunk)
    return digest.hexdigest()


def entry_at(path: str, without_model_line: bool = False) -> Entry | None:
    """The entry at ``path``, never following a link; None when nothing is
    there."""
    try:
        status = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        # Not there, or below something that is not a directory: absent.
        return None
    if stat.S_ISLNK(status.st_mode):
        return Entry(KIND_LINK, os.readlink(path))
    if stat.S_ISDIR(status.st_mode):
        return Entry(KIND_DIR)
    if stat.S_ISREG(status.st_mode):
        digest = _file_digest(path, without_model_line)
        return Entry(KIND_FILE, digest, bool(status.st_mode & EXEC_BITS))
    return Entry(KIND_OTHER)


def _raise(error: OSError) -> None:
    raise error


def snapshot(root: str, skip_top: tuple[str, ...] = (GIT_DIR,)) -> dict[str, Entry]:
    """Every path below ``root`` (relative, ``/``-separated) with its entry.
    Links are recorded, never followed."""
    found: dict[str, Entry] = {}
    for directory, dirnames, filenames in os.walk(root, onerror=_raise):
        relative = os.path.relpath(directory, root)
        prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
        if not prefix:
            dirnames[:] = [name for name in dirnames if name not in skip_top]
            filenames = [name for name in filenames if name not in skip_top]
        for name in [*dirnames, *filenames]:
            entry = entry_at(os.path.join(directory, name))
            if entry is not None:
                found[prefix + name] = entry
        # A link to a directory is an entry, not a place to walk into.
        dirnames[:] = [
            name for name in dirnames if found.get(prefix + name) == Entry(KIND_DIR)
        ]
    return found


def fingerprint(
    root: str, dirs: tuple[str, ...], files: tuple[str, ...], version_rel: str
) -> str:
    """One digest of the template-owned content below ``root``: every file
    and link under ``dirs``, each of ``files``, and the version stamp.

    The lists themselves are part of it, so a path that becomes template-owned
    changes the digest. Left out: files that appear by themselves (bytecode,
    Finder's index) and the ``model:`` line of an agent definition, which the
    manifest tool renders per project.
    """
    lines = ["dirs\0" + "\0".join(dirs), "files\0" + "\0".join(files)]

    def record(name: str, entry: Entry | None) -> None:
        if entry is not None and entry.kind == KIND_FILE:
            if AGENT_FILE_RE.fullmatch(name) is not None:
                entry = entry_at(os.path.join(root, name), without_model_line=True)
        if entry is not None and entry.kind != KIND_DIR:
            mark = "x" if entry.executable else "-"
            lines.append(f"{name}\0{entry.kind}\0{mark}\0{entry.value}")

    for top in dirs:
        base = os.path.join(root, top)
        if os.path.islink(base) or not os.path.isdir(base):
            record(top, entry_at(base))
            continue
        for rel, entry in sorted(snapshot(base, skip_top=()).items()):
            if not is_junk(rel):
                record(f"{top}/{rel}", entry)
    for rel in files:
        record(rel, entry_at(os.path.join(root, rel)))
    stamp = entry_at(os.path.join(root, version_rel))
    lines.append(f"version\0{'' if stamp is None else stamp.value}")
    joined = "\n".join(lines).encode("utf-8", "surrogateescape")
    return hashlib.sha256(joined).hexdigest()


# --------------------------------------------------------------------------
# Copying out, and writing back
# --------------------------------------------------------------------------


def changed_paths(before: dict[str, Entry], after: dict[str, Entry]) -> list[str]:
    """Every path whose entry differs between two snapshots, sorted, so a
    directory comes before what is inside it."""
    return sorted(
        rel for rel in set(before) | set(after) if before.get(rel) != after.get(rel)
    )


def _require_real_parents(target: str, rel: str) -> None:
    current = target
    for part in rel.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise Conflict(f"{rel}: a directory on the way to it is a link")
        if os.path.lexists(current) and not os.path.isdir(current):
            raise Conflict(f"{rel}: a parent of it is not a directory")


def _file_mode(destination: str, executable: bool) -> int:
    """The mode of a written file: the one it replaces, or a normal new
    file's, with the executable bit as the update has it."""
    try:
        base = os.lstat(destination).st_mode & KEPT_MODE_BITS
    except FileNotFoundError:
        base = NEW_FILE_MODE
    return base | ((base & READ_BITS) >> 2) if executable else base


def _temporary(destination: str) -> str:
    name = f"{TEMP_PREFIX}{secrets.token_hex(TEMP_TOKEN_BYTES)}{TEMP_SUFFIX}"
    return os.path.join(os.path.dirname(destination), name)


def _place(source: str, destination: str, executable: bool) -> None:
    """Put one prepared file in place: written beside the destination under a
    name nobody can predict, then renamed over it. However this ends, by an
    error or an interrupt, the temporary file does not stay behind."""
    temporary = _temporary(destination)
    try:
        mode = _file_mode(destination, executable)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, common.PRIVATE_FILE_MODE)
        try:
            with open(source, "rb") as handle:
                for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                    os.write(descriptor, chunk)
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Plan:
    """Every change one update makes to a project, in the order it is made:
    files that go away, emptied directories from the deepest up, new
    directories from the top down, then the files to put in place."""

    target: str
    stage: str
    unlink: tuple[str, ...]
    rmdir: tuple[str, ...]
    mkdir: tuple[str, ...]
    place: tuple[tuple[str, bool], ...]

    def __bool__(self) -> bool:
        return bool(self.unlink or self.rmdir or self.mkdir or self.place)


def plan_changes(
    target: str, stage: str, before: dict[str, Entry], after: dict[str, Entry]
) -> Plan:
    """Work out, and prove possible, everything that makes the target's paths
    what ``after`` says. Nothing is written here.

    Raises :class:`Conflict` unless every path that differs between the two
    snapshots is, right now, what ``before`` recorded; every prepared file is
    what ``after`` recorded; and every directory that has to make room for a
    file holds nothing but what the update removes.
    """
    changed = changed_paths(before, after)
    directory = Entry(KIND_DIR)
    # Directories the update made that the project already has: the updater
    # creates a few it never fills, and those were not staged from.
    present: set[str] = set()
    for rel in changed:
        wanted, now = after.get(rel), entry_at(os.path.join(target, rel))
        if wanted is not None and wanted.kind not in (KIND_FILE, KIND_DIR):
            raise Conflict(f"{rel}: the update produced something that is not a file")
        _require_real_parents(target, rel)
        if before.get(rel) is None and wanted == directory and now == directory:
            present.add(rel)
        elif now != before.get(rel):
            raise Conflict(f"{rel}: changed since the update was prepared")
        if wanted is not None and wanted.kind == KIND_FILE:
            if entry_at(os.path.join(stage, rel)) != wanted:
                raise Conflict(f"{rel}: the prepared file is not the one checked")

    def was(rel: str) -> str | None:
        had = directory if rel in present else before.get(rel)
        return None if had is None else had.kind

    def becomes(rel: str) -> str | None:
        wanted = after.get(rel)
        return None if wanted is None else wanted.kind

    unlink = [
        rel
        for rel in changed
        if was(rel) not in (None, KIND_DIR) and becomes(rel) != KIND_FILE
    ]
    # A directory goes only when everything in it is part of the update.
    # One that holds something else stays, and so does every parent of it;
    # where a file has to take its place, that is a conflict, found now.
    rmdir: list[str] = []
    kept: list[str] = []
    for rel in reversed(changed):
        if was(rel) != KIND_DIR or becomes(rel) == KIND_DIR:
            continue
        names = os.listdir(os.path.join(target, rel))
        foreign = [name for name in names if f"{rel}/{name}" not in before]
        if foreign or any(inner.startswith(f"{rel}/") for inner in kept):
            if becomes(rel) == KIND_FILE:
                raise Conflict(f"{rel}: holds something the update did not stage")
            kept.append(rel)
        else:
            rmdir.append(rel)
    mkdir = [
        rel for rel in changed if becomes(rel) == KIND_DIR and was(rel) != KIND_DIR
    ]
    place = [
        (rel, after[rel].executable) for rel in changed if becomes(rel) == KIND_FILE
    ]
    return Plan(target, stage, tuple(unlink), tuple(rmdir), tuple(mkdir), tuple(place))


def apply_plan(plan: Plan) -> None:
    """Make the planned changes, with plain file operations; no git command
    runs. Whatever fails here fails after the first change may have been
    made, so it is raised as :class:`Interrupted`, never as a conflict."""
    try:
        for rel in plan.unlink:
            os.unlink(os.path.join(plan.target, rel))
        for rel in plan.rmdir:
            os.rmdir(os.path.join(plan.target, rel))
        for rel in plan.mkdir:
            # The job runs with a private umask; a project's new directory
            # gets a normal mode all the same.
            path = os.path.join(plan.target, rel)
            os.mkdir(path)
            os.chmod(path, NEW_DIR_MODE)
        for rel, executable in plan.place:
            _require_real_parents(plan.target, rel)
            source = os.path.join(plan.stage, rel)
            _place(source, os.path.join(plan.target, rel), executable)
    except (OSError, Conflict) as exc:
        raise Interrupted(f"{type(exc).__name__}: {exc}") from exc


def apply_changes(
    target: str, stage: str, before: dict[str, Entry], after: dict[str, Entry]
) -> None:
    """Plan and make the changes in one call. :class:`Conflict` means nothing
    was written; :class:`Interrupted` means the write stopped part-way."""
    apply_plan(plan_changes(target, stage, before, after))


def remove_leftovers(target: str, rels: tuple[str, ...]) -> int:
    """Delete the temporary files a killed write of this job left under the
    given paths, and beside them; return how many. Only names this module
    makes up are touched, and no link is followed to find them."""
    directories: set[str] = set()
    for rel in rels:
        try:
            _require_real_parents(target, rel)
        except Conflict:
            continue
        path = os.path.join(target, rel)
        directories.add(os.path.dirname(path))
        if os.path.isdir(path) and not os.path.islink(path):
            directories.update(found[0] for found in os.walk(path))
    removed = 0
    for folder in sorted(directories):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            if LEFTOVER_RE.fullmatch(name) and not os.path.isdir(path):
                os.unlink(path)
                removed += 1
    return removed


def applied_problem(
    target: str, before: dict[str, Entry], after: dict[str, Entry]
) -> str | None:
    """The first path in the target that is not what the update produced;
    None when all of them are."""
    for rel in sorted(after):
        if entry_at(os.path.join(target, rel)) != after[rel]:
            return f"{rel} is not what the update produced"
    for rel in sorted(set(before) - set(after)):
        left = entry_at(os.path.join(target, rel))
        if left is not None and left.kind != KIND_DIR:
            return f"{rel} should be gone"
    return None
