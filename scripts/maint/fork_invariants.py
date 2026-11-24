#!/usr/bin/env python3
"""The fork's invariants, and the entry point for taking upstream in.

The fork differs from its upstream by changes a tool can make again at any
time: the two general-purpose roles are renamed, model names live only in the
manifest, the files a new project is seeded from live in ``scripts/scaffold/``,
and the default install commands clone the fork. This file says what those
invariants are and whether a tree satisfies them:

    roles-renamed         ``scripts/rename-roles.py`` finds nothing left to rename
    model-names           ``models_lint.py`` finds no model name outside the manifest
    manifest-applied      ``models.py check --strict`` finds no drift
    manifest-needs-owner  that tool refuses the tree: an agent the manifest does
                          not name, or names and the tree lacks. Only the owner
                          assigns a model, so no tool fixes this one
    seeds-pinned          each seed has the SHA-256 ``tests/test_scaffold.py`` pins
    install-urls          the updater assigns its default repository once, to
                          the fork, and no README command installs from upstream
    no-claude-md          none of the files Claude Code reads instead of AGENTS.md
    plain-entries         (``check --rev`` only) the commit holds nothing that is
                          not checked out: see ``scratch_repo.unsupported_entries``

The first three are the repository's own checkers, run with ``--project-root``
on the tree in question. The last three have no checker that can be pointed
at another tree without running that tree's code, so they are read here as
data, from the same definitions: the digests out of the test file, the file
list ``scripts/check.sh`` refuses.

``check`` judges the work tree, or one revision checked out in a scratch
repository, with the tools of the checkout it is run from. ``intake``
(``upstream_intake.py``) and ``seal`` (``intake_seal.py``) prepare the merge of
upstream; they run the fork's tools as they are at the fork's commit,
extracted into a private directory, so no file of the tree being merged is
ever executed.

Everything reported that came from a tree, a tool or git goes through
``scratch_repo.inert_path`` or ``inert_text``. What is in the JSON is safe as
ONE quoted argument or an argv element, and in Markdown prose; never unquoted
in a shell. A path is posted inside a code span; the lines this prints for a
person already do that. Nothing here pushes, and nothing contacts a remote.

Standard library only; runs on Python 3.9. Needs git 2.40 or newer.

Usage:
    python3 scripts/maint/fork_invariants.py check [--rev SHA]
    python3 scripts/maint/fork_invariants.py intake --fork-rev SHA \\
        --upstream-rev SHA [--resolve-in DIR]
    python3 scripts/maint/fork_invariants.py seal --fork-rev SHA \\
        --upstream-rev SHA --resolve-in DIR --resolutions FILE

Stdout carries exactly one JSON object; findings and conflicts are repeated
on stderr, one line each. Outcomes: ``OUTCOMES`` below, explained in
``scripts/maint/README.md``.

Exit codes:
    0  the invariants hold; nothing to take in; or the merge commit is ready
    1  usage, or a repository this cannot work in
    2  a human must decide: a conflict, a failed invariant, a refused tree
    3  git or one of the fork's tools failed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import common
import scratch_repo
from scratch_repo import Repo, Scratch, Stop, failed, inert_text, refuse

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The repository the fork was made from. It is named here to recognise its
# URL in text; nothing in this directory contacts it.
UPSTREAM_SLUG = "DeL-TaiseiOzaki/claude-code-orchestra"
UPSTREAM_GIT_URL = f"https://{common.GITHUB_HOST}/{UPSTREAM_SLUG}.git"

RENAME_TOOL = "scripts/rename-roles.py"
MODELS_TOOL = ".claude/skills/_shared/models.py"
LINT_TOOL = ".claude/skills/_shared/models_lint.py"
# Every file of the fork that is executed. models_lint.py imports models.py.
FORK_TOOLS = (RENAME_TOOL, MODELS_TOOL, LINT_TOOL)
# Where upstream keeps what a new project is seeded with -> where the fork
# does. In the fork the left-hand paths are its own state.
SEEDS = {
    ".claude/STATE.md": "scripts/scaffold/STATE.md",
    ".claude/docs/DESIGN.md": "scripts/scaffold/DESIGN.md",
}
SEED_PIN_FILE = "tests/test_scaffold.py"
SEED_PIN_BLOCK = "SEED_SHA256 = {"
SEED_PIN_RE = re.compile(r'SEED_([A-Z]+):\s*"([0-9a-f]{64})"')
# The files `scripts/check.sh` refuses.
SHADOW_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
UPDATER = "scripts/update.sh"
README = "README.md"
DEFAULT_REPO_PREFIX = "TEMPLATE_REPO="
# The one assignment the updater may hold, byte for byte.
DEFAULT_REPO_LINE = (
    f'{DEFAULT_REPO_PREFIX}"${{ORCHESTRA_TEMPLATE_REPO:-{common.FORK_GIT_URL}}}"'
)
# Any assignment to that variable, whatever its indentation or keyword.
REPO_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:(?:export|readonly|local|declare|typeset)\s+(?:-\w+\s+)*)?TEMPLATE_REPO\+?="
)
CLONE_COMMAND = "git clone"
BRANCH_FLAG = "--branch"
# A command that fetches an installer instead of cloning the repository.
FETCH_RE = re.compile(r"\b(?:curl|wget)\b|raw\.githubusercontent\.com")
REFUSED_PREFIX = "refused:"
INVARIANT_NAMES = (
    "roles-renamed",
    "model-names",
    "manifest-applied",
    "manifest-needs-owner",
    "seeds-pinned",
    "install-urls",
    "no-claude-md",
    "plain-entries",
)
TOOL_TIMEOUT_SEC = 120
PATH_ARGUMENTS = ("project_root", "resolve_in", "resolutions")
DETAIL_LINES = 20

RESULT_SATISFIED = "satisfied"
RESULT_VIOLATED = "violated"
RESULT_UP_TO_DATE = "up-to-date"
RESULT_MERGED = "merged"
RESULT_CONFLICTS = "conflicts"
RESULT_INVARIANTS = "invariants-failed"
RESULT_WORKFLOWS = "workflows-changed"
RESULT_NO_BASE = "no-single-merge-base"
RESULT_UNSUPPORTED = "unsupported-entry"
RESULT_TRANSFORM = "transform-refused"
RESULT_FORK = "fork-not-clean"
RESULT_BRANCH = "branch-exists"
RESULT_UNRESOLVED = "unresolved"
RESULT_CRASHED = "crashed"

_NEEDS_OWNER = (common.EXIT_NEEDS_OWNER,)
_ALWAYS = {
    scratch_repo.RESULT_REFUSED: (common.EXIT_USAGE,),
    scratch_repo.RESULT_GIT: (common.EXIT_FAILED,),
    scratch_repo.RESULT_TOOL: (common.EXIT_FAILED,),
    RESULT_CRASHED: (common.EXIT_FAILED,),
}
# command -> outcome -> exit codes. The README tables are pinned to this, and
# `report` refuses to print an outcome its command did not declare.
OUTCOMES: dict[str, dict[str, tuple[int, ...]]] = {
    "check": {
        RESULT_SATISFIED: (common.EXIT_OK,),
        RESULT_VIOLATED: _NEEDS_OWNER,
        **_ALWAYS,
    },
    "intake": {
        RESULT_UP_TO_DATE: (common.EXIT_OK,),
        RESULT_MERGED: (common.EXIT_OK,),
        RESULT_CONFLICTS: _NEEDS_OWNER,
        RESULT_INVARIANTS: _NEEDS_OWNER,
        RESULT_WORKFLOWS: _NEEDS_OWNER,
        RESULT_NO_BASE: _NEEDS_OWNER,
        RESULT_UNSUPPORTED: _NEEDS_OWNER,
        RESULT_TRANSFORM: _NEEDS_OWNER,
        RESULT_FORK: _NEEDS_OWNER,
        RESULT_BRANCH: _NEEDS_OWNER,
        **_ALWAYS,
    },
    # `seal` builds the automatic merge again, so everything that stops
    # `intake` before a merge exists can stop it too.
    "seal": {
        RESULT_MERGED: (common.EXIT_OK,),
        RESULT_UNRESOLVED: _NEEDS_OWNER,
        RESULT_INVARIANTS: _NEEDS_OWNER,
        RESULT_WORKFLOWS: _NEEDS_OWNER,
        RESULT_NO_BASE: _NEEDS_OWNER,
        RESULT_UNSUPPORTED: _NEEDS_OWNER,
        RESULT_TRANSFORM: _NEEDS_OWNER,
        RESULT_FORK: _NEEDS_OWNER,
        RESULT_BRANCH: _NEEDS_OWNER,
        **_ALWAYS,
    },
}


@dataclass(frozen=True)
class Finding:
    """One thing about a tree that an invariant does not allow."""

    invariant: str
    detail: str


@dataclass(frozen=True)
class Target:
    """A checked-out tree, and the fork's tools that judge it."""

    tools: Path
    tree: Path
    env: dict[str, str]
    # Seed digests to hold the tree to. None: the ones the tree pins itself.
    pins: dict[str, str] | None = None


