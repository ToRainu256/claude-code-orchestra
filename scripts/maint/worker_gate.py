#!/usr/bin/env python3
"""Say whether the issue worker may merge its own pull request.

The issue worker is an unattended session with the owner's GitHub identity.
What stands between its pull request and ``main`` must not be its own reading
of a file list, so the decision is made here, and the worker runs this file
as it is on ``main``, never as it is in the pull request:

    d=$(mktemp -d) &&
      git show refs/remotes/origin/main:scripts/maint/worker_gate.py > "$d/gate.py" &&
      test -s "$d/gate.py" && python3 -I "$d/gate.py" --head <head SHA>

That is why this file imports nothing from the repository: it is one file,
read out of a commit. The verifier it starts
(``.claude/skills/_shared/verify_delegation.py``) is read out of ``main`` the
same way.

**The worker merges documentation-only changes by itself; everything else
waits for the owner.** ``self-merge-allowed`` only when all of this holds for
the change between ``--head`` and its merge base with
``refs/remotes/origin/main``, which this file works out itself and takes
from nobody:

* every changed path is on the allow-list: ``README.md``,
  ``TEMPLATE_DESIGN_LOG.md``, or a file below ``.claude/docs/research/`` or
  ``.claude/docs/reviews/``, matched exactly, case included. It is a list of
  what may merge, not of what may not: a file name nobody thought of
  (a ``conftest.py``, a ``pytest.ini``, a new settings file) is not on it;
* each of them is added or modified, never deleted, and is a plain file of
  mode 100644 before and after, with a name made of ``A-Za-z0-9._/+@-`` and
  not one that tools read as instructions wherever it lies (a ``CLAUDE.md``
  below an allowed directory is still a ``CLAUDE.md``);
* the verifier reports no weakened test and no placeholder.

The change is listed by comparing ``git ls-tree -r`` of the two commits.
Nothing in the pull request's tree is asked what changed: a ``.gitmodules``
that tells ``git diff`` to ignore a submodule hides nothing here.

Anything else is ``needs-owner``: the pull request stays open and the owner
merges it.

``sensitive_entry`` is a second, different list: what ``upstream_intake.py``
reports as ``sensitive_changes``, for a person reading a pull request. It
names what a later session executes or loads. It is a deny-list, which is
acceptable there because it only annotates; it decides nothing here beyond
refusing such a name below an allowed directory.

A path in ``reasons`` is given in the inert form, inside a code span, so the
text can be posted as it is.

Standard library only; runs on Python 3.9. Stdout carries exactly one JSON
object; each reason is repeated on stderr.

Usage:
    python3 -I worker_gate.py --head SHA [--project-root DIR]

Exit codes:
    0  self-merge-allowed
    1  refused: bad arguments, no ``refs/remotes/origin/main``, or the head
       commit is not what is checked out
    2  needs-owner: at least one reason
    3  git or the verifier failed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import NoReturn

RESULT_ALLOWED = "self-merge-allowed"
RESULT_NEEDS_OWNER = "needs-owner"
RESULT_REFUSED = "refused"
RESULT_GIT = "git-failed"
RESULT_TOOL = "tool-failed"
RESULT_CRASHED = "crashed"
EXIT_OK, EXIT_USAGE, EXIT_NEEDS_OWNER, EXIT_FAILED = 0, 1, 2, 3
# outcome -> exit codes. The README table is pinned to this.
OUTCOMES: dict[str, tuple[int, ...]] = {
    RESULT_ALLOWED: (EXIT_OK,),
    RESULT_NEEDS_OWNER: (EXIT_NEEDS_OWNER,),
    RESULT_REFUSED: (EXIT_USAGE,),
    RESULT_GIT: (EXIT_FAILED,),
    RESULT_TOOL: (EXIT_FAILED,),
    RESULT_CRASHED: (EXIT_FAILED,),
}

# The branch a pull request is judged against, by its full name: a short name
# is resolved through tags first.
MAIN_REF = "refs/remotes/origin/main"

# Everything the worker may merge by itself. Exact paths, case included.
ALLOWED_FILES = ("README.md", "TEMPLATE_DESIGN_LOG.md")
ALLOWED_DIRECTORIES = (".claude/docs/research", ".claude/docs/reviews")
ALLOWED_MODE = "100644"

# What `sensitive_changes` names: a change below one of these directories...
SENSITIVE_DIRECTORIES = (
    ".github",
    ".claude/hooks",
    ".claude/rules",
    ".claude/skills",
    ".claude/agents",
    ".claude/commands",
    ".claude/output-styles",
    ".claude/agent-memory",
    ".claude/docs/plans",
    ".claude/docs/libraries",
    ".agents",
    ".codex",
    "scripts",
    "tests",
)
# ...a file directly in one of these (the procedure documents every session
# is sent to)...
SENSITIVE_FLAT_DIRECTORIES = (".claude/docs",)
# ...one of these files...
SENSITIVE_FILES = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/STATE.md",
    "PROGRESS.md",
    ".mcp.json",
    "pyproject.toml",
    "uv.lock",
)
# ...or a file of one of these names in whatever directory: instructions for
# an agent, or configuration for a tool somebody runs here.
SENSITIVE_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "GEMINI.md",
    ".cursorrules",
    "opencode.json",
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "ruff.toml",
    "uv.toml",
    ".python-version",
    "sitecustomize.py",
    "Makefile",
    "package.json",
    ".pre-commit-config.yaml",
    ".envrc",
    ".gitattributes",
    ".gitmodules",
    ".gitignore",
)
SENSITIVE_SUFFIXES = (".pth",)

VERIFIER = ".claude/skills/_shared/verify_delegation.py"
VERIFIER_FINDINGS = (
    ("weakened_tests", "weakened test"),
    ("placeholders", "placeholder"),
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[A-Za-z0-9._/+@-]+")
PLAIN_MODES = ("100644", "100755")
ABSENT = "absent"
GIT_TIMEOUT_SEC = 60
VERIFIER_TIMEOUT_SEC = 180
REASONS_SHOWN = 40
GIT_ENV_PREFIX = "GIT_"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
}
GIT_OPTIONS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.excludesFile=/dev/null",
)  # fmt: skip


class Stop(Exception):
    """The run ends here with a declared outcome."""

    def __init__(self, result: str, exit_code: int, detail: str) -> None:
        super().__init__(detail)
        self.result = result
        self.exit_code = exit_code
        self.detail = detail


def inert(text: str) -> str:
    """A name or a word from the pull request, with every character outside
    ``A-Za-z0-9._/+@-`` written as a visible escape. Safe as an argument; in
    Markdown it belongs inside a code span, where ``@`` and ``www.`` are not
    read either. It can hold no backtick, so a span always closes."""
    made = "".join(
        char if NAME_RE.fullmatch(char) else f"\\x{ord(char):02x}" for char in text
    )
    return f"\\x2d{made[1:]}" if made.startswith("-") else made


def spanned(path: str) -> str:
    """``path`` as it is written into a reason: inert, in a code span."""
    return f"`{inert(path)}`"


def allowed(path: str) -> bool:
    """True when ``path`` is on the allow-list. Exact, case included: a case
    variant is another path to git, and on the owner's disk another's twin."""
    if path in ALLOWED_FILES:
        return True
    return any(path.startswith(f"{directory}/") for directory in ALLOWED_DIRECTORIES)


def sensitive_entry(path: str) -> str | None:
    """The entry of the sensitive list ``path`` falls under, or None. Case is
    ignored: on the owner's disk ``.GitHub/`` is ``.github/``."""
    folded = path.casefold()
    name = folded.rsplit("/", 1)[-1]
    for directory in SENSITIVE_DIRECTORIES:
        if folded == directory.casefold() or folded.startswith(
            directory.casefold() + "/"
        ):
            return f"{directory}/"
    for directory in SENSITIVE_FLAT_DIRECTORIES:
        if folded.rsplit("/", 1)[0] == directory.casefold():
            return f"{directory}/*"
    for entry in SENSITIVE_FILES:
        if folded == entry.casefold():
            return entry
    for entry in SENSITIVE_NAMES:
        if name == entry.casefold():
            return entry
    for suffix in SENSITIVE_SUFFIXES:
        if name.endswith(suffix):
            return f"*{suffix}"
    return None


