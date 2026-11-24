#!/usr/bin/env python3
"""Shared plumbing for the fork's local maintenance job.

Everything a step needs and no step should write twice: where the job keeps
its state, the guard that keeps git inside the clone the job owns, a
subprocess call with a deadline, the run log and lock, and the GitHub helper
that can only ever name the fork.

Properties enforced here rather than left to each caller:

* **One repository.** ``FORK_SLUG`` is the only place the fork is named. Every
  ``gh`` call is built in ``gh.py``, which adds ``--repo`` (or a
  ``repos/<fork>/`` REST path) itself; no caller passes a repository.
* **One working directory.** :class:`Git` runs only in the dedicated clone
  under the state root, or in a scratch worktree beside it, and refuses any
  other directory: the developer's checkout is never a target.
* **Fully qualified refs.** The default branch is always named
  ``refs/remotes/origin/main``. A short name is resolved by git through tags
  first, so a tag called ``origin/main`` would stand in for the branch.
* **No ambient git configuration.** Every git process runs without the
  user's global and system configuration and without inherited ``GIT_*``
  variables, with hooks, filesystem monitors and attribute files switched
  off. Identity and the credential helper are passed on the command line.
* **Nothing from outside is printed raw.** :func:`safe_text` turns every
  character that is not printable ASCII into a visible escape, so text read
  from git, GitHub or the catalog cannot drive a terminal.

Standard library only; runs on Python 3.9.

Exit codes shared by every maintenance script:
    0  ok, or nothing to do
    1  usage or configuration error (including a refused working directory)
    2  a human must act
    3  failure: a tool, the network, or authentication
"""

from __future__ import annotations

import calendar
import fcntl
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import gh

# The one place the fork is named. Owner, URLs and every `gh` target derive
# from it, so nothing in this directory can address the upstream repository.
FORK_SLUG = "ToRainu256/claude-code-orchestra"
FORK_OWNER = FORK_SLUG.split("/", 1)[0]
GITHUB_HOST = "github.com"
# What `--repo` is given. The host is part of it, so an inherited GH_HOST
# cannot turn the same owner and name into a repository somewhere else.
FORK_REPO = f"{GITHUB_HOST}/{FORK_SLUG}"
FORK_WEB_URL = f"https://{GITHUB_HOST}/{FORK_SLUG}"
FORK_GIT_URL = f"{FORK_WEB_URL}.git"
REMOTE = "origin"
DEFAULT_BRANCH = "main"
TRACKING_PREFIX = f"refs/remotes/{REMOTE}/"
MAIN_REF = f"{TRACKING_PREFIX}{DEFAULT_BRANCH}"
APPROVED_REF = "refs/heads/approved"
# Set in the clone's own configuration by `launchd.sh install`. A directory in
# the right place is not enough to be reset and cleaned: it must carry this.
OWNERSHIP_KEY = "orchestra.maintclone"
OWNERSHIP_VALUE = "true"

STATE_ROOT_ENV = "ORCHESTRA_MAINT_HOME"
CHECKOUT_FAILED_ENV = "ORCHESTRA_MAINT_CHECKOUT_FAILED"
DEFAULT_STATE_ROOT_PARTS = (".claude", "orchestra")
CLONE_DIR = "fork"
STATE_DIR = "state"
RUN_DIR = "run"
LOGS_DIR_PARTS = ("logs", "maint")
BIN_DIR = "bin"
LAUNCHER_NAME = "orchestra-maint"
APPROVED_FILE = "approved-rev"
LAST_RUN_FILE = "last-run.json"
BUMP_RECORDS_FILE = "bump-records.json"
LAUNCHER_FAILURE_FILE = "launcher-failure"
LOCK_FILE = "maint.lock"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NEEDS_OWNER = 2
EXIT_FAILED = 3

GIT_TIMEOUT_SEC = 60
KILL_GRACE_SEC = 5
LOGS_KEPT = 60
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
# The longest piece of outside text shown or logged in one go.
DISPLAY_LIMIT = 400
LOGGED_ARGS = 6
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHORT_SHA_LENGTH = 12
SECONDS_PER_DAY = 86400
PRINTABLE_FIRST, PRINTABLE_LAST = 0x20, 0x7E

