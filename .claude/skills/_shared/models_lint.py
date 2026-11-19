#!/usr/bin/env python3
"""Fail on a model name written anywhere but the model manifest.

``models.json`` beside this script says which model each role runs on. A
sentence that says the same thing is a second copy nobody updates, so prose,
code and configuration name the *role* and leave the model to the manifest.
This script is the gate for that. It is the template repository's own gate
(``scripts/check.sh`` runs it there and nowhere else): in an installed project
the template's files are copies that already passed it, and everything else is
the project's own text.

What is scanned: every file git tracks or would add (``git ls-files -co
--exclude-standard``) under the project root, which must be the top level of a
git work tree. Nothing is listed by hand, so a new file or directory is
scanned the day it appears. Ignored files are not part of the tree and are not
read.

What fails (one finding each, as ``file:line``):

* ``legacy-role``: a role name that used to state a model, in any letter case
  and with ``-`` or ``_`` between its words. ``scripts/rename-roles.py``
  rewrites the exact spelling; a variant is flagged here for an edit by hand.
* ``model-id``: a Codex model slug or a Claude model ID.
* ``versioned-name``: a model family followed by a version.
* ``family-word``: a bare model-family word, also when a suffix is hyphenated
  onto it. The tier IDs ``fable`` and ``sol`` are identifiers, not model
  claims, and pass; so does a word inside a longer hyphenated identifier.
* ``symlink``: a symlink, or a file reached through one, whose target is not
  itself a scanned file held to at least the same rules. What a link points at
  is otherwise text this gate never saw.
* ``not-text``: a file with a NUL byte or invalid UTF-8 (UTF-16 included). It
  cannot be vouched for, so it fails unless it is on ``BINARY_ALLOWLIST``.

The family-word rule cannot tell a model from the ordinary English word, so
"magnum opus" or "write a haiku" is flagged by design. Reword the sentence.

What is exempt: ``EXEMPTIONS`` lists each path with the rules it is excused
from and the reason: the manifest itself, the two tools that define the
names, history that records what was true when written, and test fixtures.
``GENERATED_LINES`` excuses only the lines ``models.py apply`` writes. One
line can be excused from the three model rules (never from ``legacy-role``)
with a ``models-lint: allow <reason>`` comment on that line. Every line
excused that way is reported under ``allowed``, and the set this repository
may contain is pinned by a test, so adding a marker is a reviewed change.

This script does not read the manifest, so it runs on a tree whose manifest is
broken. Stdout carries exactly one JSON object; each finding is repeated on
stderr as one ``lint:`` line, which is what ``scripts/check.sh`` prints.

Usage:
    python3 models_lint.py
    python3 models_lint.py --project-root DIR

Exit codes:
    0  no model name outside the manifest
    1  bad arguments, or the project root is not the top level of a git work
       tree
    2  at least one finding
    3  git failed, a listed file could not be read, or there was nothing to
       scan
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import models

PROJECT_ROOT = models.PROJECT_ROOT

RULE_LEGACY_ROLE = "legacy-role"
RULE_MODEL_ID = "model-id"
RULE_VERSIONED = "versioned-name"
RULE_FAMILY = "family-word"
RULE_SYMLINK = "symlink"
RULE_NOT_TEXT = "not-text"

# Each retired role name with `-` or `_` between its words.
_LEGACY_ROLE_NAMES = "|".join(
    "[-_]".join(map(re.escape, name.split("-"))) for name in models.LEGACY_AGENT_NAMES
)
# Applied in this order, each to what the earlier ones left: a family name
# with a version is one versioned name, not that and a family word. The model
# rules are ASCII-only, so a name is still found where the text around it has
# no spaces to mark a word boundary (Japanese prose).
TEXT_RULE_PATTERNS = (
    (
        RULE_LEGACY_ROLE,
        re.compile(
            rf"(?<![A-Za-z0-9-])(?:{_LEGACY_ROLE_NAMES})(?![A-Za-z0-9-])",
            re.IGNORECASE,
        ),
    ),
    # A Codex slug, or a Claude ID with the family before or after the version.
    # The repeats are bounded so a line of hyphenated words costs linear time.
    (
        RULE_MODEL_ID,
        re.compile(
            r"\b(?:gpt-\d(?:[\w.-]*\w)?"
            r"|claude-(?:\w+(?:\.\w+)*-){0,8}(?:opus|sonnet|haiku|fable|mythos)(?!\w)"
            r"(?:-\w+(?:\.\w+)*){0,8})",
            re.ASCII,
        ),
    ),
    # The version is what makes it a model reference, so the tier's display
    # name without one passes.
    (
        RULE_VERSIONED,
        re.compile(
            r"\b(?:opus|sonnet|haiku|fable|mythos|gpt)(?:[ \t]+|-)?\d+(?:\.\d+)*\+?",
            re.ASCII | re.IGNORECASE,
        ),
    ),
    # `fable` and `sol` are tier IDs and are not listed. The lookbehind keeps
    # a word inside a hyphenated identifier out; a suffix hyphenated onto the
    # word ("-class", "-tier") does not hide it.
    (
        RULE_FAMILY,
        re.compile(
            r"(?<![\w-])(?:opus|sonnet|haiku|mythos)(?!\w)", re.ASCII | re.IGNORECASE
        ),
    ),
)
MODEL_RULES = frozenset((RULE_MODEL_ID, RULE_VERSIONED, RULE_FAMILY))
LEGACY_ROLE_ONLY = frozenset((RULE_LEGACY_ROLE,))
TEXT_RULES = MODEL_RULES | LEGACY_ROLE_ONLY

# Paths excused from some or all text rules: (path, rules, why). A path ending
# in "/" is a directory. Every entry needs its reason; an exemption nobody can
# justify is a place stale model names collect. The paths excused from the
# legacy-role rule are exactly the ones scripts/rename-roles.py leaves alone,
# so this gate never asks for an edit the codemod refuses to make.
EXEMPTIONS = (
    (
        models.MANIFEST_REL,
        MODEL_RULES,
        "the manifest: where model values live, history included",
    ),
    (
        ".claude/skills/_shared/models.py",
        TEXT_RULES,
        "defines the model value patterns and the retired role names",
    ),
    (
        ".claude/skills/_shared/models_lint.py",
        MODEL_RULES,
        "defines these rules, family words included",
    ),
    (
        "scripts/rename-roles.py",
        LEGACY_ROLE_ONLY,
        "the codemod names the roles it renames",
    ),
    ("TEMPLATE_DESIGN_LOG.md", TEXT_RULES, "design log: what was true when written"),
    (".claude/docs/research/", TEXT_RULES, "research notes quote what they measured"),
    (".claude/docs/reviews/", TEXT_RULES, "review notes quote the tree they reviewed"),
    (".claude/docs/plans/", TEXT_RULES, "plans name the state they set out to change"),
    (
        "tests/",
        MODEL_RULES,
        "fixtures need literal model values; a retired role name still fails",
    ),
    (
        "tests/test_models_lint.py",
        LEGACY_ROLE_ONLY,
        "pins the legacy-role rule and the legacy-key refusal",
    ),
    (
        "tests/test_rename_roles.py",
        LEGACY_ROLE_ONLY,
        "pins the codemod's old-to-new mapping",
    ),
)
# Files that are binary on purpose: (path, why). Anything else that is not
# text is a finding, because a NUL byte or a second encoding is also how a
# file would be hidden from this gate.
BINARY_ALLOWLIST = (("summary.png", "the image the README shows"),)


def _frontmatter_extent(lines: list[str]) -> int:
    """How many leading lines are YAML frontmatter (0 when there is none)."""
    if not lines or lines[0] != models.FRONTMATTER_FENCE:
        return 0
    for index in range(1, len(lines)):
        if lines[index] == models.FRONTMATTER_FENCE:
            return index
    return 0


def _toml_top_level_extent(lines: list[str]) -> int:
    """How many leading lines come before the first TOML table header."""
    for index, line in enumerate(lines):
        if models.TOML_TABLE_HEADER_RE.match(line):
            return index
    return len(lines)


# Lines `models.py apply` writes from the manifest: (file pattern, line
# pattern, how many leading lines the exemption covers, why). Only the managed
# line in the managed place is exempt: a `model:` line in an agent's body, a
# `model` key inside a TOML table, or a nested settings key is text somebody
# typed.
GENERATED_LINES = (
    (
        re.compile(r"\.claude/agents/[^/]+\.md"),
        re.compile(r"model:"),
        _frontmatter_extent,
        "agent frontmatter, rendered by apply",
    ),
    (
        # Matched at the indentation of the two-space layout, the only one
        # `apply` writes: the top-level key, and the two keys inside "env".
        re.compile(re.escape(models.SETTINGS_REL)),
        re.compile(
            rf' {{2}}"model":| {{4}}"(?:{models.SUBAGENT_ENV}|{models.CODEX_ENV})":'
        ),
        len,
        "settings keys, patched by apply",
    ),
    (
        re.compile(re.escape(models.CODEX_CONFIG_REL)),
        re.compile(r"(?:model|model_reasoning_effort)[ \t]*="),
        _toml_top_level_extent,
        "top-level config keys, patched by apply",
    ),
)
# The one-line escape hatch, for the model rules only. The reason is required
# and must start with a word (a comment terminator such as `-->` is not one):
# it is what a reviewer reads to decide whether the exception still holds.
ALLOW_MARKER_RE = re.compile(r"models-lint: allow[ \t]+(?P<reason>\w.*)")
# What may close the comment a marker sits in; not part of the reason.
COMMENT_CLOSERS = ("-->", "*/")

# Variables that point git at another repository, index or work tree. A scan
# started from inside a hook or a wrapper must still list *this* tree.
GIT_ENV_DROPPED = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
FILE_LEVEL_LINE = 1

EXIT_OK = models.EXIT_OK
EXIT_BAD_ARGS = models.EXIT_BAD_ARGS
EXIT_FINDINGS = 2
EXIT_FAILED = models.EXIT_FAILED


class NotTopLevel(Exception):
    """The project root is not the top level of a git work tree."""


class ScanFailed(Exception):
    """git could not list the tree."""


@dataclass(frozen=True)
class Finding:
    """One thing that fails the gate, at a place a reader can open."""

    file: str
    line: int
    rule: str
    text: str


@dataclass(frozen=True)
class Allowed:
    """One line a marker excused, with what it excused and the stated reason."""

    file: str
    line: int
    reason: str
    text: tuple[str, ...]


def exempt_rules(rel: str) -> frozenset[str]:
    """The text rules that do not apply to *rel*, from every matching entry."""
    exempt: set[str] = set()
    for path, rules, _reason in EXEMPTIONS:
        if rel.startswith(path) if path.endswith("/") else rel == path:
            exempt |= rules
    return frozenset(exempt)


def _generated_line_numbers(rel: str, lines: list[str]) -> set[int]:
    """The 1-based numbers of the lines in *rel* that `apply` writes."""
    numbers: set[int] = set()
    for file_re, line_re, extent, _reason in GENERATED_LINES:
        if not file_re.fullmatch(rel):
            continue
        numbers.update(
            index + 1 for index in range(extent(lines)) if line_re.match(lines[index])
        )
    return numbers


def _marker_reason(tail: str) -> str:
    """The stated reason, without the comment terminator that follows it."""
    reason = tail.rstrip()
    for closer in COMMENT_CLOSERS:
        if reason.endswith(closer):
            reason = reason[: -len(closer)].rstrip()
    return reason


def lint_text(rel: str, text: str) -> tuple[list[Finding], list[Allowed]]:
    """Every model name in *text* that *rel*'s exemptions do not cover.

    Lines are split on every line-break form, not only ``\\n``: a file written
    with bare carriage returns or Unicode separators would otherwise be one
    line, and one marker would excuse all of it.
    """
    exempt = exempt_rules(rel)
    rules = [(n, pattern) for n, pattern in TEXT_RULE_PATTERNS if n not in exempt]
    findings: list[Finding] = []
    allowed: list[Allowed] = []
    if not rules:
        return findings, allowed
    lines = text.splitlines()
    generated = _generated_line_numbers(rel, lines)
    for number, line in enumerate(lines, start=1):
        if number in generated:
            continue
        found: list[Finding] = []
        remaining = line
        for name, pattern in rules:
            found.extend(
                Finding(rel, number, name, match.group())
                for match in pattern.finditer(remaining)
            )
            remaining = pattern.sub(lambda match: " " * len(match.group()), remaining)
        marker = ALLOW_MARKER_RE.search(line)
        excused = [item for item in found if marker and item.rule in MODEL_RULES]
        if marker and excused:
            reason = _marker_reason(marker.group("reason"))
            texts = tuple(item.text for item in excused)
            allowed.append(Allowed(rel, number, reason, texts))
        findings.extend(item for item in found if item not in excused)
    return findings, allowed


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run git in *root* against this tree only, whatever the caller exported."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROPPED}
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise ScanFailed(f"git could not be run: {exc}") from exc


def listed_files(root: Path) -> list[str]:
    """Every path git tracks or would add under *root*, sorted, once each.

    *root* must be the top level: below it git lists paths relative to the
    subdirectory, and no exemption written from the top would match them.
    """
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise NotTopLevel(f"{root} is not inside a git work tree")
    top_level = os.fsdecode(top.stdout).rstrip("\n")
    # samefile, not a string comparison: the same directory can be spelled
    # through a symlink or, on a case-insensitive filesystem, in another case.
    if not os.path.samefile(top_level, root):
        raise NotTopLevel(
            f"{root} is not the top level of its git work tree ({top_level})"
        )
    sparse = _git(root, "config", "--bool", "core.sparseCheckout")
    if sparse.stdout.strip() == b"true":
        # Tracked files outside the sparse cone are not on disk to be read.
        raise ScanFailed("this is a sparse checkout; the lint needs the whole tree")
    listing = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listing.returncode != 0:
        detail = listing.stderr.decode("utf-8", "replace").strip()
        raise ScanFailed(f"git ls-files exited {listing.returncode}: {detail}")
    return sorted(
        {os.fsdecode(entry) for entry in listing.stdout.split(b"\0") if entry}
    )


def _through_symlink(root: Path, rel: str) -> bool:
    """True when *rel* is a symlink or lies below a symlinked directory."""
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _symlink_problem(root: Path, rel: str, plain: set[str]) -> str | None:
    """Why the link at (or above) *rel* is not vouched for, or None if it is.

    A link is fine only when what it resolves to is itself a scanned file held
    to at least the rules that apply where the link sits. Anything else is
    text this gate never read: a file outside the project, an ignored or
    exempt file, a directory.
    """
    if not os.path.exists(root / rel):
        return "a dangling symlink"
    real_root = os.path.realpath(root)
    target = os.path.realpath(root / rel)
    if not target.startswith(real_root + os.sep):
        return "resolves to a path outside the project"
    target_rel = Path(os.path.relpath(target, real_root)).as_posix()
    if target_rel not in plain:
        return f"resolves to {target_rel}, which is not a scanned file"
    if not exempt_rules(target_rel) <= exempt_rules(rel):
        return f"resolves to {target_rel}, which is excused from rules this path is not"
    if _excused_by_name(target_rel):
        # An allow-listed binary, or a file with generated lines, is excused
        # as that file. Under another name the same bytes are ordinary text.
        return f"resolves to {target_rel}, which is excused only under its own name"
    return None


def _excused_by_name(rel: str) -> bool:
    """True when *rel* is an allow-listed binary or carries generated lines."""
    if any(rel == path for path, _reason in BINARY_ALLOWLIST):
        return True
    return any(file_re.fullmatch(rel) for file_re, *_rest in GENERATED_LINES)


def _not_text_problem(data: bytes) -> str | None:
    """Why *data* cannot be read as text, or None when it can."""
    if b"\0" in data:
        return "contains a NUL byte (binary, or UTF-16 text)"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"is not valid UTF-8 (byte {exc.start})"
    return None


@dataclass(frozen=True)
class Scan:
    """What one run found. ``binary`` is the allow-listed files left unread."""

    findings: tuple[Finding, ...]
    allowed: tuple[Allowed, ...]
    binary: tuple[str, ...]
    scanned: int


def scan(root: Path) -> Scan:
    """Lint every listed file under *root* that is not excused from all rules."""
    candidates = [
        rel for rel in listed_files(root) if not TEXT_RULES <= exempt_rules(rel)
    ]
    plain = {rel for rel in candidates if not _through_symlink(root, rel)}
    binary_ok = {path for path, _reason in BINARY_ALLOWLIST}
    findings: list[Finding] = []
    allowed: list[Allowed] = []
    binary: list[str] = []
    scanned = 0
    for rel in candidates:
        path = root / rel
        if rel not in plain:
            problem = _symlink_problem(root, rel, plain)
            if problem:
                findings.append(Finding(rel, FILE_LEVEL_LINE, RULE_SYMLINK, problem))
            continue
        if not os.path.lexists(path):
            # Deleted in the work tree and not staged yet: there is no text.
            continue
        if path.is_dir():
            below = rel.rstrip("/") + "/"
            if any(other.startswith(below) and other != rel for other in candidates):
                # A tracked file replaced by a directory, not staged yet: the
                # files now in it are listed, and scanned, under their own names.
                continue
            # git lists a nested repository as one entry and none of its files.
            reason = "is a directory git lists as one entry; its files are not scanned"
            findings.append(Finding(rel, FILE_LEVEL_LINE, RULE_NOT_TEXT, reason))
            continue
        if not path.is_file():
            # A FIFO, socket or device where a file is listed: reading it could
            # block for ever, and there is no text to vouch for.
            reason = "is not a regular file"
            findings.append(Finding(rel, FILE_LEVEL_LINE, RULE_NOT_TEXT, reason))
            continue
        data = path.read_bytes()
        problem = _not_text_problem(data)
        if problem and rel in binary_ok:
            binary.append(rel)
            continue
        scanned += 1
        if problem:
            findings.append(Finding(rel, FILE_LEVEL_LINE, RULE_NOT_TEXT, problem))
        # Scanned even so: whatever else is wrong in it is reported in one run.
        found, excused = lint_text(rel, data.decode("utf-8", "replace"))
        findings.extend(found)
        allowed.extend(excused)
    return Scan(tuple(findings), tuple(allowed), tuple(binary), scanned)


# =============================================================================
# CLI
# =============================================================================


def _emit(payload: dict[str, Any]) -> None:
    """Print the run's single JSON object to stdout (ASCII, so any path prints)."""
    print(json.dumps(payload))