def git_env() -> dict[str, str]:
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(GIT_ENV_PREFIX)
    }
    return {**kept, **GIT_ENV}


def git(
    root: Path, *args: str, ok: tuple[int, ...] = (0,)
) -> subprocess.CompletedProcess:
    """git in ``root`` without the user's configuration, hooks or ``GIT_*``."""
    try:
        done = subprocess.run(
            ["git", *GIT_OPTIONS, "-C", str(root), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop(
            RESULT_GIT, EXIT_FAILED, f"git {args[0]}: {type(exc).__name__}"
        ) from exc
    if done.returncode not in ok:
        raise Stop(RESULT_GIT, EXIT_FAILED, f"git {args[0]} exited {done.returncode}")
    return done


def text_of(done: subprocess.CompletedProcess) -> str:
    return done.stdout.decode("utf-8", "replace")


def refuse(detail: str) -> NoReturn:
    raise Stop(RESULT_REFUSED, EXIT_USAGE, detail)


def commit_at(root: Path, name: str) -> str | None:
    """The commit ``name`` is, or None when it is not one here."""
    found = git(
        root, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", ok=(0, 1)
    )
    return text_of(found).strip() if found.returncode == 0 else None


def require_checked_out(root: Path, head: str) -> str:
    """The verifier reads the work tree, so the work tree must be exactly the
    commit that would be merged: checked out, with nothing beside it. Returns
    the commit ``main`` is at: read here, never taken from the caller, who
    could name a base that hides the branch's own earlier commits."""
    if SHA_RE.fullmatch(head) is None:
        refuse("--head must be a full commit SHA")
    top = git(root, "rev-parse", "--show-toplevel", ok=(0, 128))
    if top.returncode != 0 or not os.path.samefile(text_of(top).rstrip("\n"), root):
        refuse("the project root is not the top level of a git work tree")
    if commit_at(root, head) is None:
        refuse("--head is not a commit in this repository")
    main = commit_at(root, MAIN_REF)
    if main is None:
        refuse(f"{MAIN_REF} is not here; fetch it first")
    if text_of(git(root, "rev-parse", "HEAD")).strip() != head:
        refuse("--head is not the commit that is checked out")
    if text_of(git(root, "status", "--porcelain", "--untracked-files=all")).strip():
        refuse("the work tree holds changes that are not in --head")
    return main


def tree_entries(root: Path, commit: str) -> dict[str, str]:
    """Every entry of a commit's tree: path -> "mode object". Read from the
    tree object alone; no attribute, module file or setting is consulted."""
    listed = text_of(git(root, "ls-tree", "-r", "-z", "--full-tree", commit))
    entries = {}
    for line in listed.split("\0"):
        if line:
            meta, _, path = line.partition("\t")
            mode, _, oid = meta.split(" ")
            entries[path] = f"{mode} {oid}"
    return entries


def changed_entries(root: Path, start: str, head: str) -> list[tuple[str, str, str]]:
    """Every path whose entry differs: (path, mode before, mode after), with
    ``absent`` for a side that has no such entry. A rename is two of these,
    so both of its names are judged."""
    before, after = tree_entries(root, start), tree_entries(root, head)
    return [
        (
            path,
            before[path].split(" ")[0] if path in before else ABSENT,
            after[path].split(" ")[0] if path in after else ABSENT,
        )
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    ]


def path_reasons(entries: list[tuple[str, str, str]]) -> list[str]:
    """Why each changed path may not merge unattended; empty when all may."""
    reasons = []
    for path, was, now in entries:
        shown = spanned(path)
        unusual = [mode for mode in (was, now) if mode not in (*PLAIN_MODES, ABSENT)]
        if unusual:
            reasons.append(f"not a plain file (mode {unusual[0]}): {shown}")
        if not allowed(path):
            reasons.append(f"not on the allow-list: {shown}")
            continue
        entry = sensitive_entry(path)
        if entry is not None:
            reasons.append(f"read by a tool wherever it lies ({entry}): {shown}")
        if NAME_RE.fullmatch(path) is None or "/-" in path:
            reasons.append(f"a name outside A-Za-z0-9._/+@-: {shown}")
        if now == ABSENT:
            reasons.append(f"deleted: {shown}")
        elif now != ALLOWED_MODE or was not in (ALLOWED_MODE, ABSENT):
            reasons.append(f"mode {was} -> {now}, not {ALLOWED_MODE}: {shown}")
    return reasons


def verifier_reasons(root: Path, main: str, start: str, work: Path) -> list[str]:
    """What the repository's own verifier, as it is on ``main``, reports about
    the change: weakened tests and placeholders."""
    blob = git(root, "cat-file", "blob", f"{main}:{VERIFIER}", ok=(0, 128))
    if blob.returncode != 0:
        raise Stop(RESULT_TOOL, EXIT_FAILED, f"{VERIFIER} is not on main")
    tool = work / "verify_delegation.py"
    tool.write_bytes(blob.stdout)
    argv = [sys.executable, "-I", "-B", str(tool)]
    argv += ["--base", start, "--project-root", str(root), "--label", "worker-gate"]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=VERIFIER_TIMEOUT_SEC,
            check=False,
            cwd=str(work),
            env=git_env(),
        )
        payload = json.loads(done.stdout.decode("utf-8", "replace"))
        collected = payload["ok"] is True and done.returncode in (
            EXIT_OK,
            EXIT_NEEDS_OWNER,
        )
        found = [(label, payload[key]) for key, label in VERIFIER_FINDINGS]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        detail = f"{VERIFIER} did not report: {type(exc).__name__}"
        raise Stop(RESULT_TOOL, EXIT_FAILED, detail) from exc
    if not collected:
        raise Stop(RESULT_TOOL, EXIT_FAILED, f"{VERIFIER} exited {done.returncode}")
    return [
        f"{label} ({inert(str(item.get('pattern', '')))}): "
        f"{spanned(str(item.get('file', '')))}"
        for label, items in found
        for item in items
    ]


def judge(root: Path, head: str, work: Path) -> tuple[dict[str, object], list[str]]:
    """What is known about the change, and every reason not to merge it."""
    main = require_checked_out(root, head)
    bases = text_of(git(root, "merge-base", "--all", main, head, ok=(0, 1))).split()
    facts: dict[str, object] = {"main": main, "head": head}
    if len(bases) != 1:
        return facts, [f"{len(bases)} merge bases between main and --head"]
    start = bases[0]
    entries = changed_entries(root, start, head)
    reasons = path_reasons(entries) if entries else ["nothing is changed"]
    reasons += verifier_reasons(root, main, start, work)
    return {**facts, "merge_base": start, "changed": len(entries)}, reasons


def report(payload: dict[str, object], result: str, exit_code: int, detail: str) -> int:
    """Print the outcome, only when it is a declared one."""
    if exit_code not in OUTCOMES.get(result, ()):
        raise ValueError(f"undeclared outcome {result!r} with exit {exit_code}")
    reasons = payload.get("reasons", [])
    for reason in reasons if isinstance(reasons, list) else []:
        print(f"reason: {reason}", file=sys.stderr)
    print(f"worker-gate: {result}: {detail}", file=sys.stderr)
    body = {"ok": exit_code == EXIT_OK, "command": "worker-gate", **payload}
    print(
        json.dumps({**body, "result": result, "detail": detail, "exit_code": exit_code})
    )
    return exit_code


class JsonArgumentParser(argparse.ArgumentParser):
    """Report usage errors as JSON on stdout with exit 1, like every outcome."""

    def error(self, message: str) -> NoReturn:
        body = {"ok": False, "command": "worker-gate", "result": RESULT_REFUSED}
        print(json.dumps({**body, "detail": message, "exit_code": EXIT_USAGE}))
        sys.exit(EXIT_USAGE)


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        description="Say whether the issue worker may merge its own pull request.",
        allow_abbrev=False,
    )
    parser.add_argument("--head", required=True, help="The pull request's head commit")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Top level of the checkout that holds --head (default: here)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(os.path.abspath(args.project_root))
    work = Path(tempfile.mkdtemp(prefix="orchestra-gate-"))
    try:
        facts, reasons = judge(root, args.head, work)
        payload = {
            **facts,
            "reasons": reasons[:REASONS_SHOWN],
            "reasons_total": len(reasons),
        }
        if reasons:
            detail = f"{len(reasons)} reason(s); the owner merges this one"
            return report(payload, RESULT_NEEDS_OWNER, EXIT_NEEDS_OWNER, detail)
        detail = "documentation only; nothing stands against it"
        return report(payload, RESULT_ALLOWED, EXIT_OK, detail)
    except Stop as stop:
        return report({}, stop.result, stop.exit_code, stop.detail)
    except Exception as exc:
        # A bug here must never read as permission: one JSON object, exit 3.
        traceback.print_exc()
        return report({}, RESULT_CRASHED, EXIT_FAILED, type(exc).__name__)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