# Environment that re-aims `gh` at another host or repository. Never passed on.
SCRUBBED_ENV = ("GH_HOST", "GH_REPO")
GIT_ENV_PREFIX = "GIT_"
# Set for every child after all inherited GIT_* variables are dropped: no
# user or system configuration, and never a prompt.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
BOT_NAME = "orchestra-maint"
BOT_EMAIL = f"{FORK_OWNER}@users.noreply.{GITHUB_HOST}"
# Passed to every git process. With no global configuration there is no
# identity and no credential helper, so both are given here: commits carry
# the bot identity, and a push to the fork asks `gh` for the login it
# already holds (the empty value first clears any helper list).
GIT_OPTIONS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.quotePath=true",
    "-c", "commit.gpgsign=false",
    "-c", "submodule.recurse=false",
    "-c", "advice.detachedHead=false",
    "-c", f"user.name={BOT_NAME}",
    "-c", f"user.email={BOT_EMAIL}",
    "-c", "credential.helper=",
    "-c", f"credential.https://{GITHUB_HOST}.helper=!gh auth git-credential",
)  # fmt: skip

LABEL_NEEDS_OWNER = "automation:needs-owner"
LABEL_STATUS = "automation:status"
# name -> (color, description). Created when missing; never edited afterwards.
LABELS = {
    LABEL_NEEDS_OWNER: ("d93f0b", "An automation job needs a decision"),
    LABEL_STATUS: ("0e8a16", "The automation status dashboard"),
}
# How every issue, comment and pull request body written by a job starts.
# Jobs act under the owner's GitHub account, so an author cannot tell the
# owner's words from a job's; the first characters of the body can. The
# issue worker refuses any issue whose body starts with this prefix.
JOB_MARKER_PREFIX = "<!-- job:"
LOCAL_JOB = "local-maint"
PULL_URL_RE = re.compile(re.escape(FORK_WEB_URL) + r"/pull/([1-9][0-9]{0,8})")
ISSUE_URL_RE = re.compile(re.escape(FORK_WEB_URL) + r"/issues/([1-9][0-9]{0,8})")
GITHUB_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# A model slug is <vendor>-<version>-<family>. Nothing here names a vendor or
# a family: both are read from the manifest's current value at runtime. The
# pattern cannot match a quote, whitespace, a slash or a leading dash, which
# is what makes a slug safe as an argv element, a branch name and a TOML
# value. Applied with fullmatch because `$` would accept a trailing newline.
SLUG_RE = re.compile(
    r"([a-z]{1,16})-([0-9]{1,4}(?:\.[0-9]{1,4}){0,3})-([a-z][a-z0-9]{0,31})"
)
EFFORT_RE = re.compile(r"[a-z]{1,16}")
# Any model value the manifest may hold, slug-shaped or not. It is read from
# a file on the fork and ends up in issue titles and log lines, so it is
# held to the same characters the manifest tool itself allows.
MODEL_VALUE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class Refused(Exception):
    """The job was asked to work somewhere it does not own."""


@dataclass(frozen=True)
class Paths:
    """Every location the job reads or writes, all below one state root."""

    state_root: Path

    @property
    def clone(self) -> Path:
        return self.state_root / CLONE_DIR

    @property
    def state_dir(self) -> Path:
        return self.state_root / STATE_DIR

    @property
    def run_dir(self) -> Path:
        return self.state_root / RUN_DIR

    @property
    def logs_dir(self) -> Path:
        return self.state_root.joinpath(*LOGS_DIR_PARTS)

    @property
    def approved_file(self) -> Path:
        return self.state_dir / APPROVED_FILE

    @property
    def last_run_file(self) -> Path:
        return self.state_dir / LAST_RUN_FILE

    @property
    def bump_records_file(self) -> Path:
        return self.state_dir / BUMP_RECORDS_FILE

    @property
    def launcher_failure_file(self) -> Path:
        return self.state_dir / LAUNCHER_FAILURE_FILE

    @property
    def lock_file(self) -> Path:
        return self.run_dir / LOCK_FILE

    @property
    def launcher(self) -> Path:
        return self.state_root / BIN_DIR / LAUNCHER_NAME


def resolve_state_root(flag: Path | None, env: dict[str, str]) -> Path:
    """The state root: the flag, then the environment, then the home default."""
    if flag is not None:
        return flag
    from_env = env.get(STATE_ROOT_ENV, "")
    if from_env:
        return Path(from_env)
    return Path.home().joinpath(*DEFAULT_STATE_ROOT_PARTS)