# =============================================================================
# the fork's tools
# =============================================================================


def extract_tools(repo: Repo, revision: str, into: Path) -> Path:
    """Write the fork's tools as they are at ``revision`` into ``into``.

    This is what makes them the fork's: they come out of the fork's commit,
    never out of the tree that is checked out or being merged.
    """
    for rel in FORK_TOOLS:
        content = repo.text_at(revision, rel)
        if content is None:
            refuse(f"{rel} is not a plain file at {common.short_sha(revision)}")
        target = into / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    return into


def run_tool(target: Target, rel: str, *args: str) -> common.CommandResult:
    """Run one of the fork's tools on ``target.tree``. Python is told to
    ignore its environment and to write no bytecode beside the tool."""
    argv = [sys.executable, "-E", "-s", "-B", str(target.tools / rel), *args]
    result = common.run_command(
        argv, TOOL_TIMEOUT_SEC, cwd=target.tools, env=target.env
    )
    if result.returncode is None:
        failed(scratch_repo.RESULT_TOOL, f"{rel}: {result.failure()}")
    return result


def said(result: common.CommandResult) -> list[str]:
    """What a tool reported, as bounded lines with nothing left in them
    that a shell or Markdown would act on: a tool quotes the tree it read."""
    lines = [inert_text(line) for line in result.stderr.split("\n")]
    kept = [line for line in lines if line]
    if len(kept) > DETAIL_LINES:
        return [*kept[:DETAIL_LINES], f"... {len(kept) - DETAIL_LINES} more line(s)"]
    return kept


