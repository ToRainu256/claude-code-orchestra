#!/usr/bin/env python3
"""Decide which merged revision this Mac may execute and distribute.

Merging and promoting are separate. Green pull requests merge to the default
branch on their own; this machine runs only the **approved revision**, one
commit SHA in ``state/approved-rev`` under the state root. This module looks
at the range from that commit to ``refs/remotes/origin/main`` and advances the
file by itself in exactly two cases:

* **Inert range.** No changed path is one the Mac executes or ships: nothing
  under ``scripts/``, no ``VERSION``, nothing the approved ``scripts/update.sh``
  copies into a project (its ``SAFE_DIRS``, ``SAFE_FILES``, ``SETTINGS_FILES``
  and every literal ``${TEMPLATE_DIR}/…`` source), and no file git itself
  interprets at checkout.
* **This Mac's own model bump.** The executed-or-shipped changes are exactly
  the model manifest and the Codex configuration file, both regular files,
  differing only by one newer slug of the same family
  (:func:`bump_problem_between`), **and** the two resulting files are the ones
  this Mac's model watch built and made a live call with
  (:func:`recorded_bump`). The same change merged by any other means has no
  such record and waits.

Everything else waits for the owner to run ``launchd.sh promote``. There is no
setting that relaxes this: the rule is code in the approved revision, and a
merged change cannot alter the approved revision.

The comparison is between the two trees, not commit by commit: what matters is
the tree the Mac would run next, and a change that was merged and reverted
inside the range never runs here.

Nothing from the unapproved revision is executed. Its files are read with
``git show`` as data, and everything of it that is shown to the owner goes
through ``common.safe_text`` first.

Exit codes: see ``common.py``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import common

STEP = "promote"
RESULT_PROMOTED = "promoted"
RESULT_UP_TO_DATE = "up-to-date"
# Every outcome this step reports, with the exit codes it may carry. The
# README table is pinned to this by a test.
OUTCOMES: dict[str, tuple[int, ...]] = {
    "no-approved-revision": (common.EXIT_USAGE,),
    "git-failed": (common.EXIT_FAILED,),
    RESULT_UP_TO_DATE: (common.EXIT_OK,),
    "diverged": (common.EXIT_NEEDS_OWNER,),
    RESULT_PROMOTED: (common.EXIT_OK,),
    "awaiting-promotion": (common.EXIT_OK, common.EXIT_NEEDS_OWNER),
}

MANIFEST_REL = ".claude/skills/_shared/models.json"
CODEX_CONFIG_REL = ".codex/config.toml"
UPDATE_SCRIPT_REL = "scripts/update.sh"
MODEL_KEY = "codex.model"
MODEL_SECTION, MODEL_LEAF = MODEL_KEY.split(".")
SUPERSEDED = "superseded"
BUMP_RELS = (MANIFEST_REL, CODEX_CONFIG_REL)
BUMP_PATHS = frozenset(BUMP_RELS)
REGULAR_ENTRY = ("100644", "blob")

# Executed or shipped whatever the updater's lists say: the maintenance code
# and installers, the shared scripts the job runs, the version stamp, and the
# two settings files every project receives a merge candidate of.
ALWAYS_RELEVANT = (
    "scripts",
    "VERSION",
    ".claude/skills",
    ".claude/settings.json",
    CODEX_CONFIG_REL,
)
# Files git itself interprets, in any directory. They are not shipped by name,
# but they decide what a checkout writes for the files that are: an attribute
# can re-encode a script, change its line endings, or route it through a
# filter program configured on this machine.
GIT_CONTROL_NAMES = (".gitattributes", ".gitmodules", ".lfsconfig")
SHIPPED_ARRAYS = ("SAFE_DIRS", "SAFE_FILES", "SETTINGS_FILES")
TEMPLATE_SOURCE_RE = re.compile(r"\$\{TEMPLATE_DIR\}/([A-Za-z0-9._/-]+)")
# The updater reads this one from the template by a literal path. Not finding
# it means the scan above no longer understands the updater, so nothing is
# classified as inert.
SOURCE_CANARY = "VERSION"

# A change that has waited this long is the owner's to look at, not noise.
PROMOTION_OVERDUE_DAYS = 7
RELEVANT_SHOWN = 10
COMMITS_SHOWN = 50
DIVERGED_SHOWN = 20
STAT_LINES_SHOWN = 60
RECORDS_KEPT = 20
TOML_TABLE_RE = re.compile(r"^[ \t]*\[")
TOML_MULTILINE = ('"""', "'''")
TOML_MODEL_RE = re.compile(r'(model[ \t]*=[ \t]*")([^"\\\r\n]*)("[ \t]*(?:#[^\r\n]*)?)')
PULL_REFERENCE_RE = re.compile(r"\(#([1-9][0-9]{0,8})\)\s*$")
CONFIRM_WORD = "yes"

