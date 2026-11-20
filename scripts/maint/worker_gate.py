#!/usr/bin/env python3
"""Say whether the issue worker may merge its own pull request.

The issue worker is an unattended session with the owner's GitHub identity.
What stands between its pull request and ``main`` must not be its own reading
of a file list, so the decision is made here, and the worker runs this file
as it is on ``main``, never as it is in the pull request:

    git show refs/remotes/origin/main:scripts/maint/worker_gate.py > "$tmp/gate.py"
    python3 "$tmp/gate.py" --base <main SHA> --head <head SHA>

That is why this file imports nothing from the repository: it is one file,
read out of a commit. The verifier it starts
(``.claude/skills/_shared/verify_delegation.py``) is read out of ``--base``
the same way.

``self-merge-allowed`` only when all of this holds for the change between the
merge base of the two commits and ``--head``:

* no changed path is on the stop-list: the required checks and what defines
  them (``.github/``, ``scripts/``, ``tests/``, ``pyproject.toml``,
  ``uv.lock``), and what a later session executes or loads as instructions
  (``.claude/settings.json``, hooks, rules, skills, agents, ``.agents/``,
  ``.codex/``, ``.mcp.json``, any ``AGENTS.md`` or ``CLAUDE.md``). A pull request
  that changes one of these would certify itself, or would instruct the
  session that merges the next one;
* every changed path is a plain file with a name made of ``A-Za-z0-9._/+@-``;
* no test file is deleted;
* the verifier reports no weakened test and no placeholder.

Anything else is ``needs-owner``: the pull request stays open and the owner
merges it. The stop-list is also what ``upstream_intake.py`` reports as
``sensitive_changes``; it is defined once, here.

Standard library only; runs on Python 3.9. Stdout carries exactly one JSON
object; each reason is repeated on stderr.

Usage:
    python3 worker_gate.py --base SHA --head SHA [--project-root DIR]

Exit codes:
    0  self-merge-allowed
    1  refused: bad arguments, or the head commit is not what is checked out
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

# A change below one of these directories is never merged unattended.
STOP_DIRECTORIES = (
    ".github",
    ".claude/hooks",
    ".claude/rules",
    ".claude/skills",
    ".claude/agents",
    ".agents",
    ".codex",
    "scripts",
    "tests",
)
STOP_FILES = (
    ".claude/settings.json",
    ".mcp.json",
    "pyproject.toml",
    "uv.lock",
)
# Read as instructions in whatever directory they lie.
STOP_NAMES = ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md")

VERIFIER = ".claude/skills/_shared/verify_delegation.py"
VERIFIER_FINDINGS = (
    ("weakened_tests", "weakened test"),
    ("placeholders", "placeholder"),
)
TEST_PATH_RE = re.compile(
    r"(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$"
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[A-Za-z0-9._/+@-]+")
PLAIN_MODES = ("100644", "100755")
DELETED = "D"
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
    ``A-Za-z0-9._/+@-`` written as a visible escape: safe to print, to paste
    into a shell and to post."""
    made = "".join(
        char if NAME_RE.fullmatch(char) else f"\\x{ord(char):02x}" for char in text
    )
    return f"\\x2d{made[1:]}" if made.startswith("-") else made


def stop_list_entry(path: str) -> str | None:
    """The stop-list entry ``path`` falls under, or None. Case is ignored: on
    the owner's disk ``.GitHub/`` is ``.github/``."""
    folded = path.casefold()
    for directory in STOP_DIRECTORIES:
        if folded == directory.casefold() or folded.startswith(
            directory.casefold() + "/"
        ):
            return f"{directory}/"
    for name in STOP_FILES:
        if folded == name.casefold():
            return name
    for name in STOP_NAMES:
        if folded.rsplit("/", 1)[-1] == name.casefold():
            return name
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