def verdict(
    rel: str, result: common.CommandResult, violated: tuple[int, ...]
) -> list[str]:
    """A checker's exit code as findings; any other failure stops the run."""
    if result.returncode == 0:
        return []
    if result.returncode in violated:
        return said(result) or [f"{rel} exited {result.returncode}"]
    detail = f"{rel} exited {result.returncode}: {' | '.join(said(result)[:3])}"
    failed(scratch_repo.RESULT_TOOL, detail)


# =============================================================================
# invariants
# =============================================================================


def plain_file(root: Path, rel: str) -> Path | None:
    """``root/rel`` when it is a regular file reached through no link."""
    current = root
    for part in rel.split("/"):
        current = current / part
        if current.is_symlink():
            return None
    return current if current.is_file() else None


def read_text(root: Path, rel: str) -> str | None:
    path = plain_file(root, rel)
    if path is None:
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def found(invariant: str, details: list[str]) -> list[Finding]:
    return [Finding(invariant, detail) for detail in details]


def check_roles(target: Target) -> list[Finding]:
    result = run_tool(target, RENAME_TOOL, "--project-root", str(target.tree))
    return found("roles-renamed", verdict(RENAME_TOOL, result, _NEEDS_OWNER))


def check_model_names(target: Target) -> list[Finding]:
    result = run_tool(target, LINT_TOOL, "--project-root", str(target.tree))
    return found("model-names", verdict(LINT_TOOL, result, _NEEDS_OWNER))


def check_manifest(target: Target) -> list[Finding]:
    """Exit 1 is a manifest the tree holds but the tool cannot read: the
    tree's fault, like drift, not the tool's. A line the tool marks as
    refused is not drift: rendering the manifest again cannot clear it, and
    the manifest itself is the owner's to change."""
    result = run_tool(
        target, MODELS_TOOL, "--project-root", str(target.tree), "check", "--strict"
    )
    lines = verdict(MODELS_TOOL, result, (common.EXIT_USAGE, common.EXIT_NEEDS_OWNER))
    refused = [line for line in lines if line.startswith(REFUSED_PREFIX)]
    drift = [line for line in lines if not line.startswith(REFUSED_PREFIX)]
    if refused:
        return found("manifest-needs-owner", refused)
    return found("manifest-applied", drift)


def seed_pins(text: str | None) -> dict[str, str] | None:
    """The digests ``tests/test_scaffold.py`` pins, read as data: seed path ->
    SHA-256. None when the text does not pin exactly the two seeds."""
    if text is None or SEED_PIN_BLOCK not in text:
        return None
    body = text.split(SEED_PIN_BLOCK, 1)[1].split("}", 1)[0]
    by_name = {f"{name}.md": digest for name, digest in SEED_PIN_RE.findall(body)}
    pins = {seed: by_name.get(seed.rsplit("/", 1)[1], "") for seed in SEEDS.values()}
    if len(by_name) != len(pins) or "" in pins.values():
        return None
    return pins