REASON_INERT = "inert-paths"
REASON_BUMP = "model-bump"


def parse_bash_array(text: str, name: str) -> tuple[str, ...] | None:
    """Read ``NAME=( "a" "b" )`` written one double-quoted literal per line.

    Returns None when the array is absent or holds anything but plain
    literals: a caller deciding what is safe must not guess at an expansion.
    """
    lines = text.splitlines()
    opening = f"{name}=("
    starts = [index for index, line in enumerate(lines) if line.strip() == opening]
    if len(starts) != 1:
        return None
    entries: list[str] = []
    for line in lines[starts[0] + 1 :]:
        body = line.strip()
        if body == ")":
            return tuple(entries)
        if not body or body.startswith("#"):
            continue
        match = re.fullmatch(r'"([A-Za-z0-9._/-]+)"', body)
        if match is None:
            return None
        entries.append(match.group(1))
    return None


def casefold_path(path: str) -> str:
    """A path as a case- and normalization-insensitive filesystem sees it.
    macOS would check two names differing only in case out onto one file."""
    return unicodedata.normalize("NFC", path).casefold()


def read_approved(paths: common.Paths) -> str | None:
    """The approved SHA, or None when the file is missing or is not one."""
    try:
        text = paths.approved_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if common.SHA_RE.fullmatch(text) else None


def write_approved(paths: common.Paths, sha: str) -> None:
    if not common.SHA_RE.fullmatch(sha):
        raise ValueError(f"not a commit SHA: {sha!r}")
    common.write_text_atomic(paths.approved_file, sha + "\n")


# --------------------------------------------------------------------------
# The shape of one model bump
# --------------------------------------------------------------------------


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _parse_manifest(text: str) -> dict[str, Any] | None:
    """The manifest as an object, refusing a duplicated key: a parser that
    keeps the last one would compare a different document than the tool
    reads."""
    try:
        parsed = json.loads(text, object_pairs_hook=_unique_object)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def manifest_model(text: str | None) -> str | None:
    """The model slug a manifest text names, or None."""
    document = _parse_manifest(text or "")
    section = document.get(MODEL_SECTION) if document else None
    value = section.get(MODEL_LEAF) if isinstance(section, dict) else None
    return value if isinstance(value, str) else None


def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, ensure_ascii=True)


def _history(document: dict[str, Any]) -> list[Any] | None:
    section = document.get(SUPERSEDED)
    if not isinstance(section, dict):
        return None
    entries = section.get(MODEL_KEY, [])
    return list(entries) if isinstance(entries, list) else None


