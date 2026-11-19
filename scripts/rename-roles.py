#!/usr/bin/env python3
"""Rename the two general-purpose roles to names that do not state a model.

    general-purpose-sonnet -> general-purpose-standard
    general-purpose-opus   -> general-purpose-deep

A role name is what every router, hook hint and skill shows the routing agent,
so a name that states a model is wrong as soon as the model manifest assigns
another one. This script is the whole rename: it moves the two agent
definitions and rewrites every reference, and it can be run again at any time
(after bringing in text that still uses the old names, for example) because a
second run finds nothing left to change.

It is a maintenance tool for this template repository only. It is not in the
allow-lists of ``scripts/install.sh`` or ``scripts/update.sh``, so it is never
copied into a project: there the agents arrive already renamed.

Modes:

* default (check): report what ``--apply`` would change and write nothing.
* ``--apply``: move the agent definitions (``git mv`` when tracked) and rewrite
  both names in every tracked or untracked-but-not-ignored text file outside
  ``ALLOWLIST``.

What it will not do:

* Guess at a file it cannot read as text. A NUL byte or invalid UTF-8 (UTF-16
  included) is also how a file would be hidden from the rename, so such a file
  is reported as ``unreadable``: check fails, and ``--apply`` refuses and
  changes nothing. The files that are binary on purpose are named in
  ``BINARY_ALLOWLIST``.
* Write through a symlink. A symlink, or a file below a symlinked directory,
  is listed under ``symlinks`` and left exactly as it is: what it points at may
  not be this repository's file.
* Work from anywhere but the top level of the repository. Below it git lists
  paths relative to the subdirectory, and ``ALLOWLIST`` would match nothing.
* Resolve merge conflicts. It never reads or writes the index of a merge in
  progress; taking in a branch that still uses the old names is a separate
  job.

``--apply`` reads every file before it changes any, so a file it cannot read
stops the run with nothing moved. A run that fails later, while writing, is
completed by running it again.

Stdout carries exactly one JSON object; the same findings are repeated on
stderr, one line each.

Usage:
    python3 scripts/rename-roles.py
    python3 scripts/rename-roles.py --apply
    python3 scripts/rename-roles.py --project-root DIR --apply

Exit codes:
    0  nothing to do, or everything renamed
    1  bad arguments, or the project root is not the top level of a git work
       tree
    2  check found something to rename; or a rename target already exists, a
       definition sits behind a symlink, or a file is not readable as text
       (then --apply changes nothing)
    3  git failed, or a file could not be read or written
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

PROJECT_ROOT = Path(__file__).resolve().parent.parent

RENAMES = {
    "general-purpose-sonnet": "general-purpose-standard",
    "general-purpose-opus": "general-purpose-deep",
}
# A name is rewritten only where it stands alone: `my-general-purpose-opus`
# and `general-purpose-opus-plus` are different identifiers.
ROLE_RE = re.compile(
    r"(?<![A-Za-z0-9-])(" + "|".join(map(re.escape, RENAMES)) + r")(?![A-Za-z0-9-])"
)
AGENTS_DIR = ".claude/agents"
FILE_RENAMES = {
    f"{AGENTS_DIR}/{old}.md": f"{AGENTS_DIR}/{new}.md" for old, new in RENAMES.items()
}

# Paths this script never rewrites, each with the reason. An entry ending in
# "/" is a directory; any other entry is one file. These are exactly the paths
# models_lint.py excuses from its legacy-role rule (a test pins that), so the
# gate never asks for an edit this script refuses to make.
ALLOWLIST = (
    ("TEMPLATE_DESIGN_LOG.md", "design log: records the names in use at the time"),
    (".claude/docs/reviews/", "review notes quote what the reviewed tree said"),
    (".claude/docs/research/", "research notes quote the tree they measured"),
    (".claude/docs/plans/", "plans name the state they set out to change"),
    ("scripts/rename-roles.py", "this script has to name what it renames"),
    (
        ".claude/skills/_shared/models.py",
        "names the old roles, to refuse them in an override and for the lint",
    ),
    ("tests/test_rename_roles.py", "pins this script's old-to-new mapping"),
    ("tests/test_models_lint.py", "pins the lint rule and the legacy-key refusal"),
)
# Files that are binary on purpose, each with the reason (the same list as
# models_lint.py; a test pins that). Any other file that is not text is
# reported instead of skipped.
BINARY_ALLOWLIST = (("summary.png", "the image the README shows"),)

# Variables that point git at another repository, index or work tree. Run from
# inside a hook or a wrapper, this script must still act on *this* tree.
GIT_ENV_DROPPED = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
MODE_BITS = 0o777
TEMP_PREFIX = ".rename-roles."
TEMP_SUFFIX = ".rename-roles-tmp"

EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_PENDING = 2
EXIT_FAILED = 3


class GitError(Exception):
    """A git command this script depends on failed."""


@dataclass(frozen=True)
class Scan:
    """What the tree holds, read once before anything is changed."""

    rewrites: dict[str, int]
    unreadable: list[str]
    symlinks: list[str]


def is_allowlisted(rel: str) -> bool:
    """True when *rel* is history or tooling that must keep the old names."""
    for entry, _reason in ALLOWLIST:
        if entry.endswith("/"):
            if rel.startswith(entry):
                return True
        elif rel == entry:
            return True
    return False


def rename_text(text: str) -> tuple[str, int]:
    """Return *text* with both role names replaced, and how many were."""
    return ROLE_RE.subn(lambda match: RENAMES[match.group(1)], text)


def decode_text(data: bytes) -> str | None:
    """*data* as text, or None when it cannot be read as text.

    A NUL byte is valid UTF-8, so decoding alone would let a binary file
    through to be rewritten; it is the test git itself applies.
    """
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# =============================================================================
# git
# =============================================================================


def git(
    root: Path, *args: str, ok_codes: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess[bytes]:
    """Run git in *root*; a status outside *ok_codes* is a GitError."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROPPED}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise GitError(f"git could not be run: {exc}") from exc
    if result.returncode not in ok_codes:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} exited {result.returncode}: {detail}")
    return result


