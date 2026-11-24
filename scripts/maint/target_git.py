#!/usr/bin/env python3
"""git in somebody else's repository: reading only, and trusting none of it.

A project's repository is data. Its hooks, its configuration, its attribute
files and its index were written by whoever cloned or committed there, so:

* **Only reading subcommands run** (``READ_COMMANDS``), with the job's
  hardened options and environment, no optional index refresh, no lazy fetch
  and no transport. :class:`TargetGit` refuses to start any other.
* **The repository's own configuration is vetted first.** Before any command
  looks at the work tree, every key the repository sets must be on a list of
  inert ones (:func:`config_problem`). A filter, a diff driver, an include or
  a promisor remote is a refusal, not a risk to weigh.
* **Clean is decided here, with what git cannot see added.** :func:`dirt`
  reports what ``git status`` lists and, beside it, every index entry whose
  changes git was told not to look for.
* **Nothing is written.** :func:`git_state` hashes ``HEAD``, every ref and the
  index so a caller can prove that around a write of its own.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import common

GIT_DIR = ".git"
STATUS_TIMEOUT_SEC = 300

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

UNTRACKED_MARK = "??"
RENAME_MARKS = frozenset("RC")
# How `git ls-files -v` tags an entry whose changes git was told not to look
# for: assume-unchanged (a lower-case letter) or skip-worktree.
HIDDEN_TAGS = ("h", "S", "s")
RECORD_WIDTH = 100


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


@dataclass(frozen=True)
class Dirt:
    """What keeps a tree from being clean, as git records name it: paths
    changed or staged, paths untracked and not ignored, and paths whose
    changes git was told not to look for (assume-unchanged, skip-worktree).
    The last kind never shows in ``git status``; nothing can vouch for it."""

    changed: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.changed or self.untracked or self.hidden)

    @property
    def visible(self) -> tuple[str, ...]:
        """What ``git status`` itself lists."""
        return (*self.changed, *self.untracked)

    def summary(self) -> str:
        """Counts only: fit for a line that leaves this machine."""
        return (
            f"{len(self.changed)} modified or staged, {len(self.untracked)} "
            f"untracked, {len(self.hidden)} hidden from git status"
        )


def shown(record: str) -> str:
    """One git record or path as a short, safe piece of a line."""
    return common.one_line(record, RECORD_WIDTH)


def index_paths(git: TargetGit) -> tuple[set[str], set[str]] | None:
    """(every path in the index, those among them whose changes git was told
    not to look for); None when git could not say."""
    done = git.run(["ls-files", "-v", "-z"])
    if not done.ok:
        return None
    tracked: set[str] = set()
    hidden: set[str] = set()
    for record in done.stdout.split("\0"):
        tag, _, name = record.partition(" ")
        if not name:
            continue
        tracked.add(name)
        if tag in HIDDEN_TAGS:
            hidden.add(name)
    return tracked, hidden


def dirt(git: TargetGit) -> Dirt | None:
    """Everything that keeps the tree from being clean; None when git could
    not say. Changes inside a submodule's own work tree are not looked at:
    that would run git inside it."""
    done = git.run(
        [
            "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignore-submodules=dirty",
        ],
        timeout=STATUS_TIMEOUT_SEC,
    )  # fmt: skip
    index = index_paths(git)
    if not done.ok or index is None:
        return None
    changed: list[str] = []
    untracked: list[str] = []
    records = iter(done.stdout.split("\0"))
    for record in records:
        if not record:
            continue
        if record.startswith(UNTRACKED_MARK):
            untracked.append(record)
            continue
        changed.append(record)
        if set(record[:2]) & RENAME_MARKS:
            # A rename or copy names its origin in a record of its own.
            next(records, None)
    return Dirt(tuple(changed), tuple(untracked), tuple(sorted(index[1])))


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
