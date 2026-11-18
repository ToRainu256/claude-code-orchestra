#!/usr/bin/env python3
"""Read, edit, render, and check the orchestration model manifest.

``models.json`` beside this script is the one file that says which model each
role runs on. Every other place a model value lives is a copy of it:

* **Rendered** (template-owned, always rewritten): the ``model:`` frontmatter
  line of every ``.claude/agents/*.md``.
* **Applied** (project-owned, patched one key at a time): ``model`` and
  ``env.CLAUDE_CODE_SUBAGENT_MODEL`` in ``.claude/settings.json``, and the
  top-level ``model`` and ``model_reasoning_effort`` in ``.codex/config.toml``.
  ``env.CODEX_MODEL`` is removed: a settings-file ``env`` value is exported to
  every child process and would silently shadow the manifest.
* **Read at runtime**: ``codex_consult.py`` imports :func:`resolve`.

A project-owned key is advanced only when it is absent, already equal to the
target, or equal to a value a template version shipped (the manifest's
``superseded`` list, plus the manifest's own value when the project overrides
it locally). Any other value is a deliberate project choice: it is
kept and reported as ``custom``. ``.claude/models.local.json`` overrides single
keys (``null`` leaves one key unmanaged) and
``"manage_native_settings": false`` stops this script from touching the two
project-owned files at all.

``apply`` is all-or-nothing. Every file is computed first; a layout this script
cannot edit with certainty (reformatted JSON, a quoted or duplicated TOML key,
a multi-line TOML value) is refused rather than guessed at, and one refusal
means nothing is written anywhere. Writes go through a temporary file and an
atomic rename that keeps the file mode, and each renamed file is read back.
An interrupt (Ctrl-C, SIGTERM, SIGHUP) restores what was already renamed and
then ends the process with that same signal, so a calling script stops too.

``check`` writes nothing. In an installed project a value the project chose is
reported as ``custom`` and passes. ``check --strict`` is for the template
itself, where nothing is a project choice: a custom value, a managed file that
is skipped, or a local override is a failure there.

Stdout carries exactly one JSON object. The same findings are repeated on
stderr as one ``changed:`` / ``drift:`` / ``custom:`` / ``refused:`` /
``skipped:`` line each, which is what the shell callers print. They decide
what happened from the exit code alone.

Why one file of this size: it is a single pipeline, load -> plan -> commit,
whose stages only make sense together, and its bulk is three editors (JSON,
TOML, YAML frontmatter) that must change one value without a parser, because
the script runs on the macOS system Python 3.9 where none is available. Each
refusal rule in them stands for an input that would otherwise be mis-edited.
Every ``.py`` under ``.claude/skills/`` must also be a complete CLI under the
Shared Script Contract, so splitting it would add scaffolding, not clarity.

Usage:
    python3 models.py get codex.model
    python3 models.py set codex.model gpt-6.2-astra
    python3 models.py apply
    python3 models.py check
    python3 models.py check --strict
    python3 models.py --project-root DIR check

Exit codes:
    0  ok: value resolved, manifest rewritten, or every location in sync
    1  bad arguments, an unknown key, or an unreadable or invalid manifest or
       local override
    2  check found drift, a file it cannot verify, or (with --strict) a value
       the manifest does not name; apply refused a layout and wrote nothing
    3  a write failed and the files already replaced were restored, or the
       tree could not be read
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

SCHEMA_VERSION = 1
MANIFEST_REL = ".claude/skills/_shared/models.json"
LOCAL_REL = ".claude/models.local.json"
SETTINGS_REL = ".claude/settings.json"
CODEX_CONFIG_REL = ".codex/config.toml"
AGENTS_DIR_REL = ".claude/agents"
AGENT_SUFFIX = ".md"
APPLY_COMMAND = "python3 .claude/skills/_shared/models.py apply"

KEY_MAIN = "claude.main"
KEY_SUBAGENT = "claude.subagent_default"
KEY_CODEX_MODEL = "codex.model"
KEY_EFFORT = "codex.reasoning_effort"
AGENT_PREFIX = "claude.agents."
# Keys that land in project-owned files. Only these carry a `superseded`
# history: agent frontmatter is template-owned and rendered unconditionally.
NATIVE_KEYS = (KEY_MAIN, KEY_SUBAGENT, KEY_CODEX_MODEL, KEY_EFFORT)
# Keys the template manifest itself may set to null ("do not manage").
NULLABLE_KEYS = (KEY_MAIN, KEY_SUBAGENT)
NULL_LITERAL = "null"

MANIFEST_TOP_KEYS = ("$comment", "schema_version", "claude", "codex", "superseded")
LOCAL_TOP_KEYS = (
    "$comment",
    "schema_version",
    "manage_native_settings",
    "claude",
    "codex",
)
CLAUDE_SECTION_KEYS = ("main", "subagent_default", "agents")
CODEX_SECTION_KEYS = ("model", "reasoning_effort")

# Neither pattern can match a quote, whitespace, or a newline. That is a
# security property, not tidiness: the values are written into YAML
# frontmatter, TOML and JSON, and a value that cannot hold a quote or a newline
# cannot inject a key. They are applied with fullmatch because `$` would accept
# a trailing newline.
CLAUDE_MODEL_RE = re.compile(
    r"(fable|opus|sonnet|haiku|inherit|claude-[a-z]+-[0-9][a-z0-9-]*)(\[1m\])?"
)
CODEX_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
# An agent name becomes a file name, so it must not be able to leave the
# agents directory.
AGENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

SUBAGENT_ENV = "CLAUDE_CODE_SUBAGENT_MODEL"
CODEX_ENV = "CODEX_MODEL"
JSON_INDENT = 2

TOML_MANAGED_KEYS = (("model", KEY_CODEX_MODEL), ("model_reasoning_effort", KEY_EFFORT))
TOML_TABLE_HEADER_RE = re.compile(r"^[ \t]*\[\[?[^\[\]]+\]\]?[ \t]*(#.*)?$")
TOML_ASSIGNMENT_RE = re.compile(r"""^[ \t]*[A-Za-z0-9_."'-]+[ \t]*=[ \t]*\S""")
TOML_LOOSE_KEY_RE = re.compile(
    r"""^\s*["']?(model|model_reasoning_effort)["']?\s*[.=]"""
)
TOML_STRICT_KEY_RE = re.compile(
    r"^(?P<k>model|model_reasoning_effort)(?P<sp>[ \t]*=[ \t]*)"
    r'"(?P<v>[^"\\]*)"(?P<rest>[ \t]*(#.*)?)$'
)
TOML_MULTILINE_QUOTES = ('"""', "'''")
TOML_OPENERS = "[{"
TOML_CLOSERS = "]}"
TOML_QUOTES = "\"'"
TOML_MANAGED_NAMES = tuple(name for name, _ in TOML_MANAGED_KEYS)
# Whitespace and quotes, removed from a key so `a . "b"` and `a.b` compare equal.
TOML_KEY_NOISE_RE = re.compile(r"""[\s"']""")
# One component of a TOML key: bare, a basic string, or a literal string.
TOML_COMPONENT = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')"""
# A complete table or array-of-tables header. A table whose first component is
# a managed key (`[model]`, `[[model]]`, `[model.sub]`, `["model"]`) defines
# that key as a table, so a scalar of the same name at the top level would be
# a duplicate definition.
TOML_HEADER_SHAPE_RE = re.compile(
    rf"^\s*\[\[?\s*(?P<first>{TOML_COMPONENT})"
    rf"(?:\s*\.\s*{TOML_COMPONENT})*\s*\]\]?\s*(#.*)?$"
)