def _manifest_problem(
    old_text: str, new_text: str
) -> tuple[str | None, common.Slug | None, common.Slug | None]:
    """Why the manifest change is not one model bump, with the two slugs."""
    old_doc = _parse_manifest(old_text)
    new_doc = _parse_manifest(new_text)
    if old_doc is None or new_doc is None:
        return "the manifest is not a JSON object with unique keys", None, None
    old_section = old_doc.get(MODEL_SECTION)
    new_section = new_doc.get(MODEL_SECTION)
    if not isinstance(old_section, dict) or not isinstance(new_section, dict):
        return f"the manifest has no {MODEL_SECTION} section", None, None
    old = common.parse_slug(old_section.get(MODEL_LEAF))
    new = common.parse_slug(new_section.get(MODEL_LEAF))
    if old is None or new is None:
        return f"{MODEL_KEY} is not a <vendor>-<version>-<family> slug", old, new
    if not new.same_line(old):
        return f"{MODEL_KEY} changes family: {old.text} -> {new.text}", old, new
    if new.version <= old.version:
        return f"{MODEL_KEY} does not move forward: {old.text} -> {new.text}", old, new

    old_history = _history(old_doc)
    new_history = _history(new_doc)
    if old_history is None or new_history is None:
        return f"{SUPERSEDED} has no list for {MODEL_KEY}", old, new
    kept = [entry for entry in old_history if entry != new.text]
    added = new_history[len(kept) :]
    if new_history[: len(kept)] != kept or old.text not in new_history:
        return f"{SUPERSEDED} does not record {old.text}", old, new
    if len(set(added)) != len(added):
        return f"{SUPERSEDED} repeats an entry", old, new
    for entry in added:
        step = common.parse_slug(entry)
        if step is None or not step.same_line(old):
            return f"{SUPERSEDED} gains a value of another family", old, new
        if not old.version <= step.version < new.version:
            return f"{SUPERSEDED} gains a value outside the bump", old, new

    expected = {
        **old_doc,
        MODEL_SECTION: {**old_section, MODEL_LEAF: new.text},
        SUPERSEDED: {**old_doc[SUPERSEDED], MODEL_KEY: new_history},
    }
    # Compared as text: as objects, `true` equals `1` and `1.0` equals `1`.
    if _canonical(expected) != _canonical(new_doc):
        return "the manifest changes more than the model and its history", old, new
    return None, old, new


def _toml_problem(old_text: str, new_text: str, old: str, new: str) -> str | None:
    """Why the configuration change is not the one top-level model line.

    Lines are split at line feeds only and compared byte for byte, so a
    carriage return added anywhere is a change like any other: it cannot
    hide inside a line that "only" changed its model.
    """
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    if len(old_lines) != len(new_lines):
        return f"{CODEX_CONFIG_REL} gains or loses lines"
    changed = [
        index for index in range(len(old_lines)) if old_lines[index] != new_lines[index]
    ]
    if len(changed) != 1:
        return f"{CODEX_CONFIG_REL} changes {len(changed)} lines, not one"
    index = changed[0]
    if any(TOML_TABLE_RE.match(line) for line in old_lines[:index]):
        return f"{CODEX_CONFIG_REL} changes a line inside a table"
    # A multi-line string above the line could contain it: text that looks
    # like the model key and is not one. Refused rather than tokenized.
    if any(quote in line for line in old_lines[:index] for quote in TOML_MULTILINE):
        return f"{CODEX_CONFIG_REL} has a multi-line string above the changed line"
    before = TOML_MODEL_RE.fullmatch(old_lines[index])
    after = TOML_MODEL_RE.fullmatch(new_lines[index])
    if before is None or after is None:
        return f"{CODEX_CONFIG_REL} changes a line that is not the model"
    if (before.group(1), before.group(3)) != (after.group(1), after.group(3)):
        return f"{CODEX_CONFIG_REL} changes more than the model value"
    if before.group(2) != old or after.group(2) != new:
        return f"{CODEX_CONFIG_REL} does not follow the manifest"
    return None