def _note(line: str) -> None:
    """Print one finding to stderr as exactly one line, whatever it quotes."""
    safe = models.LINE_BREAKING_RE.sub(
        lambda match: f"\\x{ord(match.group()):02x}", line
    )
    print(safe.encode("utf-8", "backslashreplace").decode("utf-8"), file=sys.stderr)


def _fail(status: int, message: str) -> int:
    _note(f"error: {message}")
    _emit({"ok": False, "command": "lint", "error": message, "artifacts": []})
    return status


def _report(result: Scan) -> int:
    for finding in result.findings:
        text = json.dumps(finding.text, ensure_ascii=False)
        _note(f"lint: {finding.file}:{finding.line}: {finding.rule}: {text}")
    for item in result.allowed:
        _note(f"allowed: {item.file}:{item.line}: {item.reason}")
    if result.findings:
        _note(
            "hint: name the role, not the model; model values live only in "
            f"{models.MANIFEST_REL}"
        )
    clean = not result.findings
    _emit(
        {
            "ok": clean,
            "command": "lint",
            "result": "clean" if clean else "findings",
            "findings": [vars(finding) for finding in result.findings],
            "allowed": [
                {**vars(item), "text": list(item.text)} for item in result.allowed
            ],
            "binary": list(result.binary),
            "files_scanned": result.scanned,
            "artifacts": [],
            "error": None if clean else "model names found outside the manifest",
        }
    )
    return EXIT_OK if clean else EXIT_FINDINGS


def build_parser() -> models.JsonArgumentParser:
    parser = models.JsonArgumentParser(
        description=(
            "Fail on a model name written anywhere but the model manifest. Scans "
            "every file git tracks or would add. An ordinary-English use of a "
            "model-family word is flagged by design: reword the sentence."
        ),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help=(
            "Top level of the git work tree to scan (defaults to 4 levels above "
            "this script)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root: Path = args.project_root
    try:
        result = scan(root)
    except NotTopLevel as exc:
        return _fail(EXIT_BAD_ARGS, str(exc))
    except (ScanFailed, OSError) as exc:
        # A file that could not be read holds text nobody checked.
        return _fail(EXIT_FAILED, str(exc))
    if result.scanned == 0:
        # An empty scan must never read as a clean one.
        return _fail(EXIT_FAILED, f"lint found no file to scan under {root}")
    return _report(result)


if __name__ == "__main__":
    sys.exit(main())
