#!/usr/bin/env python3
"""The owner's side of a new project: install the approved template there,
list the project, and adopt it, in one typed confirmation.

``launchd.sh install-project <project>`` is typed at a terminal. It is
``adopt`` for a project that has no template yet, and it is the same
pipeline: the approved revision laid out in a scratch worktree, a private copy
of the project's paths, the approved **installer** (``scripts/install.sh
--yes``) run on that copy under the same fence the updater gets, the same
screen, and after a typed ``yes`` exactly the difference the installer made,
written into the project as uncommitted changes. Nothing is installed from
any other tree, and nothing of the project is executed.

Then the project is listed in ``targets.txt`` (once; a line that is already
there is not repeated), and the adoption is recorded in
``state/distribution.json``. Anything but ``yes`` changes nothing at all,
the registry included.

Until the owner commits the installed files (or keeps them out of git with
``.git/info/exclude``), the tree is not clean, and the scheduled runs treat
the project as they treat any update that is not committed yet: ``uncommitted``
while the approved template is the one written there, ``waiting-dirty`` once a
newer revision waits for it.

Exit codes: 0 installed, or declined · 1 a path that cannot be one line of
the registry, or no approved revision · 2 the project must be fixed first (not
clean, a link, already installed, the installer refused it), or it changed
while the screen was up · 3 the installation could not be prepared or
written, or the registry could not be written after it.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import adopt
import common
import distribute
import ledger
import promote
import staging
import target_git
import targets

COMMAND = "install-project"
CONFIRM_WORD = adopt.CONFIRM_WORD
SCREEN_WIDTH = adopt.SCREEN_WIDTH
VERSION_STAMP = staging.VERSION_STAMP_REL
DELETE_CHAR = 0x7F
READ_CHUNK = 1 << 16


def name_problem(raw: str) -> str | None:
    """Why ``raw`` cannot be written as one line of the registry and read
    back as the same path; None when it can."""
    if any(ord(char) < ord(" ") or ord(char) == DELETE_CHAR for char in raw):
        return "holds a control character"
    if raw != raw.strip():
        return "starts or ends with white space"
    if not targets.is_absolute(raw):
        return "is not an absolute path"
    if os.path.normpath(raw) != raw or raw.startswith("//"):
        plain = os.path.normpath(raw)
        plain = "/" + plain.lstrip("/")
        return f"is not written plainly (a trailing slash, //, . or ..); use {plain}"
    return None


def registry_problem(registry: Path) -> str | None:
    """Why the registry cannot be added to; None when it can. It is replaced,
    never written through: a link there is refused, not followed."""
    if registry.is_symlink():
        return f"{registry} is a link; it must be a real file"
    if registry.exists() and not registry.is_file():
        return f"{registry} is not a regular file"
    return None


def read_registry_bytes(registry: Path) -> bytes:
    """Every byte of the registry, read once and without following a link;
    nothing when there is none. ``OSError`` when it is a link, not a regular
    file, or cannot be read: an unreadable registry is not an empty one."""
    data = read_registry_file(registry)
    return b"" if data is None else data


def read_registry_file(registry: Path) -> bytes | None:
    """:func:`read_registry_bytes`, with None when there is no file: one open
    says both whether it exists and what it holds."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(str(registry), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(f"{registry} is a link; it must be a real file") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{registry} is not a regular file")
        chunks: list[bytes] = []
        for chunk in iter(lambda: os.read(descriptor, READ_CHUNK), b""):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def is_listed(registry: Path, target: str) -> bool:
    """Whether the registry lists ``target``, read without following a link;
    False when it cannot be read."""
    try:
        return target in targets.parse_registry(read_registry_bytes(registry))
    except OSError:
        return False


@dataclass(frozen=True)
class Added:
    """What adding one line did to the registry: the bytes before (None when
    there was no file) and the bytes written. What it takes to undo it."""

    before: bytes | None
    written: bytes


def add_to_registry(registry: Path, target: str) -> Added | None:
    """Add ``target`` to the registry on a line of its own, keeping every
    byte that is there; None when it is listed already. The file is replaced
    in one rename, so a reader never sees half of it, and a link is never
    followed. ``OSError`` when it cannot be written, or does not list the
    target afterwards.

    The run lock keeps the job's own writers apart; an edit the owner saves
    by hand in the same instant is the one thing that could be lost."""
    found = read_registry_file(registry)
    data = b"" if found is None else found
    if target in targets.parse_registry(data):
        return None
    written = data + (b"\n" if data and not data.endswith(b"\n") else b"")
    written += os.fsencode(target) + b"\n"
    common.write_bytes_atomic(registry, written)
    if target not in targets.parse_registry(read_registry_bytes(registry)):
        raise OSError("it is not listed after writing it")
    return Added(found, written)


def list_in_registry(registry: Path, target: str) -> bool:
    """:func:`add_to_registry`, for a caller that only needs to know that the
    registry lists ``target`` afterwards."""
    add_to_registry(registry, target)
    return is_listed(registry, target)