def check_seeds(target: Target) -> list[Finding]:
    pins = target.pins or seed_pins(read_text(target.tree, SEED_PIN_FILE))
    if pins is None:
        detail = f"{SEED_PIN_FILE} does not pin a digest for each seed"
        return found("seeds-pinned", [detail])
    problems = []
    for seed, digest in pins.items():
        path = plain_file(target.tree, seed)
        if path is None:
            problems.append(f"{seed} is missing or not a plain file")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            problems.append(f"{seed} has SHA-256 {actual}; the pin is {digest}")
    return found("seeds-pinned", problems)


def is_default_repo_line(line: str) -> bool:
    return line.startswith(DEFAULT_REPO_PREFIX)


def is_default_clone(line: str) -> bool:
    """A command that installs the default branch. A clone of a named branch
    stays where it points: upstream has branches the fork does not carry."""
    return CLONE_COMMAND in line and BRANCH_FLAG not in line


# The lines that say where an installation comes from: file -> which lines.
INSTALL_LINES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    (UPDATER, is_default_repo_line),
    (README, is_default_clone),
)


def installs_from_upstream(line: str) -> bool:
    """A README line that would install upstream's default branch: a clone
    without a branch, or an installer fetched from upstream."""
    if UPSTREAM_SLUG not in line:
        return False
    return is_default_clone(line) or FETCH_RE.search(line) is not None


def check_install_urls(target: Target) -> list[Finding]:
    """The updater's default is compared as a whole line: a line that merely
    contains the fork's URL (in a comment, say) proves nothing, and neither
    does a correct line with a second assignment below it."""
    problems = []
    updater = read_text(target.tree, UPDATER) or ""
    assigned = [line for line in updater.split("\n") if REPO_ASSIGNMENT_RE.match(line)]
    if assigned != [DEFAULT_REPO_LINE]:
        problems.append(
            f"{UPDATER} must assign TEMPLATE_REPO exactly once, as the line "
            f"{DEFAULT_REPO_LINE}; it has {len(assigned)} assignment(s)"
        )
    readme = read_text(target.tree, README) or ""
    for number, line in enumerate(readme.split("\n"), 1):
        if installs_from_upstream(line):
            problems.append(f"{README}:{number}: an install command names upstream")
    return found("install-urls", problems)


def check_shadow_files(target: Target) -> list[Finding]:
    return found(
        "no-claude-md",
        [
            f"{rel} exists: Claude Code reads it instead of AGENTS.md"
            for rel in SHADOW_FILES
            if os.path.lexists(target.tree / rel)
        ],
    )


INVARIANTS: tuple[Callable[[Target], list[Finding]], ...] = (
    check_roles,
    check_model_names,
    check_manifest,
    check_seeds,
    check_install_urls,
    check_shadow_files,
)


def check_tree(target: Target) -> list[Finding]:
    return [finding for check in INVARIANTS for finding in check(target)]


def as_json(findings: list[Finding]) -> list[dict[str, str]]:
    return [
        {"invariant": finding.invariant, "detail": inert_text(finding.detail)}
        for finding in findings
    ]


# =============================================================================
# commands
# =============================================================================


def cmd_check(args: argparse.Namespace, work: Path) -> dict[str, object]:
    """Judge the work tree, or one revision checked out in scratch, with the
    tools of the checkout this is run from."""
    project = Repo(args.project_root, scratch_repo.child_env())
    # No merge base is needed here, so a shallow clone (CI's) will do.
    scratch_repo.require_repository(project, full_history=False)
    tree = args.project_root
    findings: list[Finding] = []
    if args.rev is not None:
        rev = scratch_repo.commit_of(project, args.rev, "--rev")
        entries = scratch_repo.unsupported_entries(project, rev)
        findings = found("plain-entries", entries)
        if not findings:
            scratch = Scratch.create(work / "tree", project)
            scratch.load(rev)
            tree = scratch.repo.path
    if not findings:
        findings = check_tree(Target(args.project_root, tree, project.env))
    if findings:
        raise Stop(
            RESULT_VIOLATED,
            common.EXIT_NEEDS_OWNER,
            f"{len(findings)} finding(s)",
            findings=as_json(findings),
        )
    return {"result": RESULT_SATISFIED, "detail": "every invariant holds"}


