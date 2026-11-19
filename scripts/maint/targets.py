#!/usr/bin/env python3
"""What the distribution step may learn about an installed project, and the
only code that writes into one.

A target is somebody's working repository. Everything in it is treated as
hostile until shown otherwise, and four rules follow from that:

* **Nothing of the target is executed.** git runs there through
  :class:`TargetGit` only: five reading subcommands, the job's hardened
  options and environment, no optional index refresh, no lazy fetch, no
  transport. Before any command that looks at the work tree, the repository's
  own configuration must consist of keys from a list of inert ones
  (:func:`config_problem`): a filter, a diff driver, an include or a promisor
  remote there is a refusal, not a risk to weigh. The updater itself never
  runs in a target; it runs on a private copy, and :func:`apply_changes`
  carries the result over.
* **git state is never written.** No function here can commit, stage, stash,
  reset or check out. :func:`git_state` hashes ``HEAD``, the index and the ref
  list so the caller can prove that afterwards.
* **Writes stay inside the project.** Every path written is checked, part by
  part, to be a real directory chain below the target: a link or a nested
  repository on the way is a refusal. Files are staged beside their
  destination under an unpredictable name and renamed into place.
* **Nothing is replaced blind.** :func:`apply_changes` first compares every
  path it is about to touch with what was staged from, and changes nothing
  when one differs.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import common

REGISTRY_FILE = "targets.txt"
GIT_DIR = ".git"
STATUS_TIMEOUT_SEC = 300
HASH_CHUNK = 1 << 20

# The only git subcommands ever run in a target. All of them read.
READ_COMMANDS = ("rev-parse", "config", "status", "ls-files", "for-each-ref")
# On top of the job's own options: `git status` never summarises submodules
# (that runs git inside them), and nothing refreshes the index as a side effect.
TARGET_GIT_OPTIONS = (
    "-c", "status.submoduleSummary=false",
    "-c", "diff.autoRefreshIndex=false",
)  # fmt: skip
# No optional lock (so no index rewrite), no fetch of a missing object, and no
# transport of any kind: the protocol list names none that exists.
TARGET_GIT_ENV = {
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_ALLOW_PROTOCOL": "none",
}
COMMAND_SCOPE = "command"

EVERY = frozenset({"*"})
NONE: frozenset[str] = frozenset()
# section -> (variables allowed without a subsection, with a subsection).
# A key is on this list because no reading command acts on it by running a
# program or by reading outside the repository. `core.hooksPath` and
# `core.fsmonitor` are here because the command line overrides both.
INERT_CONFIG: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "core": (
        frozenset(
            {
                "repositoryformatversion", "filemode", "bare", "logallrefupdates",
                "ignorecase", "precomposeunicode", "symlinks", "autocrlf", "eol",
                "safecrlf", "quotepath", "trustctime", "checkstat", "preloadindex",
                "untrackedcache", "hookspath", "fsmonitor", "sparsecheckout",
                "sparsecheckoutcone", "abbrev", "compression", "excludesfile",
                "splitindex", "sharedrepository", "commitgraph", "multipackindex",
            }
        ),
        NONE,
    ),
    "remote": (
        frozenset({"pushdefault"}),
        frozenset(
            {
                "url", "pushurl", "fetch", "push", "tagopt", "prune", "prunetags",
                "mirror", "skipdefaultupdate", "skipfetchall", "gh-resolved",
            }
        ),
    ),
    "submodule": (
        frozenset({"recurse", "active", "fetchjobs"}),
        frozenset({"url", "active", "branch", "update", "ignore"}),
    ),
    "extensions": (frozenset({"worktreeconfig", "objectformat", "refstorage"}), NONE),
    "merge": (EVERY, NONE),
    **{
        name: (EVERY, EVERY)
        for name in ("branch", "color", "gc", "maintenance", "lfs", "gpg")
    },
    **{
        name: (EVERY, NONE)
        for name in (
            "user", "author", "committer", "pull", "push", "fetch", "init", "rebase",
            "commit", "tag", "column", "advice", "gui", "github", "log", "format",
            "status", "index", "pack", "stash", "checkout", "switch", "clean",
            "rerere", "repack", "feature", "commitgraph", "receive",
        )
    },
}  # fmt: skip

# Present in the repository while git is in the middle of something.
GIT_OPERATION_MARKERS = (
    "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
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


class Conflict(Exception):
    """A path in the target is not what the update was prepared from."""


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
# git in a target: reading only
# --------------------------------------------------------------------------


class TargetGit:
    """git in one target. Reading subcommands only, with the job's hardened
    options and environment; anything else is refused before it starts."""

    def __init__(self, ctx: common.Context, target: str) -> None:
        self.target = target
        self.log = ctx.log
        self.env = {**ctx.env, **TARGET_GIT_ENV}

    def run(
        self, args: list[str], timeout: float | None = None
    ) -> common.CommandResult:
        if not args or args[0] not in READ_COMMANDS:
            raise common.Refused(
                f"git {' '.join(args[:1])} is not a reading command; "
                "nothing else runs in a target"
            )
        deadline = common.GIT_TIMEOUT_SEC if timeout is None else timeout
        options = [*common.GIT_OPTIONS, *TARGET_GIT_OPTIONS]
        argv = ["git", *options, "-C", self.target, *args]
        result = common.run_command(argv, deadline, env=self.env)
        if not result.ok:
            self.log.line(f"target git failed: {args[0]} -- {result.failure()}")
        return result

    def text(self, args: list[str]) -> str | None:
        result = self.run(args)
        return result.stdout.strip() if result.ok else None


def config_key_is_inert(key: str) -> bool:
    """Whether a configuration key (as ``git config --list`` names it) is on
    the list of keys no reading command acts on."""
    section, _, rest = key.partition(".")
    plain, scoped = INERT_CONFIG.get(section, (NONE, NONE))
    if "." in rest:
        variable, allowed = rest.rsplit(".", 1)[1], scoped
    else:
        variable, allowed = rest, plain
    return allowed == EVERY or variable in allowed


def config_problem(git: TargetGit) -> str | None:
    """Why the repository's own configuration rules it out; None when every
    key it sets is inert. The keys this job passes on the command line are
    not the repository's and are skipped."""
    listed = git.run(["config", "--list", "--show-scope", "-z"])
    if not listed.ok:
        return "has a git configuration that cannot be read"
    # Records are `scope NUL key LF value NUL`; a key cannot hold a line break.
    fields = listed.stdout.split("\0")
    for index in range(0, len(fields) - 1, 2):
        if fields[index] == COMMAND_SCOPE:
            continue
        key = fields[index + 1].split("\n", 1)[0]
        if not config_key_is_inert(key):
            return f"sets the git configuration key {key}, which the job does not allow"
    return None