def take_back(registry: Path, added: Added) -> bool:
    """Undo :func:`add_to_registry`, but only while the registry still holds
    exactly the bytes it wrote: anything else there is somebody's edit, and
    stays. Returns whether the line is gone. The run lock keeps the job's own
    writers out; a hand edit saved in the very instant between the comparison
    and the rename is the one thing it could undo."""
    try:
        if read_registry_bytes(registry) != added.written:
            return False
        if added.before is None:
            os.unlink(str(registry))
        else:
            common.write_bytes_atomic(registry, added.before)
    except OSError:
        return False
    return True


def run(
    ctx: common.Context, raw: str, ask: adopt.Ask, out: Callable[[str], None]
) -> int:
    """Show what installing the approved revision into one project would
    change, ask for a typed word, install it, list the project and record
    that scheduled runs may keep it updated."""

    def say(text: str = "") -> None:
        out(common.safe_text(text, SCREEN_WIDTH))

    approved = promote.read_approved(ctx.paths)
    if approved is None:
        say(f"{COMMAND}: no approved revision recorded; run launchd.sh install")
        return common.EXIT_USAGE
    problem = name_problem(raw)
    if problem is not None:
        say(f"{COMMAND}: the project's path {problem}; it must fit one line of")
        say("the registry and be read back as the same path.")
        return common.EXIT_USAGE
    # Named exactly as given: the screen shows, and the registry receives,
    # the very path that is checked to be the real path of a directory.
    target = raw
    say(f"Project:   {target}")
    problem = targets.path_problem(target, ctx.paths)
    problem = problem or targets.spelling_problem(target)
    if problem is not None:
        say(f"{COMMAND}: this path {problem}")
        return common.EXIT_NEEDS_OWNER
    if os.path.lexists(os.path.join(target, VERSION_STAMP)):
        say(f"{COMMAND}: the template is already installed there ({VERSION_STAMP}).")
        say(f"Use launchd.sh adopt {target} to let the job update it.")
        return common.EXIT_NEEDS_OWNER
    registry = distribute.registry_path(ctx.paths)
    problem = registry_problem(registry)
    if problem is not None:
        say(f"{COMMAND}: {problem}")
        return common.EXIT_NEEDS_OWNER
    book = adopt.open_ledger(ctx, say, COMMAND)
    if book is None:
        return common.EXIT_NEEDS_OWNER
    git = target_git.TargetGit(ctx, target)
    try:
        with staging.open_source(ctx, approved) as source:
            return _install_from(ctx, source, git, book, registry, ask, say)
    except staging.SourceError as exc:
        say(f"{COMMAND}: {exc}")
        return common.EXIT_FAILED


def _install_from(
    ctx: common.Context,
    source: staging.Source,
    git: target_git.TargetGit,
    book: ledger.Ledger,
    registry: Path,
    ask: adopt.Ask,
    say: adopt.Say,
) -> int:
    target = git.target
    prepared = adopt.prepare(ctx, source, git, say, COMMAND, staging.INSTALL)
    if not isinstance(prepared, adopt.Prepared):
        return prepared
    listed = is_listed(registry, target)
    say(
        f"Typing {CONFIRM_WORD} also lists the project in the registry of projects"
        " the job maintains"
        + (" (it is listed already):" if listed else ", before anything is written:")
    )
    say(f"  {registry}")
    short = common.short_sha(source.revision)
    answer = ask(
        f"Type {CONFIRM_WORD} to install {short} into this project, list it, and "
        "let scheduled runs keep it updated: "
    )
    if answer.strip() != CONFIRM_WORD:
        say(f"{COMMAND}: not confirmed; nothing was changed")
        return common.EXIT_OK

    added: list[Added] = []

    def listed_first() -> bool:
        # Listed before the first change, so that a write killed half-way is
        # still one the scheduled run finds, names, and says how to recover.
        # Listed and not adopted, a project is only counted: never looked at.
        try:
            line = add_to_registry(registry, target)
            added.extend([line] if line is not None else [])
        except OSError as exc:
            ctx.log.line(f"{COMMAND}: cannot list {target}: {exc}")
            say(f"{COMMAND}: the project could not be listed in {registry}: {exc}")
            return False
        say(f"{COMMAND}: listed in {registry}.")
        return True

    code, began = adopt.write(
        ctx, source, git, book, prepared, say, COMMAND, ready=listed_first
    )
    if began and code == common.EXIT_OK:
        _name_candidates(prepared, say)
    elif began:
        say("To start again: discard what git status lists in the project")
        say(f"(git restore ., and git clean for the new files), then run {COMMAND}.")
    elif added and take_back(registry, added[0]):
        # The line went in, and then the write never began (the record of
        # adoptions could not be saved): a write that never began leaves no
        # line behind.
        say(f"{COMMAND}: the line added to {registry} was taken out again.")
    elif is_listed(registry, target):
        say(f"Nothing was written into the project; run {COMMAND} again. Listed and")
        say("not adopted, it is only counted by the scheduled runs, never looked at.")
    return code


def _name_candidates(prepared: adopt.Prepared, say: adopt.Say) -> None:
    """Name the settings merge candidates the installer wrote. They are
    ignored by git (the installer says so in .gitignore), so git status
    would not show them."""
    made = [
        rel
        for what, rel in prepared.staged.changes()
        if what == staging.CREATE and rel in staging.SETTINGS_CANDIDATES
    ]
    for rel in made:
        say(f"Merge the settings in {rel} into the project's own, then delete it.")