def require_checked_out(root: Path, base: str, head: str) -> None:
    """The verifier reads the work tree, so the work tree must be exactly the
    commit that would be merged: checked out, with nothing beside it."""
    for name, sha in (("--base", base), ("--head", head)):
        if SHA_RE.fullmatch(sha) is None:
            refuse(f"{name} must be a full commit SHA")
    top = git(root, "rev-parse", "--show-toplevel", ok=(0, 128))
    if top.returncode != 0 or not os.path.samefile(text_of(top).rstrip("\n"), root):
        refuse("the project root is not the top level of a git work tree")
    for name, sha in (("--base", base), ("--head", head)):
        known = git(
            root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", ok=(0, 1)
        )
        if known.returncode != 0:
            refuse(f"{name} is not a commit in this repository")
    if text_of(git(root, "rev-parse", "HEAD")).strip() != head:
        refuse("--head is not the commit that is checked out")
    if text_of(git(root, "status", "--porcelain", "--untracked-files=all")).strip():
        refuse("the work tree holds changes that are not in --head")


def changed_entries(root: Path, start: str, head: str) -> list[tuple[str, str, str]]:
    """Every entry that differs: (status, mode at head, path). A rename is a
    deletion and an addition, so both names are judged."""
    raw = text_of(
        git(root, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", start, head)
    )
    fields = raw.split("\0")
    entries = []
    for index in range(0, len(fields) - 1, 2):
        meta = fields[index].split(" ")
        entries.append((meta[4], meta[1], fields[index + 1]))
    return entries


def path_reasons(entries: list[tuple[str, str, str]]) -> list[str]:
    reasons = []
    for status, mode, path in entries:
        shown = inert(path)
        entry = stop_list_entry(path)
        if entry is not None:
            reasons.append(f"stop-list {entry}: {shown}")
        if NAME_RE.fullmatch(path) is None or path.startswith("-") or "/-" in path:
            reasons.append(f"a name outside A-Za-z0-9._/+@-: {shown}")
        if status != DELETED and mode not in PLAIN_MODES:
            reasons.append(f"not a plain file (mode {mode}): {shown}")
        if status == DELETED and TEST_PATH_RE.search(path):
            reasons.append(f"a test file is deleted: {shown}")
    return reasons


def verifier_reasons(root: Path, base: str, start: str, work: Path) -> list[str]:
    """What the repository's own verifier, as it is at ``base``, reports about
    the change: weakened tests and placeholders."""
    blob = git(root, "cat-file", "blob", f"{base}:{VERIFIER}", ok=(0, 128))
    if blob.returncode != 0:
        raise Stop(RESULT_TOOL, EXIT_FAILED, f"{VERIFIER} is not in --base")
    tool = work / "verify_delegation.py"
    tool.write_bytes(blob.stdout)
    argv = [sys.executable, "-E", "-s", "-B", str(tool)]
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
        f"{label} ({inert(str(item.get('pattern', '')))}): {inert(str(item.get('file', '')))}"
        for label, items in found
        for item in items
    ]


def judge(
    root: Path, base: str, head: str, work: Path
) -> tuple[dict[str, object], list[str]]:
    """What is known about the change, and every reason not to merge it."""
    require_checked_out(root, base, head)
    bases = text_of(git(root, "merge-base", "--all", base, head, ok=(0, 1))).split()
    facts: dict[str, object] = {"base": base, "head": head}
    if len(bases) != 1:
        return facts, [f"{len(bases)} merge bases between --base and --head"]
    start = bases[0]
    entries = changed_entries(root, start, head)
    reasons = path_reasons(entries) if entries else ["nothing is changed"]
    reasons += verifier_reasons(root, base, start, work)
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
        description="Say whether the issue worker may merge its own pull request."
    )
    parser.add_argument("--base", required=True, help="The commit main is at")
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
        facts, reasons = judge(root, args.base, args.head, work)
        payload = {
            **facts,
            "reasons": reasons[:REASONS_SHOWN],
            "reasons_total": len(reasons),
        }
        if reasons:
            detail = f"{len(reasons)} reason(s); the owner merges this one"
            return report(payload, RESULT_NEEDS_OWNER, EXIT_NEEDS_OWNER, detail)
        return report(payload, RESULT_ALLOWED, EXIT_OK, "nothing stands against it")
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