def _cmd_intake(args: argparse.Namespace, work: Path) -> dict[str, object]:
    # Imported when used: upstream_intake builds on this module.
    import upstream_intake

    return upstream_intake.cmd_intake(args, work)


def _cmd_seal(args: argparse.Namespace, work: Path) -> dict[str, object]:
    import intake_seal

    return intake_seal.cmd_seal(args, work)


COMMANDS: dict[str, Callable[[argparse.Namespace, Path], dict[str, object]]] = {
    "check": cmd_check,
    "intake": _cmd_intake,
    "seal": _cmd_seal,
}


# =============================================================================
# CLI
# =============================================================================


class JsonArgumentParser(argparse.ArgumentParser):
    """Report usage errors as JSON on stdout with exit 1, like every outcome."""

    def error(self, message: str) -> NoReturn:
        body = {"ok": False, "result": scratch_repo.RESULT_REFUSED, "detail": message}
        print(json.dumps({**body, "exit_code": common.EXIT_USAGE}))
        sys.exit(common.EXIT_USAGE)


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        description="Check the fork's invariants, or prepare the merge of upstream."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Top level of the fork's checkout (defaults to this script's own)",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
        parser_class=JsonArgumentParser,
    )
    check = commands.add_parser("check", help="Report whether a tree holds")
    check.add_argument(
        "--rev", help="A full commit SHA to check instead of the work tree"
    )
    intake = commands.add_parser("intake", help="Prepare the merge of upstream")
    seal = commands.add_parser("seal", help="Commit a merge finished by hand")
    for command in (intake, seal):
        command.add_argument("--fork-rev", required=True, help="The fork's commit")
        command.add_argument(
            "--upstream-rev", required=True, help="Upstream's commit to take in"
        )
        command.add_argument(
            "--owner-allows-workflow-changes",
            action="store_true",
            help="For the owner at a terminal only: let .github change",
        )
    intake.add_argument(
        "--resolve-in",
        type=Path,
        help="A directory, not yet existing, to leave an unfinished merge in",
    )
    seal.add_argument(
        "--resolve-in",
        type=Path,
        required=True,
        help="The directory intake --resolve-in left",
    )
    seal.add_argument(
        "--resolutions",
        type=Path,
        required=True,
        help="JSON object: every conflicted path -> a note on what was decided",
    )
    return parser


def report(command: str, payload: dict[str, object], exit_code: int) -> int:
    """Print the outcome, only when its command declared it."""
    result = str(payload["result"])
    detail = inert_text(str(payload["detail"]))
    common.checked_result(OUTCOMES[command], command, result, exit_code, detail)
    findings = payload.get("findings", [])
    conflicts = payload.get("conflicts", [])
    for finding in findings if isinstance(findings, list) else []:
        print(f"finding: {finding['invariant']}: {finding['detail']}", file=sys.stderr)
    for conflict in conflicts if isinstance(conflicts, list) else []:
        kinds = ", ".join(conflict["types"]) or "unmerged"
        print(f"conflict: `{conflict['path']}`: {kinds}", file=sys.stderr)
    print(f"{command}: {result}: {detail}", file=sys.stderr)
    body = {"ok": exit_code == common.EXIT_OK, "command": command, **payload}
    print(json.dumps({**body, "detail": detail, "exit_code": exit_code}))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Absolute before anything runs: the fork's tools are started from their
    # own directory, where a relative path would name something else.
    for name in PATH_ARGUMENTS:
        if getattr(args, name, None) is not None:
            setattr(args, name, Path(os.path.abspath(getattr(args, name))))
    work = Path(tempfile.mkdtemp(prefix="orchestra-intake-")).resolve()
    try:
        payload = COMMANDS[args.command](args, work)
        return report(args.command, payload, common.EXIT_OK)
    except Stop as stop:
        payload = {**stop.fields, "result": stop.result, "detail": stop.detail}
        return report(args.command, payload, stop.exit_code)
    except OSError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        payload = {"result": scratch_repo.RESULT_TOOL, "detail": detail}
        return report(args.command, payload, common.EXIT_FAILED)
    except Exception as exc:
        # A bug in this file. It is still one JSON object and exit 3, so a
        # caller that reads only those never takes a crash for a verdict.
        traceback.print_exc()
        detail = f"{type(exc).__name__}: {exc}"
        payload = {"result": RESULT_CRASHED, "detail": detail}
        return report(args.command, payload, common.EXIT_FAILED)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    # Run under the module's own name: upstream_intake imports this file, and
    # a second copy called __main__ would declare its own outcomes and types.
    import fork_invariants

    sys.exit(fork_invariants.main())