def safe_text(text: str, limit: int = DISPLAY_LIMIT) -> str:
    """``text`` as printable ASCII, bounded.

    Commit subjects, path names, command output and anything GitHub or the
    catalog returns are written by someone else. An escape sequence in them
    can clear the screen or hide a line, and a bidirectional control can
    reorder one, right above a prompt the owner answers. Every such character
    becomes a visible ``\\xNN`` or ``\\uNNNN`` instead of being sent.
    """
    shown: list[str] = []
    for char in text:
        code = ord(char)
        if PRINTABLE_FIRST <= code <= PRINTABLE_LAST:
            shown.append(char)
        elif code <= 0xFF:
            shown.append(f"\\x{code:02x}")
        else:
            shown.append(f"\\u{code:04x}")
    flat = "".join(shown)
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def one_line(text: str, limit: int = DISPLAY_LIMIT) -> str:
    """Collapse whitespace, escape and cut: a value cannot forge a line."""
    return safe_text(" ".join(text.split()), limit)


@dataclass(frozen=True)
class CommandResult:
    """What one external command did. ``returncode`` is None when it never
    started or was killed at its deadline."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def failure(self) -> str:
        """One safe line saying why the command did not succeed."""
        if self.missing:
            return f"{self.argv[0]}: could not be started"
        if self.timed_out:
            return f"{self.argv[0]}: exceeded its deadline"
        return f"{self.argv[0]}: exit {self.returncode}: {one_line(self.stderr)}"


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the command and everything it started."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def _decode(data: bytes | None) -> str:
    """Bytes as text with nothing translated: a carriage return stays one."""
    return (data or b"").decode("utf-8", errors="replace")


def run_command(
    argv: list[str],
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run one command with a deadline. Never a shell, stdin always closed.

    The command gets its own process group, and the whole group is killed at
    the deadline: a child that leaves a grandchild holding the pipe would
    otherwise keep the caller waiting past it. Output is read as bytes, so
    line endings reach the caller exactly as the command wrote them.
    """
    frozen = tuple(argv)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=None if cwd is None else str(cwd),
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandResult(frozen, None, "", str(exc), missing=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        try:
            stdout, stderr = process.communicate(timeout=KILL_GRACE_SEC)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
        return CommandResult(
            frozen, None, _decode(stdout), _decode(stderr), timed_out=True
        )
    return CommandResult(frozen, process.returncode, _decode(stdout), _decode(stderr))


def utc_stamp(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def utc_display(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


def parse_github_time(text: object) -> float | None:
    """Epoch seconds of a GitHub timestamp, or None when it is not one."""
    if not isinstance(text, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(text, GITHUB_TIME_FORMAT)))
    except ValueError:
        return None


class Log:
    """The run log: one stamped line per event, appended, private."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def line(self, text: str) -> None:
        if self.path is None:
            return
        entry = f"{utc_stamp(time.time())} {one_line(text)}\n"
        descriptor = os.open(
            str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE_MODE
        )
        try:
            os.write(descriptor, entry.encode("utf-8"))
        finally:
            os.close(descriptor)

    def command(self, result: CommandResult) -> None:
        """Record a command by its first few arguments and its outcome. git's
        fixed options are left out; issue and pull-request bodies come last
        and are cut off with the rest."""
        program, rest = result.argv[0], result.argv[1:]
        shown = rest[len(GIT_OPTIONS) :] if program == "git" else rest
        head = " ".join([program, *shown[:LOGGED_ARGS]])
        if result.ok:
            self.line(f"ran: {head}")
            return
        self.line(f"failed: {head} -- {result.failure()}")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)


def open_run_log(paths: Paths, now: float) -> Log:
    """Create this run's log and drop all but the newest ``LOGS_KEPT``."""
    ensure_private_dir(paths.logs_dir)
    log = Log(paths.logs_dir / f"{utc_stamp(now)}.log")
    existing = sorted(paths.logs_dir.glob("*.log"))
    surplus = len(existing) - (LOGS_KEPT - 1)
    for old in existing[: max(surplus, 0)]:
        old.unlink()
    return log


def acquire_lock(paths: Paths) -> int | None:
    """Take the run lock without waiting. None means another run holds it."""
    ensure_private_dir(paths.run_dir)
    descriptor = os.open(
        str(paths.lock_file), os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` in one rename, so a reader never sees half a file."""
    ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    descriptor = os.open(
        str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE
    )
    try:
        os.write(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(str(temporary), str(path))


def child_env(env: dict[str, str]) -> dict[str, str]:
    """The environment every child gets: nothing that re-aims git or ``gh``.

    Inherited ``GIT_*`` variables can point git at another repository, index
    or configuration; they are all dropped, and the user's own configuration
    is switched off.
    """
    kept = {
        name: value
        for name, value in env.items()
        if not name.startswith(GIT_ENV_PREFIX) and name not in SCRUBBED_ENV
    }
    return {**kept, **GIT_ENV}


def require_scratch(paths: Paths, path: Path) -> Path:
    """Return ``path`` only when it lies strictly below the run directory.

    The run directory is taken from the resolved state root, not resolved
    itself: a run directory that is a link to somewhere else makes every path
    under it fail here instead of quietly moving the scratch area.
    """
    run_root = paths.state_root.resolve() / RUN_DIR
    if run_root not in path.resolve().parents:
        raise Refused(f"{path} is not a scratch path under {run_root}")
    return path


def require_real_layout(paths: Paths) -> None:
    """Refuse when anything the job manages is a link. Each managed path must
    be the real thing inside the state root: a link would send a lock, a log,
    a forced checkout or the next launcher start somewhere else."""
    managed = (
        paths.clone,
        paths.clone / ".git",
        paths.state_dir,
        paths.run_dir,
        paths.logs_dir.parent,
        paths.logs_dir,
        paths.launcher.parent,
        paths.launcher,
        paths.approved_file,
        paths.last_run_file,
        paths.bump_records_file,
        paths.launcher_failure_file,
        paths.lock_file,
    )
    for path in managed:
        if path.is_symlink():
            raise Refused(f"{path} is a link; it must be a real path in the state root")


class Git:
    """git, confined to the clone this job owns and its scratch worktrees,
    and cut off from the user's configuration and environment."""

    def __init__(self, paths: Paths, env: dict[str, str], log: Log) -> None:
        self.paths = paths
        self.env = env
        self.log = log

    def _require_owned(self, where: Path) -> None:
        clone = self.paths.clone
        expected = self.paths.state_root.resolve() / CLONE_DIR
        if clone.resolve() != expected:
            raise Refused(f"{clone} resolves outside the state root")
        if where.resolve() == expected:
            return
        require_scratch(self.paths, where)

    def run(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        where = self.paths.clone if cwd is None else cwd
        self._require_owned(where)
        deadline = GIT_TIMEOUT_SEC if timeout is None else timeout
        argv = ["git", *GIT_OPTIONS, "-C", str(where), *args]
        result = run_command(argv, deadline, env=self.env)
        self.log.command(result)
        return result

    def text(self, args: list[str], cwd: Path | None = None) -> str | None:
        """Stripped stdout of a git command, or None when it failed."""
        result = self.run(args, cwd)
        return result.stdout.strip() if result.ok else None

    def commit_of(self, name: str) -> str | None:
        """The commit a fully qualified ref or a SHA names, or None."""
        sha = self.text(["rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"])
        return sha if sha is not None and SHA_RE.fullmatch(sha) else None

    def show(self, revision: str, rel: str) -> str | None:
        """A file's content at a revision. Data only: nothing is checked out."""
        result = self.run(["show", f"{revision}:{rel}"])
        return result.stdout if result.ok else None


def remove_scratch_worktree(git: Git, tree: Path) -> None:
    """Drop a scratch worktree of the clone and forget it. Safe when there is
    none, and refused for anything that is not below the run directory."""
    checked = require_scratch(git.paths, tree)
    if checked.exists():
        git.run(["worktree", "remove", "--force", str(checked)])
    if checked.exists():
        shutil.rmtree(str(checked))
    git.run(["worktree", "prune"])


def unexpected_refs(git: Git) -> list[str] | None:
    """Refs the clone should not have, or None when they cannot be listed.

    The clone holds the local branch ``approved`` and remote-tracking refs of
    the fork, nothing else. A tag, or a branch named like a remote-tracking
    ref, is how a short name would resolve to a commit that was never on the
    default branch.
    """
    listed = git.text(["for-each-ref", "--format=%(refname)"])
    if listed is None:
        return None
    return [
        ref
        for ref in listed.split("\n")
        if ref and ref != APPROVED_REF and not ref.startswith(TRACKING_PREFIX)
    ]


def require_owned_clone(git: Git) -> None:
    """Refuse unless the clone is the fork's and the job's own: marked at
    install, at its own top level, with one remote whose fetch and push both
    go to the fork, and with no ref that could shadow the default branch.
    The checks a destructive or publishing step depends on."""
    clone = git.paths.clone
    require_real_layout(git.paths)
    if not clone.is_dir():
        raise Refused(f"{clone} does not exist; run launchd.sh install")
    if not (clone / ".git").is_dir():
        raise Refused(f"{clone} must hold its own repository, not point at one")
    top = git.text(["rev-parse", "--show-toplevel"])
    if top is None or Path(top).resolve() != clone.resolve():
        raise Refused(f"{clone} is not the top level of a git work tree")
    if git.text(["config", "--get", OWNERSHIP_KEY]) != OWNERSHIP_VALUE:
        raise Refused(f"{clone} was not created by launchd.sh install")
    if git.text(["config", "--get", f"remote.{REMOTE}.url"]) != FORK_GIT_URL:
        raise Refused(f"{clone} is not a clone of {FORK_GIT_URL}")
    # The stored URL is not where git goes when a rewrite rule or a separate
    # push URL exists; ask git where it would actually fetch from and push to.
    for direction in ([], ["--push"]):
        urls = git.text(["remote", "get-url", *direction, "--all", REMOTE])
        if urls is None or urls.split("\n") != [FORK_GIT_URL]:
            raise Refused(f"{clone} does not fetch from and push to {FORK_GIT_URL}")
    remotes = git.text(["remote"])
    if remotes is None or remotes.split() != [REMOTE]:
        raise Refused(f"{clone} must have exactly one remote, {REMOTE}")
    extra = unexpected_refs(git)
    if extra is None or extra:
        named = one_line(", ".join(extra or ["(unreadable)"]))
        raise Refused(f"{clone} holds refs the job never creates: {named}")


@dataclass(frozen=True)
class Slug:
    """A parsed model slug. ``version`` has trailing zeros removed, so tuples
    compare numerically: 6 < 6.1 < 6.9 < 6.10, and 6 equals 6.0."""

    text: str
    vendor: str
    version: tuple[int, ...]
    family: str

    def same_line(self, other: Slug) -> bool:
        return self.vendor == other.vendor and self.family == other.family

    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)


def parse_slug(text: object) -> Slug | None:
    """Parse an untrusted value as a slug. Anything else is None."""
    if not isinstance(text, str):
        return None
    match = SLUG_RE.fullmatch(text)
    if match is None:
        return None
    parts = [int(part) for part in match.group(2).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return Slug(text, match.group(1), tuple(parts), match.group(3))


@dataclass(frozen=True)
class StepResult:
    """One step's outcome. ``result`` and the keys of ``counts`` are public
    words; ``detail`` is free text and stays on this machine."""

    name: str
    result: str
    exit_code: int
    detail: str = ""
    counts: dict[str, int] = field(default_factory=dict)


def checked_result(
    outcomes: dict[str, tuple[int, ...]],
    name: str,
    result: str,
    exit_code: int,
    detail: str = "",
    counts: dict[str, int] | None = None,
) -> StepResult:
    """A step result, only when the step declared it. Each module lists the
    outcomes it can report and their exit codes; the README table is pinned
    to those lists, so an outcome cannot appear without being documented."""
    if exit_code not in outcomes.get(result, ()):
        raise ValueError(f"{name}: undeclared outcome {result!r} with exit {exit_code}")
    return StepResult(name, result, exit_code, detail, dict(counts or {}))


@dataclass(frozen=True)
class Context:
    """What every step is handed. ``tools_root`` is the approved checkout
    whose code is running: the only repository code a step may execute."""

    paths: Paths
    tools_root: Path
    env: dict[str, str]
    log: Log
    now: float
    git: Git
    github: gh.GitHub


def make_context(
    paths: Paths,
    tools_root: Path,
    log: Log,
    env: dict[str, str] | None = None,
    now: float | None = None,
) -> Context:
    # Imported here, not at the top: gh.py builds on this module.
    import gh

    clean = child_env(dict(os.environ if env is None else env))
    return Context(
        paths=paths,
        tools_root=tools_root,
        env=clean,
        log=log,
        now=time.time() if now is None else now,
        git=Git(paths, clean, log),
        github=gh.GitHub(clean, log),
    )


def job_marker(job: str, detail: str = "") -> str:
    """The first line of a body written by ``job``."""
    return f"{JOB_MARKER_PREFIX}{job}{' ' + detail if detail else ''} -->"


def short_sha(value: object) -> str:
    if isinstance(value, str) and SHA_RE.fullmatch(value):
        return value[:SHORT_SHA_LENGTH]
    return "unknown"