FRONTMATTER_FENCE = "---"
# A frontmatter mapping line: an unquoted key and the rest of the line as its
# value. Applied to top-level lines and, with the indentation and any `- `
# removed, to nested ones.
FRONTMATTER_KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_-]+):(?:[ \t]+(?P<value>.*))?$")
FRONTMATTER_SEQUENCE_ENTRY_RE = re.compile(r"^-(?:[ \t]+(?P<entry>.*))?$")
# `|` or `>` with optional chomping and indentation indicators: the lines that
# follow, indented deeper, are free text.
FRONTMATTER_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?[0-9]?[+-]?[ \t]*(#.*)?$")
# A quoted scalar that opens and closes on the same line.
FRONTMATTER_CLOSED_QUOTED_RE = re.compile(
    r"""^(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*')[ \t]*(#.*)?$"""
)
FRONTMATTER_MODEL_KEY = "model"
# The value must look like a model name. Anything else in that position (a
# block-scalar indicator such as `>-`, an anchor, a tag) is not a value this
# script can replace by substitution.
FRONTMATTER_MODEL_RE = re.compile(
    r"""^model:(?P<sp>[ \t]*)(?P<q>["']?)(?P<v>[A-Za-z0-9][A-Za-z0-9._\[\]-]*)"""
    r"""(?P=q)(?P<rest>[ \t]*)$"""
)

ACTION_KEEP = "keep"
ACTION_SET = "set"
ACTION_CUSTOM = "custom"

TEMP_MARKER = ".models-tmp"
# Every directory this script stages a temporary file in. A run that was
# killed outright leaves its temporary files there; the next apply removes
# them, and check reports them.
TEMP_DIRS = (
    ".claude",
    ".codex",
    AGENTS_DIR_REL,
    ".claude/skills/_shared",
)
# Signals that end a run the way Ctrl-C does: restore, then die of the signal.
TERMINATION_SIGNALS = ("SIGTERM", "SIGHUP")
SIGNAL_EXIT_BASE = 128
# Control characters and the Unicode line separators: a finding is one stderr
# line, whatever a project put in the value being reported.
LINE_BREAKING_RE = re.compile("[\\x00-\\x1f\\x7f\\x85\\u2028\\u2029]")
OPT_OUT_REASON = (
    f'"manage_native_settings" is false in {LOCAL_REL}; the file is left to the project'
)

# "Refused, nothing written" and "a write failed" are different exit codes on
# purpose: the shell callers continue after the first and fail on the second.
EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_DRIFT = 2
EXIT_REFUSED = 2
EXIT_FAILED = 3


class ManifestError(Exception):
    """The manifest or the local override is missing, unreadable, or invalid."""


class LayoutRefused(Exception):
    """A managed file has a layout this script will not edit or verify."""


class Terminated(KeyboardInterrupt):
    """SIGTERM or SIGHUP, raised so it takes the same rollback path as Ctrl-C."""

    def __init__(self, signum: int) -> None:
        super().__init__()
        self.signum = signum


@dataclass(frozen=True)
class Document:
    """One validated manifest or local override, flattened to dotted keys."""

    values: dict[str, str | None]
    superseded: dict[str, tuple[str, ...]]
    manage_native_settings: bool


@dataclass(frozen=True)
class Resolved:
    """The manifest with the local override merged in, leaf by leaf.

    ``shipped`` is every value some template version put in a project-owned
    file: the ``superseded`` history plus the manifest's own current value,
    which a local override turns into "the template default, not my choice".
    """

    values: dict[str, str | None]
    superseded: dict[str, tuple[str, ...]]
    shipped: dict[str, tuple[str, ...]]
    manage_native_settings: bool
    local_keys: frozenset[str]


@dataclass(frozen=True)
class Change:
    """One managed value that differs from its target. ``new`` None = removed."""

    file: str
    key: str
    old: str | None
    new: str | None


@dataclass(frozen=True)
class Custom:
    """A project-owned value that is neither the target nor a shipped default."""

    file: str
    key: str
    value: str
    target: str | None


@dataclass(frozen=True)
class Edit:
    """What one project-owned file needs. ``text`` is set only when it changes."""

    text: str | None = None
    changes: tuple[Change, ...] = ()
    custom: tuple[Custom, ...] = ()
    refusal: str | None = None


@dataclass(frozen=True)
class FileResult:
    """What one managed file needs. ``updated`` is set only when it changes."""

    file: str
    changes: tuple[Change, ...] = ()
    custom: tuple[Custom, ...] = ()
    refusal: str | None = None
    skipped: str | None = None
    original: bytes | None = None
    updated: bytes | None = None


# =============================================================================
# Manifest loading and validation
# =============================================================================


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object, rejecting a repeated key instead of keeping the last."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text: str) -> float:
    """Parse a JSON number, rejecting one too large to survive a rewrite."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range for a JSON number")
    return value


def parse_json_strict(text: str) -> Any:
    """Parse JSON; a duplicate key or a non-finite number is an error."""
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def canonical_json(value: Any) -> str:
    """The one JSON layout this script reads back byte-for-byte and writes."""
    return (
        json.dumps(value, indent=JSON_INDENT, ensure_ascii=False, allow_nan=False)
        + "\n"
    )


def value_problem(key: str, value: object) -> str | None:
    """Return why *value* is not acceptable for *key*, or None when it is."""
    if not isinstance(value, str):
        return f"{key} must be a string, got {type(value).__name__}"
    if key == KEY_EFFORT:
        if value in REASONING_EFFORTS:
            return None
        return f"{key} must be one of {', '.join(REASONING_EFFORTS)}, got {value!r}"
    pattern = CODEX_MODEL_RE if key == KEY_CODEX_MODEL else CLAUDE_MODEL_RE
    if pattern.fullmatch(value) is None:
        return f"{key} is not a valid model name: {value!r}"
    return None


def is_settable_key(key: str) -> bool:
    """True for a leaf the manifest can hold: a fixed key or an agent entry."""
    if key in NATIVE_KEYS:
        return True
    if not key.startswith(AGENT_PREFIX):
        return False
    return AGENT_NAME_RE.fullmatch(key[len(AGENT_PREFIX) :]) is not None


def _require_object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must be a JSON object")
    return value


def _reject_unknown(obj: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise ManifestError(
            f"{where} has unknown key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(allowed)}"
        )


def _collect_leaves(top: dict[str, Any], where: str) -> dict[str, Any]:
    """Flatten the nested ``claude`` and ``codex`` sections to dotted keys."""
    leaves: dict[str, Any] = {}
    claude = _require_object(top.get("claude", {}), f"{where}: claude")
    _reject_unknown(claude, CLAUDE_SECTION_KEYS, f"{where}: claude")
    for name in ("main", "subagent_default"):
        if name in claude:
            leaves[f"claude.{name}"] = claude[name]
    agents = _require_object(claude.get("agents", {}), f"{where}: claude.agents")
    for agent, value in agents.items():
        if AGENT_NAME_RE.fullmatch(agent) is None:
            raise ManifestError(f"{where}: claude.agents has an invalid name {agent!r}")
        leaves[AGENT_PREFIX + agent] = value
    codex = _require_object(top.get("codex", {}), f"{where}: codex")
    _reject_unknown(codex, CODEX_SECTION_KEYS, f"{where}: codex")
    for name in CODEX_SECTION_KEYS:
        if name in codex:
            leaves[f"codex.{name}"] = codex[name]
    return leaves


def _parse_superseded(top: dict[str, Any], where: str) -> dict[str, tuple[str, ...]]:
    raw = _require_object(top.get("superseded", {}), f"{where}: superseded")
    _reject_unknown(raw, NATIVE_KEYS, f"{where}: superseded")
    superseded: dict[str, tuple[str, ...]] = {}
    for key, entries in raw.items():
        if not isinstance(entries, list):
            raise ManifestError(f"{where}: superseded.{key} must be a list")
        for entry in entries:
            problem = value_problem(key, entry)
            if problem:
                raise ManifestError(f"{where}: superseded: {problem}")
        superseded[key] = tuple(entries)
    return superseded


def parse_document(doc: object, where: str, local: bool) -> Document:
    """Validate a manifest (or, with *local*, an override) and flatten it."""
    top = _require_object(doc, where)
    _reject_unknown(top, LOCAL_TOP_KEYS if local else MANIFEST_TOP_KEYS, where)
    version = top.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ManifestError(
            f"{where}: schema_version must be {SCHEMA_VERSION}, got {version!r}"
        )

    leaves = _collect_leaves(top, where)
    for key, value in leaves.items():
        if value is None and (local or key in NULLABLE_KEYS):
            continue
        problem = value_problem(key, value)
        if problem:
            raise ManifestError(f"{where}: {problem}")

    manage_native = top.get("manage_native_settings", True)
    if not isinstance(manage_native, bool):
        raise ManifestError(f"{where}: manage_native_settings must be true or false")
    if local:
        return Document(leaves, {}, manage_native)

    missing = [key for key in NATIVE_KEYS if key not in leaves]
    if missing or "agents" not in _require_object(top.get("claude", {}), where):
        names = ", ".join(missing or ["claude.agents"])
        raise ManifestError(f"{where}: missing required key(s) {names}")
    return Document(leaves, _parse_superseded(top, where), manage_native)


def _read_bytes(path: Path, where: str) -> bytes:
    """Read a manifest file, refusing anything but a regular file.

    Opening a FIFO or a device blocks: a stray one at the override's path
    would hang every apply, check and Codex call.
    """
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
        if regular:
            return path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"{where}: cannot read {path}: {exc}") from exc
    raise ManifestError(f"{where}: {path} is not a regular file")


def _parse_json_bytes(data: bytes, where: str) -> Any:
    try:
        return parse_json_strict(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{where}: not valid UTF-8: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"{where}: invalid JSON: {exc}") from exc


def _read_json(path: Path, where: str) -> Any:
    return _parse_json_bytes(_read_bytes(path, where), where)


def load(root: Path) -> Resolved:
    """Load the manifest under *root* and merge the project's local override."""
    manifest = parse_document(
        _read_json(root / MANIFEST_REL, MANIFEST_REL), MANIFEST_REL, local=False
    )
    shipped = {
        key: (
            *manifest.superseded.get(key, ()),
            *(value for value in (manifest.values[key],) if value is not None),
        )
        for key in NATIVE_KEYS
    }
    local_path = root / LOCAL_REL
    # lexists, so a dangling symlink is an unreadable override, not "no override".
    if not os.path.lexists(local_path):
        return Resolved(
            manifest.values, manifest.superseded, shipped, True, frozenset()
        )
    local = parse_document(_read_json(local_path, LOCAL_REL), LOCAL_REL, local=True)
    return Resolved(
        {**manifest.values, **local.values},
        manifest.superseded,
        shipped,
        local.manage_native_settings,
        frozenset(local.values),
    )


def _agent_models(resolved: Resolved) -> dict[str, str | None]:
    return {
        key[len(AGENT_PREFIX) :]: value
        for key, value in resolved.values.items()
        if key.startswith(AGENT_PREFIX)
    }


def resolve(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Return the merged manifest in the nested shape of ``models.json``.

    Raises ManifestError when the manifest or the local override is missing,
    unreadable, or invalid. A value of None means the key is unmanaged.
    """
    resolved = load(root)
    return {
        "claude": {
            "main": resolved.values[KEY_MAIN],
            "subagent_default": resolved.values[KEY_SUBAGENT],
            "agents": _agent_models(resolved),
        },
        "codex": {
            "model": resolved.values[KEY_CODEX_MODEL],
            "reasoning_effort": resolved.values[KEY_EFFORT],
        },
        "superseded": {key: list(v) for key, v in resolved.superseded.items()},
        "manage_native_settings": resolved.manage_native_settings,
    }


# =============================================================================
# Planning: what each managed file needs
# =============================================================================


def advance_action(
    present: bool, current: object, target: str | None, shipped: tuple[str, ...]
) -> str:
    """The advance-if-not-custom rule for one project-owned key."""
    if target is None:
        return ACTION_KEEP
    if not present:
        return ACTION_SET
    if current == target:
        return ACTION_KEEP
    if isinstance(current, str) and current in shipped:
        return ACTION_SET
    return ACTION_CUSTOM


def _as_text(value: object) -> str:
    """Render a found value for a report; non-strings are shown as JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def symlink_problem(root: Path, rel: str) -> str | None:
    """Return why writing *rel* could leave *root*, or None when it cannot.

    A managed file is replaced by renaming a temporary file over it. If the
    file, or any directory between it and the project root, is a symlink, that
    rename lands wherever the link points: a shared configuration directory,
    another project. Nothing outside the project is ever written.
    """
    current = root
    parts = Path(rel).parts
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            linked = current.relative_to(root).as_posix()
            return f"is reached through the symlinked directory {linked}"
    if (root / rel).is_symlink():
        return "is a symlink; only a regular file is rewritten"
    return None


def _read_regular_text(root: Path, rel: str) -> tuple[bytes, str]:
    """Read a managed file, refusing anything but a regular UTF-8 file."""
    problem = symlink_problem(root, rel)
    if problem:
        raise LayoutRefused(problem)
    path = root / rel
    if not path.is_file():
        raise LayoutRefused("is not a regular file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LayoutRefused(f"cannot be read ({exc})") from exc
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LayoutRefused("is not valid UTF-8") from exc


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, keeping endings, so joining restores the text.

    ``str.splitlines`` also breaks on form feeds and Unicode separators, which
    are legal inside a TOML or YAML string and must not start a "line" here.
    """
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _body(line: str) -> str:
    return line.rstrip("\r\n")


def _ending(line: str) -> str:
    return line[len(_body(line)) :]


def _describe_edit(change: Change) -> str:
    if change.new is None:
        return f"remove {change.key}"
    return f'set {change.key} to "{change.new}"'


def _plan_native(
    root: Path,
    rel: str,
    resolved: Resolved,
    edit: Callable[[str, str, Resolved], Edit],
) -> FileResult:
    """Plan one project-owned file: honour the opt-out, read it, apply *edit*."""
    if not resolved.manage_native_settings:
        return FileResult(rel, skipped=OPT_OUT_REASON)
    if not os.path.lexists(root / rel):
        return FileResult(rel, skipped="the file does not exist")
    original, text = _read_regular_text(root, rel)
    result = edit(rel, text, resolved)
    if result.refusal is not None or result.text is None:
        return FileResult(rel, custom=result.custom, refusal=result.refusal)
    return FileResult(
        rel,
        result.changes,
        result.custom,
        original=original,
        updated=result.text.encode("utf-8"),
    )


def _edit_settings(rel: str, text: str, resolved: Resolved) -> Edit:
    """Advance the managed keys of a ``settings.json`` text."""
    try:
        parsed = parse_json_strict(text)
    except ValueError as exc:
        raise LayoutRefused(f"is not valid JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise LayoutRefused("does not hold a JSON object at the top level")
    env = parsed.get("env", {})
    if not isinstance(env, dict):
        raise LayoutRefused('has an "env" value that is not a JSON object')

    expected = dict(parsed)
    new_env = dict(env)
    changes: list[Change] = []
    custom: list[Custom] = []
    for holder, name, label, key in (
        (expected, "model", "model", KEY_MAIN),
        (new_env, SUBAGENT_ENV, f"env.{SUBAGENT_ENV}", KEY_SUBAGENT),
    ):
        target = resolved.values[key]
        present = name in holder
        action = advance_action(
            present, holder.get(name), target, resolved.shipped[key]
        )
        if action == ACTION_SET:
            old = _as_text(holder[name]) if present else None
            changes.append(Change(rel, label, old, target))
            holder[name] = target
        elif action == ACTION_CUSTOM:
            custom.append(Custom(rel, label, _as_text(holder[name]), target))

    # env.CODEX_MODEL is removal-only: a shipped default would keep shadowing
    # the manifest for every wrapper call made from a Claude Code session.
    codex_target = resolved.values[KEY_CODEX_MODEL]
    if codex_target is not None and CODEX_ENV in new_env:
        current = new_env[CODEX_ENV]
        shipped = resolved.shipped[KEY_CODEX_MODEL]
        label = f"env.{CODEX_ENV}"
        if current == codex_target or (isinstance(current, str) and current in shipped):
            changes.append(Change(rel, label, _as_text(current), None))
            del new_env[CODEX_ENV]
        else:
            custom.append(Custom(rel, label, _as_text(current), codex_target))
    if new_env or "env" in parsed:
        expected["env"] = new_env

    if not changes:
        return Edit(custom=tuple(custom))
    # The file must be the canonical dump, so rewriting it changes nothing but
    # the managed keys. How it ends is the file's own business: a missing final
    # newline is an ordinary file, and whatever is there is written back.
    body = text.rstrip("\n")
    ending = text[len(body) :]
    if canonical_json(parsed).rstrip("\n") != body:
        edits = "; ".join(_describe_edit(change) for change in changes)
        return Edit(
            custom=tuple(custom),
            refusal=(
                "is not in the two-space `json.dumps` layout this script can "
                f"rewrite without reformatting it; edit it by hand: {edits}"
            ),
        )
    new_text = canonical_json(expected).rstrip("\n") + ending
    if parse_json_strict(new_text) != expected:
        raise LayoutRefused("the rewritten JSON did not parse back to the expected")
    return Edit(new_text, tuple(changes), tuple(custom))


def _line_is_complete(body: str, hash_needs_space: bool) -> bool:
    """True when every string and bracket opened on the line closes on it.

    Brackets and quotes inside a string, and anything inside a comment, are
    not structure. Counting them naively would read ``a = [ # ]`` as a
    finished array and take the next line for a table header. In TOML a ``#``
    outside a string always starts a comment; in YAML only one that follows
    whitespace does (*hash_needs_space*), so ``[Bash(echo a#b)]`` is complete.
    """
    depth = 0
    quote = ""
    escaped = False
    previous = " "
    for char in body:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char == "#" and (previous.isspace() or not hash_needs_space):
            break
        elif char in TOML_QUOTES:
            quote = char
        elif char in TOML_OPENERS:
            depth += 1
        elif char in TOML_CLOSERS:
            depth -= 1
            if depth < 0:
                return False
        previous = char
    return not quote and depth == 0


def _refuse_managed_tables(lines: list[str], start: int) -> None:
    """Refuse a file that defines a managed key as a table.

    The check is by line shape over the whole rest of the file, so a line
    inside a multi-line string further down can trigger it too. That errs
    toward refusing, which is the safe direction.
    """
    for index in range(start, len(lines)):
        match = TOML_HEADER_SHAPE_RE.match(_body(lines[index]))
        if match is None:
            continue
        first = match.group("first")
        if first.startswith('"') and "\\" in first:
            raise LayoutRefused(
                f"line {index + 1} names a table with an escaped key, which cannot "
                "be compared with the managed keys"
            )
        name = first[1:-1] if first.startswith(tuple(TOML_QUOTES)) else first
        if name in TOML_MANAGED_NAMES:
            raise LayoutRefused(
                f"line {index + 1} defines a table named {name}, which collides "
                "with the managed top-level key of that name"
            )


def _scan_toml_top_level(
    lines: list[str],
) -> tuple[dict[str, tuple[int, re.Match[str]]], int]:
    """Locate the managed keys in the region before the first table header.

    Returns the strict match per key and the index where the region ends.
    Every line of the region must be blank, a comment, or a complete
    single-line assignment: a multi-line string or array could contain a line
    that merely looks like ``model = "..."``, and a quoted, dotted, or indented
    managed key would read as absent and get a duplicate inserted. A key that
    is defined twice makes the file invalid TOML and is refused here, so the
    answer does not depend on whether the interpreter has a parser to say so.
    """
    found: dict[str, tuple[int, re.Match[str]]] = {}
    first_seen: dict[str, int] = {}
    for index, line in enumerate(lines):
        body = _body(line)
        stripped = body.strip()
        number = index + 1
        if not stripped or stripped.startswith("#"):
            continue
        if any(quote in body for quote in TOML_MULTILINE_QUOTES):
            raise LayoutRefused(f"line {number} holds a multi-line string")
        if stripped.startswith("["):
            if TOML_TABLE_HEADER_RE.match(body):
                _refuse_managed_tables(lines, index)
                return found, index
            raise LayoutRefused(f"line {number} is not a recognisable table header")
        if not TOML_ASSIGNMENT_RE.match(body) or not _line_is_complete(body, False):
            raise LayoutRefused(
                f"line {number} is not a complete single-line `key = value`"
            )
        key = TOML_KEY_NOISE_RE.sub("", body.split("=", 1)[0])
        if key in first_seen:
            raise LayoutRefused(
                f"line {number} defines {key} a second time "
                f"(first on line {first_seen[key]})"
            )
        first_seen[key] = number
        if not TOML_LOOSE_KEY_RE.match(body):
            continue
        strict = TOML_STRICT_KEY_RE.match(body)
        if strict is None:
            raise LayoutRefused(
                f"line {number} sets a managed key in a form that is not edited "
                'automatically; write it as a plain `key = "value"` line'
            )
        found[strict.group("k")] = (index, strict)
    return found, len(lines)


def _toml_insertion_index(lines: list[str], region_end: int) -> int:
    """Insert before the first table header, above the comments attached to it."""
    if region_end >= len(lines):
        return len(lines)
    index = region_end
    while index > 0 and _body(lines[index - 1]).lstrip().startswith("#"):
        index -= 1
    return index


def _toml_verification_problem(
    old_text: str, new_text: str, updates: dict[str, str]
) -> str | None:
    """Confirm with a real parser that only the managed keys changed.

    ``tomllib`` exists from Python 3.11. On older interpreters the refusal
    rules in :func:`_scan_toml_top_level` are the guarantee.
    """
    try:
        import tomllib
    except ImportError:
        return None
    try:
        old = tomllib.loads(old_text)
    except tomllib.TOMLDecodeError as exc:
        return f"is not valid TOML ({exc})"
    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        return f"would not be valid TOML after the edit ({exc})"
    if new != {**old, **updates}:
        return "would change more than the managed keys"
    return None


def _edit_codex_config(rel: str, text: str, resolved: Resolved) -> Edit:
    """Advance the managed top-level keys of a ``config.toml`` text."""
    lines = _split_lines(text)
    found, region_end = _scan_toml_top_level(lines)

    new_lines = list(lines)
    inserts: list[str] = []
    updates: dict[str, str] = {}
    changes: list[Change] = []
    custom: list[Custom] = []
    for toml_key, key in TOML_MANAGED_KEYS:
        target = resolved.values[key]
        located = found.get(toml_key)
        current = located[1].group("v") if located else None
        action = advance_action(
            located is not None, current, target, resolved.shipped[key]
        )
        if action == ACTION_CUSTOM and current is not None:
            custom.append(Custom(rel, toml_key, current, target))
        if action != ACTION_SET or target is None:
            continue
        changes.append(Change(rel, toml_key, current, target))
        updates[toml_key] = target
        if located is None:
            inserts.append(f'{toml_key} = "{target}"')
            continue
        index, match = located
        new_lines[index] = (
            f'{match.group("k")}{match.group("sp")}"{target}"{match.group("rest")}'
            + _ending(lines[index])
        )

    if not changes:
        return Edit(custom=tuple(custom))
    if inserts:
        newline = "\r\n" if "\r\n" in text else "\n"
        at = _toml_insertion_index(new_lines, region_end)
        block = [f"{entry}{newline}" for entry in inserts]
        if at < len(new_lines) and _body(new_lines[at]).strip():
            block.append(newline)
        if at == len(new_lines) and new_lines and not _ending(new_lines[-1]):
            new_lines[-1] += newline
        new_lines[at:at] = block
    new_text = "".join(new_lines)
    problem = _toml_verification_problem(text, new_text, updates)
    if problem:
        raise LayoutRefused(problem)
    return Edit(new_text, tuple(changes), tuple(custom))


def _yaml_value_problem(value: str) -> str | None:
    """Return why *value* may not end on its own line, or None when it does.

    A quoted scalar or a flow collection that stays open continues on the
    following lines, whatever their indentation, and those lines can hold text
    that merely looks like ``model: ...``.
    """
    if value.startswith(tuple(TOML_QUOTES)):
        if FRONTMATTER_CLOSED_QUOTED_RE.match(value) is None:
            return "opens a quoted value that does not end on the same line"
        return None
    if value.startswith(tuple(TOML_OPENERS)) and not _line_is_complete(value, True):
        return "opens a list or mapping that does not end on the same line"
    return None


def _frontmatter_model_lines(lines: list[str], closing: int) -> list[int]:
    """Return the indexes of the top-level ``model`` keys in a frontmatter block.

    Only a ``model:`` key at column 0 is the agent's model. Every line is still
    read, because what matters is that none of them can swallow the lines after
    it: a top-level line must be a plain ``key:`` line or a ``- `` list entry,
    the free text of a ``|`` or ``>`` block scalar is skipped by indentation,
    and any other value that opens a quote or a bracket must close it on the
    same line.
    """
    found: list[int] = []
    block_indent: int | None = None
    for index in range(1, closing):
        body = _body(lines[index])
        text = body.strip()
        if not text:
            continue
        indent = len(body) - len(body.lstrip(" \t"))
        if block_indent is not None and indent > block_indent:
            continue
        block_indent = None
        if text.startswith("#"):
            continue
        entry = FRONTMATTER_SEQUENCE_ENTRY_RE.match(text)
        if entry is not None:
            text = (entry.group("entry") or "").strip()
        key = FRONTMATTER_KEY_RE.match(text)
        if key is None and indent == 0 and entry is None:
            raise LayoutRefused(
                f"line {index + 1} is not a plain `key: value` frontmatter line"
            )
        value = text if key is None else (key.group("value") or "").strip()
        if FRONTMATTER_BLOCK_SCALAR_RE.match(value):
            block_indent = indent
        problem = _yaml_value_problem(value)
        if problem:
            raise LayoutRefused(f"line {index + 1} {problem}")
        top_level_key = key is not None and indent == 0 and entry is None
        if top_level_key and key.group("key") == FRONTMATTER_MODEL_KEY:
            found.append(index)
    return found


def _plan_agent_file(root: Path, name: str, target: str | None) -> FileResult:
    rel = f"{AGENTS_DIR_REL}/{name}.md"
    # An unmanaged agent (a null leaf in the local override) is not inspected:
    # it may legitimately have no `model:` line and inherit its model.
    if target is None:
        return FileResult(rel)
    original, text = _read_regular_text(root, rel)
    lines = _split_lines(text)
    if not lines or _body(lines[0]) != FRONTMATTER_FENCE:
        raise LayoutRefused("does not start with a `---` frontmatter block")
    closing = next(
        (i for i in range(1, len(lines)) if _body(lines[i]) == FRONTMATTER_FENCE),
        None,
    )
    if closing is None:
        raise LayoutRefused("has an unclosed frontmatter block")
    candidates = _frontmatter_model_lines(lines, closing)
    if len(candidates) != 1:
        raise LayoutRefused(
            f"has {len(candidates)} `model:` lines in its frontmatter; exactly one "
            "is required"
        )
    index = candidates[0]
    match = FRONTMATTER_MODEL_RE.match(_body(lines[index]))
    if match is None:
        raise LayoutRefused(
            f"line {index + 1} is not a plain `model: <name>` frontmatter line"
        )
    # A plain scalar continues on the next indented line even across blank
    # ones, so the line to look at is the next one that holds anything.
    following = next(
        (
            body
            for body in (_body(lines[i]) for i in range(index + 1, closing))
            if body.strip()
        ),
        "",
    )
    if following.startswith((" ", "\t")):
        raise LayoutRefused(
            f"line {index + 1} has a `model:` value that continues on a later line"
        )
    current = match.group("v")
    if current == target:
        return FileResult(rel)
    quote = match.group("q")
    new_lines = list(lines)
    new_lines[index] = (
        f"model:{match.group('sp')}{quote}{target}{quote}{match.group('rest')}"
        + _ending(lines[index])
    )
    return FileResult(
        rel,
        (Change(rel, "model", current, target),),
        original=original,
        updated="".join(new_lines).encode("utf-8"),
    )


def _guard(rel: str, planner: Callable[[], FileResult]) -> FileResult:
    """Run one file's planner, turning a refusal into a result."""
    try:
        return planner()
    except LayoutRefused as exc:
        return FileResult(rel, refusal=str(exc))


def _plan_agents(root: Path, resolved: Resolved) -> list[FileResult]:
    """Render every agent definition, and require a model for each one.

    The manifest's agent keys must equal the set of definition files, so an
    agent added upstream cannot run on an unreviewed default.
    """
    assigned = _agent_models(resolved)
    agents_dir = root / AGENTS_DIR_REL
    # os.listdir, not glob: glob reports an unreadable directory as empty,
    # and "no agents" would then read as a refusal instead of an error.
    defined = (
        {
            name[: -len(AGENT_SUFFIX)]
            for name in os.listdir(agents_dir)
            if name.endswith(AGENT_SUFFIX)
        }
        if agents_dir.is_dir()
        else set()
    )
    results: list[FileResult] = []
    for name in sorted(defined | set(assigned)):
        rel = f"{AGENTS_DIR_REL}/{name}.md"
        if name not in assigned:
            reason = (
                f"has no {AGENT_PREFIX}{name} entry in the manifest; assign one "
                f"with: models.py set {AGENT_PREFIX}{name} <model> (in the "
                f"template), or under claude.agents in {LOCAL_REL} (in a project)"
            )
            results.append(FileResult(rel, refusal=reason))
            continue
        if name not in defined:
            reason = (
                f"does not exist, but the manifest assigns {AGENT_PREFIX}{name}; "
                "remove the entry or restore the agent definition"
            )
            results.append(FileResult(rel, refusal=reason))
            continue
        target = assigned[name]
        results.append(
            _guard(
                rel,
                lambda name=name, target=target: _plan_agent_file(root, name, target),
            )
        )
    return results


def build_plan(root: Path, resolved: Resolved) -> list[FileResult]:
    """Compute what every managed location needs, without writing anything."""
    native = ((SETTINGS_REL, _edit_settings), (CODEX_CONFIG_REL, _edit_codex_config))
    return [
        *_plan_agents(root, resolved),
        *(
            _guard(
                rel, lambda rel=rel, edit=edit: _plan_native(root, rel, resolved, edit)
            )
            for rel, edit in native
        ),
    ]


# =============================================================================
# Writing: staged, atomic, read back, and rolled back as a set
# =============================================================================


def _discard(path: Path) -> str | None:
    """Remove a temporary file; return why it could not be, never raise.

    It is called while handling a failure, where a second exception would
    skip the rollback that has to follow.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"temporary file {path} was left behind ({exc})"
    return None


def _stage(path: Path, data: bytes, mode: int) -> Path:
    """Write *data* to a temporary file beside *path* and return it."""
    temporary = path.with_name(f".{path.name}{TEMP_MARKER}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    staged = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # O_CREAT's mode is filtered by the umask; chmod makes it exact.
        os.chmod(temporary, mode)
        staged = True
    finally:
        # Covers an interrupt as well as an error, without catching either.
        if not staged:
            _discard(temporary)
    return temporary


def _roll_back(replaced: list[tuple[Path, bytes, bytes, int]]) -> list[str]:
    """Restore files that were replaced; return what could not be.

    An entry is recorded just before its rename, so a file may still hold its
    original bytes (the rename never happened) and then needs nothing. A file
    that holds neither the original nor what this run wrote was saved again by
    someone else in the meantime, and their content is left in place.
    """
    failures: list[str] = []
    for path, original, updated, mode in replaced:
        try:
            current = path.read_bytes()
            if current == original:
                continue
            if current != updated:
                failures.append(f"{path} was changed again and is left as it is")
                continue
            os.replace(_stage(path, original, mode), path)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    return failures


def _settle(exc: BaseException, message: str) -> str:
    """Hand back *message* for an error; re-raise an interrupt carrying it.

    An interrupt must stay an interrupt once the files are consistent again:
    turned into an ordinary exit code, a calling script would carry on with
    its remaining steps after the user pressed Ctrl-C.
    """
    if isinstance(exc, KeyboardInterrupt):
        exc.args = (message,)
        raise exc
    return message


def commit_writes(root: Path, pending: list[FileResult]) -> str | None:
    """Write every pending file or none of them; return an error message.

    Phase one stages a temporary copy of every file, which is where a full
    disk or a read-only directory fails, before anything live has changed.
    Phase two is the atomic renames. Each file is compared against the bytes
    read while planning immediately before it is renamed over, so a save made
    in the meantime is not overwritten, and is read back afterwards. If a
    rename fails, a file turns out to have changed, or the run is interrupted,
    the files already renamed are restored; an interrupt is then re-raised.

    Two gaps remain, and neither can be closed without a compare-and-swap
    rename the standard library does not offer: a save that lands between a
    file's final comparison and its rename is overwritten, and a process
    killed outright between two renames leaves the earlier ones in place.
    """
    staged: list[tuple[Path, Path, bytes, bytes, int]] = []
    try:
        for item in pending:
            if item.original is None or item.updated is None:
                continue
            problem = symlink_problem(root, item.file)
            if problem:
                raise OSError(f"{item.file} {problem}")
            path = root / item.file
            mode = stat.S_IMODE(path.stat().st_mode)
            temporary = _stage(path, item.updated, mode)
            staged.append((path, temporary, item.original, item.updated, mode))
    except (OSError, KeyboardInterrupt) as exc:
        leftovers = _discard_all(staged)
        message = f"nothing was written: {_failure_text(exc)}"
        return _settle(exc, "; ".join([message, *leftovers]))

    replaced: list[tuple[Path, bytes, bytes, int]] = []
    try:
        for path, temporary, original, updated, mode in staged:
            if path.read_bytes() != original:
                raise OSError(f"{path} changed while the update was being written")
            # Recorded first: an interrupt right after the rename must still
            # find this file in the list of what to restore.
            replaced.append((path, original, updated, mode))
            os.replace(temporary, path)
            if path.read_bytes() != updated:
                raise OSError(f"{path} does not hold the rendered content")
    except (OSError, KeyboardInterrupt) as exc:
        # Restore first: cleaning up may fail too, and must not prevent it.
        failures = _roll_back(replaced)
        leftovers = _discard_all(staged)
        reason = _failure_text(exc)
        if failures:
            message = f"write failed ({reason}) and could not be undone"
        else:
            message = f"write failed and every file was restored: {reason}"
        return _settle(exc, "; ".join([message, *failures, *leftovers]))
    return None


def _discard_all(staged: list[tuple[Path, Path, bytes, bytes, int]]) -> list[str]:
    """Remove every staged temporary file; return the ones that would not go."""
    results = [_discard(temporary) for _, temporary, _, _, _ in staged]
    return [result for result in results if result is not None]


def _failure_text(exc: BaseException) -> str:
    return "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)


def orphan_temporaries(root: Path) -> list[str]:
    """Temporary files a killed run left behind, as repo-relative paths.

    A run that is killed outright cannot clean up after itself. Its temporary
    files are untracked full copies of the files beside them, and the next
    ``git add -A`` would commit them. A directory reached through a symlink is
    not searched: nothing outside the project is ever removed.
    """
    found: list[str] = []
    for rel_dir in TEMP_DIRS:
        directory = root / rel_dir
        if symlink_problem(root, f"{rel_dir}/x") or not directory.is_dir():
            continue
        found.extend(
            f"{rel_dir}/{name}"
            for name in sorted(os.listdir(directory))
            if name.startswith(".") and f"{TEMP_MARKER}." in name
        )
    return found


# =============================================================================
# Reporting
# =============================================================================


def _emit(payload: dict[str, Any]) -> None:
    """Print the run's single JSON object to stdout."""
    print(json.dumps(payload, ensure_ascii=False))


def _note(line: str) -> None:
    """Print one human-readable finding to stderr, for the shell callers.

    The shell callers route each line by its prefix, so a line break inside a
    reported value must not be able to start a line of its own.
    """
    safe = LINE_BREAKING_RE.sub(lambda match: f"\\x{ord(match.group()):02x}", line)
    print(safe, file=sys.stderr)


def _shown(value: str | None, absent: str) -> str:
    return absent if value is None else json.dumps(value, ensure_ascii=False)


def _change_line(prefix: str, change: Change) -> str:
    old = _shown(change.old, "(absent)")
    new = _shown(change.new, "(removed)")
    return f"{prefix}: {change.file} {change.key}: {old} -> {new}"


def _custom_line(note: Custom) -> str:
    return (
        f"custom: {note.file} {note.key}: kept {_shown(note.value, '')} "
        f"(manifest: {_shown(note.target, '(unmanaged)')}); record it in "
        f"{LOCAL_REL} to silence this"
    )


def _plan_payload(plan: list[FileResult]) -> dict[str, list[dict[str, Any]]]:
    return {
        "custom": [vars(note) for item in plan for note in item.custom],
        "refused": [
            {"file": item.file, "reason": item.refusal}
            for item in plan
            if item.refusal is not None
        ],
        "skipped": [
            {"file": item.file, "reason": item.skipped}
            for item in plan
            if item.skipped is not None
        ],
    }


def _report_notes(plan: list[FileResult], custom: bool = True) -> None:
    for item in plan:
        for note in item.custom if custom else ():
            _note(_custom_line(note))
        if item.refusal is not None:
            _note(f"refused: {item.file} {item.refusal}")
        if item.skipped is not None:
            _note(f"skipped: {item.file}: {item.skipped}")


# =============================================================================
# Commands
# =============================================================================


def cmd_get(root: Path, key: str) -> int:
    resolved = load(root)
    if key not in resolved.values:
        known = ", ".join(sorted(resolved.values))
        _emit({"ok": False, "error": f"unknown key {key!r}; known keys: {known}"})
        return EXIT_BAD_ARGS
    source = "local" if key in resolved.local_keys else "manifest"
    _emit({"ok": True, "key": key, "value": resolved.values[key], "source": source})
    return EXIT_OK


def _with_leaf(doc: dict[str, Any], key: str, value: str | None) -> dict[str, Any]:
    """Return a copy of the nested manifest with one dotted leaf replaced."""
    section, _, leaf = key.partition(".")
    current = _require_object(doc.get(section, {}), section)
    if not leaf.startswith("agents."):
        return {**doc, section: {**current, leaf: value}}
    agents = _require_object(current.get("agents", {}), "claude.agents")
    name = leaf[len("agents.") :]
    return {**doc, section: {**current, "agents": {**agents, name: value}}}


def _with_superseded(
    doc: dict[str, Any], key: str, old: str | None, new: str | None
) -> dict[str, Any]:
    """Record the outgoing value as shipped, so installs advance past it."""
    history = _require_object(doc.get("superseded", {}), "superseded")
    entries = [entry for entry in history.get(key, []) if entry != new]
    if old is not None and old not in entries:
        entries.append(old)
    return {**doc, "superseded": {**history, key: entries}}


def cmd_set(root: Path, key: str, raw_value: str) -> int:
    # Read once: the same bytes are parsed and later compared before the
    # write, so a concurrent edit is detected instead of silently reverted.
    original = _read_bytes(root / MANIFEST_REL, MANIFEST_REL)
    doc = _parse_json_bytes(original, MANIFEST_REL)
    manifest = parse_document(doc, MANIFEST_REL, local=False)
    if not is_settable_key(key):
        _emit({"ok": False, "error": f"unknown key {key!r}"})
        return EXIT_BAD_ARGS
    value = None if raw_value == NULL_LITERAL else raw_value
    if value is None and key not in NULLABLE_KEYS:
        problem: str | None = (
            f"{key} cannot be null in the manifest; a project leaves a key "
            f"unmanaged with a null leaf in {LOCAL_REL}"
        )
    else:
        problem = None if value is None else value_problem(key, value)
    if problem:
        _emit({"ok": False, "error": problem})
        return EXIT_BAD_ARGS

    old = manifest.values.get(key)
    base = {"key": key, "old": old, "new": value}
    if key in manifest.values and old == value:
        _emit({"ok": True, "result": "no-op", **base, "artifacts": [], "error": None})
        return EXIT_OK

    updated = _with_leaf(doc, key, value)
    if key in NATIVE_KEYS:
        updated = _with_superseded(updated, key, old, value)
    parse_document(updated, MANIFEST_REL, local=False)
    pending = FileResult(
        MANIFEST_REL, original=original, updated=canonical_json(updated).encode("utf-8")
    )
    error = commit_writes(root, [pending])
    if error:
        _note(f"error: {error}")
        _emit(
            {"ok": False, "result": "failed", **base, "artifacts": [], "error": error}
        )
        return EXIT_FAILED
    _note(
        f"changed: {MANIFEST_REL} {key}: {_shown(old, '(absent)')} -> {_shown(value, 'null')}"
    )
    _note(f"hint: render the new value with: {APPLY_COMMAND}")
    _emit(
        {
            "ok": True,
            "result": "applied",
            **base,
            "artifacts": [MANIFEST_REL],
            "error": None,
        }
    )
    return EXIT_OK


def cmd_apply(root: Path) -> int:
    resolved = load(root)
    cleaned = orphan_temporaries(root)
    for rel in cleaned:
        (root / rel).unlink()
        _note(f"cleaned: {rel} (left behind by an earlier run that was killed)")
    plan = build_plan(root, resolved)
    pending = [item for item in plan if item.updated is not None]
    changes = [vars(change) for item in plan for change in item.changes]
    notes = {**_plan_payload(plan), "cleaned": cleaned}
    _report_notes(plan)

    error = None
    if notes["refused"]:
        error = "a managed file was refused; nothing was written"
        _note(
            "hint: nothing was written. Fix the refused file(s) above, or set "
            f'"manage_native_settings": false in {LOCAL_REL}, then run: {APPLY_COMMAND}'
        )
    else:
        error = commit_writes(root, pending)
        if error:
            _note(f"error: {error}")
    if error:
        refused = bool(notes["refused"])
        _emit(
            {
                "ok": False,
                "command": "apply",
                "result": "refused" if refused else "failed",
                "changes": [],
                "pending": changes,
                **notes,
                "artifacts": [],
                "error": error,
            }
        )
        return EXIT_REFUSED if refused else EXIT_FAILED

    for item in plan:
        for change in item.changes:
            _note(_change_line("changed", change))
    _emit(
        {
            "ok": True,
            "command": "apply",
            "result": "applied" if pending else "no-op",
            "changes": changes,
            "pending": [],
            **notes,
            "artifacts": [item.file for item in pending],
            "error": None,
        }
    )
    return EXIT_OK


def _strict_findings(root: Path, plan: list[FileResult]) -> list[str]:
    """What is acceptable in a project but not in the template itself.

    A project may choose a value, opt a file out, or carry an override. The
    template has no such choices: there, each of these is a managed location
    that does not hold what the manifest says, and the gate must not pass it.
    """
    findings = [
        f"{note.file} {note.key} holds {_shown(note.value, '')}, which the "
        "manifest does not name"
        for item in plan
        for note in item.custom
    ]
    findings.extend(
        f"{item.file} is not managed ({item.skipped})"
        for item in plan
        if item.skipped is not None
    )
    if os.path.lexists(root / LOCAL_REL):
        findings.append(f"{LOCAL_REL} is present; the template carries no override")
    return findings


def cmd_check(root: Path, strict: bool) -> int:
    plan = build_plan(root, load(root))
    drift = [change for item in plan for change in item.changes]
    payload = _plan_payload(plan)
    orphans = orphan_temporaries(root)
    strict_findings = _strict_findings(root, plan) if strict else []
    for change in drift:
        _note(_change_line("drift", change))
    # Under --strict a custom value is a finding of its own; the project
    # advice on the `custom:` line (record it in the override) is wrong there.
    _report_notes(plan, custom=not strict)
    for rel in orphans:
        _note(f"orphan: {rel} was left behind by a run that was killed")
    for finding in strict_findings:
        _note(f"strict: {finding}")

    in_sync = not (drift or payload["refused"] or orphans or strict_findings)
    if payload["refused"]:
        _note(
            "hint: fix the refused file(s) above, or set "
            f'"manage_native_settings": false in {LOCAL_REL}'
        )
    if drift or orphans:
        _note(f"hint: bring every location back in line with: {APPLY_COMMAND}")
    if strict_findings:
        _note(
            "hint: in the template every managed value comes from the manifest; "
            "change it with `models.py set`, not by hand"
        )
    _emit(
        {
            "ok": in_sync,
            "command": "check",
            "strict": strict,
            "result": "in-sync" if in_sync else "drift",
            "drift": [vars(change) for change in drift],
            **payload,
            "orphans": orphans,
            "strict_findings": strict_findings,
            "artifacts": [],
            "error": None
            if in_sync
            else "managed model locations differ from the manifest",
        }
    )
    return EXIT_OK if in_sync else EXIT_DRIFT


# =============================================================================
# CLI
# =============================================================================


class JsonArgumentParser(argparse.ArgumentParser):
    """Report usage errors as JSON on stdout with exit 1, like every outcome."""

    def error(self, message: str) -> NoReturn:
        _emit({"ok": False, "error": message})
        sys.exit(EXIT_BAD_ARGS)


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        description="Read, edit, render, and check the orchestration model manifest.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repository root (defaults to 4 levels above this script)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    get_parser = commands.add_parser("get", help="Print one resolved value")
    get_parser.add_argument("key", help="Dotted key, e.g. codex.model")
    set_parser = commands.add_parser("set", help="Change one value in the manifest")
    set_parser.add_argument("key", help="Dotted key, e.g. codex.model")
    set_parser.add_argument("value", help="New value")
    commands.add_parser("apply", help="Write every managed location from the manifest")
    check_parser = commands.add_parser(
        "check", help="Report drift without writing anything"
    )
    check_parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "For the template itself: also fail on a custom value, a skipped "
            "file, or a local override"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.project_root
    try:
        if args.command == "get":
            return cmd_get(root, args.key)
        if args.command == "set":
            return cmd_set(root, args.key, args.value)
        if args.command == "apply":
            return cmd_apply(root)
        return cmd_check(root, args.strict)
    except ManifestError as exc:
        _note(f"error: {exc}")
        _emit({"ok": False, "error": str(exc), "artifacts": []})
        return EXIT_BAD_ARGS
    except OSError as exc:
        # Planning only reads, and every write path reports its own failure and
        # rolls back, so what reaches here is an unreadable tree (for example a
        # directory that cannot be listed). It is an error, never a traceback.
        _note(f"error: {exc}")
        _emit({"ok": False, "error": str(exc), "artifacts": []})
        return EXIT_FAILED
    except KeyboardInterrupt as exc:
        # Reported like any other outcome, then passed on: the files are
        # consistent again, but the run did not finish and must not look as
        # if it had.
        message = str(exc) or "interrupted before anything was written"
        _note(f"error: {message}")
        _emit({"ok": False, "result": "interrupted", "error": message, "artifacts": []})
        raise


def _raise_terminated(signum: int, frame: object) -> NoReturn:
    raise Terminated(signum)


def run_as_script() -> NoReturn:
    """Run the CLI; an interrupt ends the process with the signal that caused it.

    Dying of the signal, rather than exiting with a code, is what lets a
    calling shell script see that it was interrupted and stop as well.
    """
    for name in TERMINATION_SIGNALS:
        signum = getattr(signal, name, None)
        # A signal the caller chose to ignore (`nohup`) stays ignored.
        if signum is not None and signal.getsignal(signum) is not signal.SIG_IGN:
            signal.signal(signum, _raise_terminated)
    try:
        sys.exit(main())
    except KeyboardInterrupt as exc:
        signum = getattr(exc, "signum", signal.SIGINT)
        # The report is still in the buffers, and a signal does not flush them.
        sys.stdout.flush()
        sys.stderr.flush()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        sys.exit(SIGNAL_EXIT_BASE + signum)


if __name__ == "__main__":
    run_as_script()