def repository_problem(git: TargetGit) -> str | None:
    """Why git must not look at this work tree; None when it may. Listing the
    configuration reads files only, so it comes first."""
    problem = config_problem(git)
    if problem is not None:
        return problem
    top = git.text(["rev-parse", "--show-toplevel"])
    if top is None or os.path.realpath(top) != git.target:
        return "is not the top level of its own work tree"
    if git.text(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]) is None:
        return "has no commit yet"
    return None


def uncommitted(git: TargetGit) -> list[str] | None:
    """Every staged, unstaged or untracked-and-not-ignored path, as git
    reports it; None when git could not say. Changes inside a submodule's own
    work tree are not looked at: that would run git inside it."""
    done = git.run(
        [
            "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignore-submodules=dirty",
        ],
        timeout=STATUS_TIMEOUT_SEC,
    )  # fmt: skip
    if not done.ok:
        return None
    return [entry for entry in done.stdout.split("\0") if entry]


def tracked_paths(git: TargetGit) -> set[str] | None:
    """Every path in the index; None when git could not say."""
    done = git.run(["ls-files", "-z"])
    if not done.ok:
        return None
    return {name for name in done.stdout.split("\0") if name}


def git_state(git: TargetGit) -> str | None:
    """A digest of where ``HEAD`` points, every ref and the index, byte for
    byte. Equal digests mean nothing was committed, staged, stashed or checked
    out in between."""
    refs = git.run(["for-each-ref", "--format=%(objectname) %(refname)"])
    head = git.text(["rev-parse", "--verify", "--quiet", "HEAD"])
    if not refs.ok or head is None:
        return None
    digest = hashlib.sha256()
    digest.update(head.encode("ascii", "replace") + b"\0" + refs.stdout.encode())
    for name in ("HEAD", "index"):
        try:
            data = Path(git.target, GIT_DIR, name).read_bytes()
        except OSError:
            data = b""
        digest.update(b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


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
    except FileNotFoundError:
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


def copy_scope(target: str, stage: str, rels: tuple[str, ...]) -> None:
    """Copy each of ``rels`` that exists from the target into ``stage``.
    Links are copied as links and nothing is followed. Raises ``OSError`` or
    ``shutil.Error`` for a file that cannot be read or is not a file, a
    directory or a link."""
    for rel in rels:
        source = os.path.join(target, rel)
        if not os.path.lexists(source):
            continue
        destination = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if os.path.islink(source):
            if not os.path.lexists(destination):
                os.symlink(os.readlink(source), destination)
        elif os.path.isdir(source):
            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        elif os.path.isfile(source):
            shutil.copy2(source, destination)
        else:
            raise OSError(f"{rel} is not a file, a directory or a link")


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


def _place(source: str, destination: str, entry: Entry) -> None:
    """Put one staged file or link in place: written beside the destination
    under a name nobody can predict, then renamed over it."""
    temporary = _temporary(destination)
    try:
        if entry.kind == KIND_LINK:
            os.symlink(entry.value, temporary)
        else:
            mode = _file_mode(destination, entry.executable)
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
    except OSError:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def apply_changes(
    target: str, stage: str, before: dict[str, Entry], after: dict[str, Entry]
) -> None:
    """Make the target's paths what ``after`` says, from the files in
    ``stage``. Only paths that differ between the two snapshots are touched.

    Nothing is written unless every one of them is, right now, what ``before``
    recorded: otherwise :class:`Conflict` is raised first. Uses plain file
    operations only; no git command runs.
    """
    changed = changed_paths(before, after)
    directory = Entry(KIND_DIR)
    # Directories the update made that the project already has: the updater
    # creates a few it never fills, and those were not staged from.
    present = set()
    for rel in changed:
        wanted, now = after.get(rel), entry_at(os.path.join(target, rel))
        if wanted is not None and wanted.kind == KIND_OTHER:
            raise Conflict(f"{rel}: the update produced something that is not a file")
        _require_real_parents(target, rel)
        if before.get(rel) is None and wanted == directory and now == directory:
            present.add(rel)
        elif now != before.get(rel):
            raise Conflict(f"{rel}: changed since the update was prepared")

    def becomes(rel: str, kinds: tuple[str, ...]) -> bool:
        wanted = after.get(rel)
        return wanted is not None and wanted.kind in kinds

    def was(rel: str, kinds: tuple[str, ...]) -> bool:
        had = directory if rel in present else before.get(rel)
        return had is not None and had.kind in kinds

    plain = (KIND_FILE, KIND_LINK, KIND_OTHER)
    # What goes away first: files and links, then emptied directories from the
    # deepest up. A directory that still holds something the job did not
    # stage is left where it is.
    for rel in changed:
        if was(rel, plain) and not becomes(rel, plain):
            _require_real_parents(target, rel)
            os.unlink(os.path.join(target, rel))
    for rel in reversed(changed):
        if was(rel, (KIND_DIR,)) and not becomes(rel, (KIND_DIR,)):
            try:
                os.rmdir(os.path.join(target, rel))
            except OSError:
                if after.get(rel) is not None:
                    raise
    for rel in changed:
        _require_real_parents(target, rel)
        destination = os.path.join(target, rel)
        wanted = after.get(rel)
        if wanted is None:
            continue
        if wanted.kind == KIND_DIR:
            if not was(rel, (KIND_DIR,)):
                # The job runs with a private umask; a project's new
                # directory gets a normal mode all the same.
                os.mkdir(destination)
                os.chmod(destination, NEW_DIR_MODE)
        else:
            _place(os.path.join(stage, rel), destination, wanted)


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