def _split_nul(data: bytes) -> list[str]:
    return [os.fsdecode(entry) for entry in data.split(b"\0") if entry]


def top_level_problem(root: Path) -> str | None:
    """Why *root* cannot be worked on, or None when it is a repository's top."""
    result = git(root, "rev-parse", "--show-toplevel", ok_codes=(0, 128))
    if result.returncode != 0:
        return f"{root} is not inside a git work tree"
    top = os.fsdecode(result.stdout).rstrip("\n")
    # samefile, not a string comparison: the same directory can be spelled
    # through a symlink or, on a case-insensitive filesystem, in another case.
    if not os.path.samefile(top, root):
        return f"{root} is not the top level of its git work tree ({top})"
    return None


def tracked_files(root: Path) -> set[str]:
    return set(_split_nul(git(root, "ls-files", "-z", "--cached").stdout))


def candidate_files(root: Path) -> list[str]:
    """Every tracked file, plus untracked files git does not ignore."""
    listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted(set(_split_nul(listed.stdout)))


# =============================================================================
# check / apply
# =============================================================================


def through_symlink(root: Path, rel: str) -> bool:
    """True when *rel* is a symlink or lies below a symlinked directory."""
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _write_bytes(path: Path, data: bytes) -> None:
    """Replace the regular file *path* atomically, keeping its permission bits.

    The temporary file is created exclusively under a fresh name, so nothing
    that already exists beside *path* (a file, or a symlink to one) can be
    overwritten or written through.
    """
    mode = path.stat().st_mode & MODE_BITS
    # A short fixed prefix: built from the file's own name, the temporary name
    # could exceed the filesystem's limit where the file's name does not.
    handle, name = tempfile.mkstemp(
        dir=path.parent, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX
    )
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def pending_moves(
    root: Path, candidates: list[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Agent definitions still under an old name, and the ones that cannot move.

    Only a regular file that git lists is moved. Anything else at an old path
    (a directory, a file git ignores, one inside a nested repository) is not
    this repository's definition, and is reported instead of renamed.
    """
    listed = set(candidates)
    moves: list[tuple[str, str]] = []
    blocked: list[str] = []
    for old, new in FILE_RENAMES.items():
        if not os.path.lexists(root / old):
            continue
        if through_symlink(root, old) or through_symlink(root, new):
            reason = "is a symlink or sits behind one; rename what it points at by hand"
        elif not (root / old).is_file():
            reason = "is not a regular file; move it by hand"
        elif old not in listed:
            reason = (
                "exists, but git does not list it (ignored, or in a nested repository)"
            )
        elif os.path.lexists(root / new):
            reason = f"and {new} both exist; merge them by hand and delete {old}"
        else:
            moves.append((old, new))
            continue
        blocked.append(f"{old} {reason}")
    return moves, blocked


def scan(root: Path, candidates: list[str]) -> Scan:
    """Read every candidate file once: what to rewrite, and what cannot be."""
    binary_ok = {path for path, _reason in BINARY_ALLOWLIST}
    rewrites: dict[str, int] = {}
    unreadable: list[str] = []
    symlinks: list[str] = []
    for rel in candidates:
        if is_allowlisted(rel):
            continue
        if through_symlink(root, rel):
            symlinks.append(rel)
            continue
        path = root / rel
        if not os.path.lexists(path) or path.is_dir():
            # Deleted in the work tree, or a directory git lists as one entry
            # (a nested repository): no file of this repository to rewrite.
            continue
        if not path.is_file():
            # A FIFO, socket or device: reading it could block for ever.
            unreadable.append(rel)
            continue
        text = decode_text(path.read_bytes())
        if text is None:
            if rel not in binary_ok:
                unreadable.append(rel)
            continue
        count = rename_text(text)[1]
        if count:
            rewrites[rel] = count
    return Scan(rewrites, unreadable, symlinks)


def _payload(mode: str, result: str, **fields: Any) -> dict[str, Any]:
    """The run's JSON object; every key is present on every outcome."""
    ok = result in ("clean", "applied", "no-op")
    base: dict[str, Any] = {
        "ok": ok,
        "mode": mode,
        "result": result,
        "moves": [],
        "files": {},
        "collisions": [],
        "unreadable": [],
        "symlinks": [],
        "error": None,
    }
    return {**base, **fields}


def _note_blockers(blocked: list[str], found: Scan) -> None:
    for problem in blocked:
        _note(f"collision: {problem}")
    for rel in found.unreadable:
        _note(
            f"unreadable: {rel}: not a text file (NUL byte, invalid UTF-8, or special)"
        )
    for rel in found.symlinks:
        _note(f"symlink: {rel}: left alone, never written through")


def cmd_check(root: Path) -> int:
    candidates = candidate_files(root)
    moves, blocked = pending_moves(root, candidates)
    found = scan(root, candidates)
    for old, new in moves:
        _note(f"pending: move {old} -> {new}")
    for rel, count in found.rewrites.items():
        _note(f"pending: {rel}: {count} reference(s)")
    _note_blockers(blocked, found)
    clean = not (moves or blocked or found.rewrites or found.unreadable)
    if clean:
        _note("nothing to do: no old role name outside the allow-list")
    else:
        _note("hint: rename with: python3 scripts/rename-roles.py --apply")
    _emit(
        _payload(
            "check",
            "clean" if clean else "pending",
            moves=[{"from": old, "to": new} for old, new in moves],
            files=found.rewrites,
            collisions=blocked,
            unreadable=found.unreadable,
            symlinks=found.symlinks,
            error=None if clean else "old role names remain, or a file is in the way",
        )
    )
    return EXIT_OK if clean else EXIT_PENDING


def cmd_apply(root: Path) -> int:
    candidates = candidate_files(root)
    moves, blocked = pending_moves(root, candidates)
    # Every file is read here, before anything changes: a file that cannot be
    # read, or is not text, stops the run with nothing moved.
    found = scan(root, candidates)
    if blocked or found.unreadable:
        _note_blockers(blocked, found)
        _emit(
            _payload(
                "apply",
                "refused",
                collisions=blocked,
                unreadable=found.unreadable,
                symlinks=found.symlinks,
                error="a file is in the way; nothing was changed",
            )
        )
        return EXIT_PENDING

    # The scan saw the definitions under their old names, so its paths are
    # mapped to where each file ends up.
    tracked = tracked_files(root)
    rewrites = {FILE_RENAMES.get(rel, rel): n for rel, n in found.rewrites.items()}
    for old, new in moves:
        if old in tracked:
            git(root, "--literal-pathspecs", "mv", "--", old, new)
        else:
            os.replace(root / old, root / new)
        _note(f"moved: {old} -> {new}")

    for rel, count in rewrites.items():
        path = root / rel
        text = decode_text(path.read_bytes())
        if text is None or through_symlink(root, rel):
            raise OSError(f"{rel} changed while the rename was running")
        _write_bytes(path, rename_text(text)[0].encode("utf-8"))
        _note(f"renamed: {rel}: {count} reference(s)")
    for rel in found.symlinks:
        _note(f"symlink: {rel}: left alone, never written through")
    changed = bool(moves or rewrites)
    if not changed:
        _note("nothing to do: no old role name outside the allow-list")
    _emit(
        _payload(
            "apply",
            "applied" if changed else "no-op",
            moves=[{"from": old, "to": new} for old, new in moves],
            files=rewrites,
            symlinks=found.symlinks,
        )
    )
    return EXIT_OK


# =============================================================================
# CLI
# =============================================================================


def _emit(payload: dict[str, Any]) -> None:
    """Print the run's single JSON object to stdout."""
    print(json.dumps(payload, ensure_ascii=False))


def _note(line: str) -> None:
    """Print one human-readable finding to stderr."""
    print(line, file=sys.stderr)


class JsonArgumentParser(argparse.ArgumentParser):
    """Report usage errors as JSON on stdout with exit 1, like every outcome."""

    def error(self, message: str) -> NoReturn:
        _emit({"ok": False, "error": message})
        sys.exit(EXIT_BAD_ARGS)


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        description="Rename the general-purpose roles to their model-neutral names.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help=(
            "Top level of the repository (defaults to the parent of this "
            "script's directory)"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move the agent definitions and rewrite every reference",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.project_root
    try:
        problem = top_level_problem(root)
        if problem:
            _note(f"error: {problem}")
            _emit({"ok": False, "error": problem})
            return EXIT_BAD_ARGS
        if args.apply:
            return cmd_apply(root)
        return cmd_check(root)
    except (GitError, OSError) as exc:
        _note(f"error: {exc}")
        _emit({"ok": False, "error": str(exc)})
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