def model_bump_problem(
    old_manifest: str,
    new_manifest: str,
    old_config: str,
    new_config: str,
    expected: str | None = None,
) -> str | None:
    """None when the four texts are exactly one model bump, else the reason.

    A bump changes ``codex.model`` to a strictly newer slug of the same vendor
    and family, appends the outgoing value to that key's ``superseded`` list,
    and rewrites the one top-level ``model`` line of the configuration file.
    Several bumps in a row count as one. ``expected`` pins the new slug.
    """
    problem, old, new = _manifest_problem(old_manifest, new_manifest)
    if problem is not None or old is None or new is None:
        return problem
    if expected is not None and new.text != expected:
        return f"{MODEL_KEY} is {new.text}, not {expected}"
    return _toml_problem(old_config, new_config, old.text, new.text)


def changed_paths(git: common.Git, base: str, head: str) -> list[str] | None:
    result = git.run(["diff", "--name-only", "-z", "--no-renames", base, head])
    if not result.ok:
        return None
    return [path for path in result.stdout.split("\0") if path]


def tree_entry(git: common.Git, revision: str, rel: str) -> tuple[str, str, str] | None:
    """(mode, type, object id) of one path at a revision, or None."""
    listed = git.text(["ls-tree", revision, "--", rel])
    if not listed or "\n" in listed or "\t" not in listed:
        return None
    fields = listed.split("\t", 1)[0].split()
    return (fields[0], fields[1], fields[2]) if len(fields) == 3 else None


def bump_problem_between(
    git: common.Git,
    base: str,
    head: str,
    expected: str | None = None,
    only: bool = True,
) -> str | None:
    """None when ``head`` differs from ``base`` by exactly one model bump.

    The one check behind every decision that treats a change as "only the
    model": the promotion rule, the bump the model watch is about to push, and
    a bump branch found on the fork. Both files must be regular files (mode
    100644) at both commits: ``git show`` returns a blob's bytes whatever the
    entry is, and a link whose target text reads like the new file would pass
    a comparison of contents alone. With ``only``, no other path may differ.
    """
    if only:
        names = changed_paths(git, base, head)
        if names is None:
            return "cannot list the changed paths"
        if set(names) != BUMP_PATHS:
            touched = ", ".join(sorted(names)) or "(nothing)"
            return f"the change touches: {touched}"
    texts: list[str] = []
    for rel in BUMP_RELS:
        for revision in (base, head):
            entry = tree_entry(git, revision, rel)
            if entry is None or entry[:2] != REGULAR_ENTRY:
                return f"{rel} is not a regular file with mode {REGULAR_ENTRY[0]}"
            text = git.show(revision, rel)
            if text is None:
                return f"cannot read {rel}"
            texts.append(text)
    old_manifest, new_manifest, old_config, new_config = texts
    return model_bump_problem(
        old_manifest, new_manifest, old_config, new_config, expected
    )


# --------------------------------------------------------------------------
# What this Mac's model watch vouched for
# --------------------------------------------------------------------------


def _read_records(paths: common.Paths) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(paths.bump_records_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def record_bump(ctx: common.Context, slug: str, commit: str) -> bool:
    """Remember that this Mac built ``commit`` as the bump to ``slug`` and made
    a live call with it: the slug and the two resulting file objects. False
    when the commit's files cannot be read."""
    blobs: dict[str, str] = {}
    for rel in BUMP_RELS:
        entry = tree_entry(ctx.git, commit, rel)
        if entry is None or entry[:2] != REGULAR_ENTRY:
            return False
        blobs[rel] = entry[2]
    record = {
        "slug": slug,
        "commit": commit,
        "blobs": blobs,
        "recorded": common.utc_stamp(ctx.now),
    }
    others = [item for item in _read_records(ctx.paths) if item.get("slug") != slug]
    kept = [*others, record][-RECORDS_KEPT:]
    common.write_text_atomic(ctx.paths.bump_records_file, json.dumps(kept, indent=2))
    return True


def recorded_bump_problem(ctx: common.Context, target: str) -> str | None:
    """None when the two model files at ``target`` are byte for byte the ones
    this Mac's model watch recorded for that slug, else why not."""
    slug = manifest_model(ctx.git.show(target, MANIFEST_REL))
    records = [item for item in _read_records(ctx.paths) if item.get("slug") == slug]
    if slug is None or not records:
        return "no model watch on this Mac tested this bump"
    blobs = records[-1].get("blobs")
    for rel in BUMP_RELS:
        entry = tree_entry(ctx.git, target, rel)
        if entry is None or not isinstance(blobs, dict) or blobs.get(rel) != entry[2]:
            return f"{rel} is not the file the model watch tested"
    return None


# --------------------------------------------------------------------------
# What the Mac executes or ships
# --------------------------------------------------------------------------


def shipped_paths(update_script: str | None) -> tuple[str, ...] | None:
    """Every path the Mac executes or ships, read from the approved updater.

    None means the updater could not be understood, and then no range is
    inert: the caller must wait for the owner rather than guess.
    """
    if update_script is None:
        return None
    found = list(ALWAYS_RELEVANT)
    for name in SHIPPED_ARRAYS:
        entries = parse_bash_array(update_script, name)
        if not entries:
            return None
        found.extend(entries)
    sources = TEMPLATE_SOURCE_RE.findall(update_script)
    if SOURCE_CANARY not in sources:
        return None
    found.extend(source.rstrip("/") for source in sources)
    return tuple(sorted(set(found)))


def relevant_paths(changed: list[str], shipped: tuple[str, ...] | None) -> list[str]:
    """The changed paths the Mac executes or ships.

    A path counts when it is a shipped entry, lies below one, or is a parent
    of one (a directory replaced by a file or a link). Names are compared the
    way a case-insensitive filesystem would check them out, and a name that
    is not plain ASCII is never inert.
    """
    if shipped is None:
        return sorted(changed)
    folded = [casefold_path(entry) for entry in shipped]
    relevant: list[str] = []
    for path in changed:
        name = casefold_path(path)
        related = any(
            name == entry
            or name.startswith(entry + "/")
            or entry.startswith(name + "/")
            for entry in folded
        )
        controls_checkout = name.rsplit("/", 1)[-1] in GIT_CONTROL_NAMES
        plain = path.isascii() and path.isprintable()
        if related or controls_checkout or not plain:
            relevant.append(path)
    return sorted(relevant)


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """What the range ``approved..target`` amounts to."""

    action: str  # up-to-date | promote | await | diverged | unconfigured | failed
    approved: str | None = None
    target: str | None = None
    reason: str = ""
    relevant: tuple[str, ...] = ()
    commits: int = 0
    oldest: float | None = None


def waiting_range(
    git: common.Git, approved: str, target: str
) -> tuple[int, float | None] | None:
    """How many changes landed on the default branch after ``approved``, and
    when the oldest of them landed. None when git cannot say.

    First parents only: one merge is one change that landed when it was
    merged, however many commits it brought and however old those are.
    """
    span = f"{approved}..{target}"
    stamps = git.text(["log", "--first-parent", "--format=%ct", span])
    if stamps is None:
        return None
    values = [int(line) for line in stamps.split() if line.isdigit()]
    return len(values), float(min(values)) if values else None


def evaluate(ctx: common.Context) -> Decision:
    """Classify the range from the approved revision to the default branch.
    Reads only; decides nothing else."""
    approved = read_approved(ctx.paths)
    if approved is None:
        return Decision("unconfigured", reason="no approved revision recorded")
    target = ctx.git.commit_of(common.MAIN_REF)
    if target is None:
        return Decision("failed", approved, reason=f"cannot read {common.MAIN_REF}")
    if target == approved:
        return Decision("up-to-date", approved, target)

    ancestry = ctx.git.run(["merge-base", "--is-ancestor", approved, target])
    if ancestry.returncode == 1:
        return Decision(
            "diverged",
            approved,
            target,
            reason=f"the approved revision is not an ancestor of {common.MAIN_REF}",
        )
    if not ancestry.ok:
        return Decision("failed", approved, target, reason=ancestry.failure())

    changed = changed_paths(ctx.git, approved, target)
    size = waiting_range(ctx.git, approved, target)
    if changed is None or size is None:
        return Decision("failed", approved, target, reason="cannot read the range")
    shipped = shipped_paths(ctx.git.show(approved, UPDATE_SCRIPT_REL))
    relevant = tuple(relevant_paths(changed, shipped))
    if not relevant:
        return Decision("promote", approved, target, REASON_INERT, (), *size)

    waiting = "changes what the Mac executes or ships"
    if shipped is None:
        waiting = f"cannot read the shipped lists from {UPDATE_SCRIPT_REL}"
    elif set(relevant) == BUMP_PATHS:
        problem = bump_problem_between(ctx.git, approved, target, only=False)
        problem = problem or recorded_bump_problem(ctx, target)
        if problem is None:
            return Decision("promote", approved, target, REASON_BUMP, relevant, *size)
        waiting = f"not a model bump this Mac tested: {problem}"
    return Decision("await", approved, target, waiting, relevant, *size)


def _result(
    result: str, exit_code: int, detail: str = "", counts: dict[str, int] | None = None
) -> common.StepResult:
    return common.checked_result(OUTCOMES, STEP, result, exit_code, detail, counts)


def auto_promote(ctx: common.Context) -> common.StepResult:
    """The scheduled step: advance the approved revision when the rule allows."""
    decision = evaluate(ctx)
    if decision.action == "unconfigured":
        return _result("no-approved-revision", common.EXIT_USAGE, decision.reason)
    if decision.action == "failed":
        return _result("git-failed", common.EXIT_FAILED, decision.reason)
    if decision.action == "up-to-date":
        return _result(RESULT_UP_TO_DATE, common.EXIT_OK)
    if decision.action == "diverged":
        return _result("diverged", common.EXIT_NEEDS_OWNER, decision.reason)
    if decision.action == "promote" and decision.target is not None:
        # What is recorded is the commit that was just classified, and only
        # while the default branch still names it.
        if ctx.git.commit_of(common.MAIN_REF) != decision.target:
            return _result("git-failed", common.EXIT_FAILED, "the default branch moved")
        write_approved(ctx.paths, decision.target)
        moved = (
            f"{common.short_sha(decision.approved)} -> "
            f"{common.short_sha(decision.target)} ({decision.reason})"
        )
        ctx.log.line(f"promote: {moved}")
        return _result(RESULT_PROMOTED, common.EXIT_OK, moved)

    age_days = 0.0
    if decision.oldest is not None:
        age_days = max(ctx.now - decision.oldest, 0.0) / common.SECONDS_PER_DAY
    overdue = age_days > PROMOTION_OVERDUE_DAYS
    shown = ", ".join(decision.relevant[:RELEVANT_SHOWN])
    detail = (
        f"awaiting promotion: {decision.commits} changes, oldest "
        f"{age_days:.1f} days; {decision.reason}; {shown}"
    )
    ctx.log.line(f"promote: {detail}")
    return _result(
        "awaiting-promotion",
        common.EXIT_NEEDS_OWNER if overdue else common.EXIT_OK,
        detail,
        {"commits": decision.commits},
    )


def awaiting_summary(ctx: common.Context) -> tuple[str | None, str | None, int | None]:
    """(approved, main, changes between them) for the status report."""
    approved = read_approved(ctx.paths)
    head = ctx.git.commit_of(common.MAIN_REF)
    if approved is None or head is None:
        return approved, head, None
    size = waiting_range(ctx.git, approved, head)
    return approved, head, None if size is None else size[0]


# --------------------------------------------------------------------------
# The owner's command
# --------------------------------------------------------------------------


def _show_range(
    ctx: common.Context, decision: Decision, say: Callable[[str], None]
) -> None:
    """Print the range for review. Records are split at NUL, never at a line
    break a commit subject could carry, and every line passes ``say``."""
    approved, target = str(decision.approved), str(decision.target)
    span = f"{approved}..{target}"
    listed = ctx.git.text(
        ["log", "-z", "--format=%h%x09%s", "-n", str(COMMITS_SHOWN), span]
    )
    subjects = [record for record in (listed or "").split("\0") if record.strip()]
    total = ctx.git.text(["rev-list", "--count", span]) or "?"
    say(f"{total} commits in {common.short_sha(approved)}..{common.short_sha(target)}:")
    for record in subjects:
        say(f"  {record.strip()}")
        reference = PULL_REFERENCE_RE.search(record)
        if reference is not None:
            say(f"    {common.FORK_WEB_URL}/pull/{reference.group(1)}")
    if len(subjects) >= COMMITS_SHOWN:
        say(f"  (the first {COMMITS_SHOWN}; the rest are in the diff)")
    say("")
    stat = ctx.git.text(["diff", "--stat=100", approved, target]) or "(no diff stat)"
    lines = stat.split("\n")
    for line in lines[:STAT_LINES_SHOWN]:
        say(line)
    if len(lines) > STAT_LINES_SHOWN:
        say(f"  (+{len(lines) - STAT_LINES_SHOWN} more lines)")
    say("")
    say(f"Inspect: git -C {ctx.paths.clone} diff {approved} {target}")
    say("")
    if decision.action == "promote":
        say(f"The next scheduled run would promote this by itself ({decision.reason}).")
        return
    say(f"Not promoted automatically: {decision.reason}")
    for path in decision.relevant:
        say(f"  executed or shipped: {path}")


def manual_promote(
    ctx: common.Context, ask: Callable[[str], str], out: Callable[[str], None]
) -> int:
    """The owner's command: show the range, ask for a typed word, advance."""

    def say(text: str) -> None:
        out(common.safe_text(text))

    decision = evaluate(ctx)
    if decision.action in ("unconfigured", "failed"):
        say(f"promote: {decision.reason}")
        unconfigured = decision.action == "unconfigured"
        return common.EXIT_USAGE if unconfigured else common.EXIT_FAILED
    if decision.action == "up-to-date" or decision.target is None:
        say(
            f"promote: {common.short_sha(decision.approved)} is already the default branch"
        )
        return common.EXIT_OK
    if decision.action == "diverged":
        say(f"WARNING: {decision.reason}.")
        say("The history of the default branch was rewritten, or the approved")
        say("revision was never on it. No range can be shown.")
        listed = ctx.git.text(
            [
                "log",
                "-z",
                "--format=%h%x09%s",
                "-n",
                str(DIVERGED_SHOWN),
                decision.target,
            ]
        )
        for record in (listed or "").split("\0"):
            say(f"  {record.strip()}")
    else:
        _show_range(ctx, decision, say)
    say("")
    answer = ask(
        f"Type {CONFIRM_WORD} to let this Mac execute and distribute "
        f"{decision.target}: "
    )
    if answer.strip() != CONFIRM_WORD:
        say("promote: not confirmed; the approved revision is unchanged")
        return common.EXIT_OK
    if ctx.git.commit_of(common.MAIN_REF) != decision.target:
        say("promote: the default branch moved while you were reading; run it again")
        return common.EXIT_FAILED
    write_approved(ctx.paths, decision.target)
    ctx.log.line(
        f"promote: {common.short_sha(decision.approved)} -> "
        f"{common.short_sha(decision.target)} (owner)"
    )
    say(f"promote: approved revision is now {decision.target}")
    say("The next scheduled run executes it; `launchd.sh run-now` runs one now.")
    return common.EXIT_OK
