#!/usr/bin/env python3
"""What a model bump is, and which bumps this Mac vouched for.

Two questions, asked by both the promotion rule and the model watch, so they
are answered in one place:

* **Is this change exactly one model bump?** :func:`bump_problem_between`
  compares two commits: only the manifest and the Codex configuration file
  differ, both are regular files, the manifest moves ``codex.model`` to a
  strictly newer slug of the same family and records the old one, and the
  configuration file changes the value of its one top-level ``model`` line.
* **Did this Mac build and test it?** A record in
  ``state/bump-records.json`` is written when the model watch has built the
  bump itself, checked its shape and made a live call. It names the slug, the
  commit, the two file objects and, once opened, the pull request. A record
  is used once, and only for the merge of that pull request from that commit:
  it is dropped when the bump is promoted and when the pull request is closed
  without merging.

Nothing here executes anything from the commits it reads. Files are read with
``git show`` and ``git ls-tree`` as data.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import json
import re
from typing import Any

import common

MANIFEST_REL = ".claude/skills/_shared/models.json"
CODEX_CONFIG_REL = ".codex/config.toml"
MODEL_KEY = "codex.model"
EFFORT_KEY = "codex.reasoning_effort"
MODEL_SECTION, MODEL_LEAF = MODEL_KEY.split(".")
EFFORT_LEAF = EFFORT_KEY.split(".")[1]
SUPERSEDED = "superseded"
BUMP_RELS = (MANIFEST_REL, CODEX_CONFIG_REL)
BUMP_PATHS = frozenset(BUMP_RELS)
REGULAR_ENTRY = ("100644", "blob")
# The only kinds of tree entry a checkout always can write and that carry no
# meaning of their own: a file, and an executable file. A link (120000) can
# point anywhere or be impossible to create; a gitlink (160000) is another
# repository.
PLAIN_MODES = ("100644", "100755")
DELETED_MODE = "000000"
# The longest name, and the longest path, the Mac's file system accepts, in
# bytes of UTF-8. A tree that adds a longer one cannot be checked out there.
# A name may use all of NAME_MAX (255). PATH_MAX (1024) counts the
# terminating NUL, so the longest path that works is one byte shorter.
NAME_MAX_BYTES = 255
PATH_MAX_BYTES = 1024 - 1
# How much of such a path is shown: enough to find it, never the whole of it.
LONG_PATH_SHOWN = 80
RAW_FIELDS = 5
RECORDS_KEPT = 20
TOML_TABLE_RE = re.compile(r"^[ \t]*\[")
TOML_MULTILINE = ('"""', "'''")
TOML_MODEL_RE = re.compile(r'(model[ \t]*=[ \t]*")([^"\\\r\n]*)("[ \t]*(?:#[^\r\n]*)?)')


# --------------------------------------------------------------------------
# The shape of one model bump
# --------------------------------------------------------------------------


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def parse_manifest(text: str) -> dict[str, Any] | None:
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
    document = parse_manifest(text or "")
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
    old_doc = parse_manifest(old_text)
    new_doc = parse_manifest(new_text)
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


def _added_or_changed(
    git: common.Git, base: str, head: str
) -> list[tuple[str, str]] | None:
    """(new mode, path) of every entry ``head`` adds or changes relative to
    ``base``; None when git cannot list them. Deletions are not listed."""
    result = git.run(["diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, head])
    if not result.ok:
        return None
    tokens = result.stdout.split("\0")
    found: list[tuple[str, str]] = []
    for index in range(0, len(tokens) - 1, 2):
        fields = tokens[index].split()
        if not tokens[index].startswith(":") or len(fields) != RAW_FIELDS:
            return None
        if fields[1] != DELETED_MODE:
            found.append((fields[1], tokens[index + 1]))
    return found


def special_entries(git: common.Git, base: str, head: str) -> list[str] | None:
    """Entries ``head`` adds or changes that are not plain files, as
    ``path (mode NNNNNN)``; None when git cannot list them.

    A link or a gitlink is never promoted without the owner: it can make the
    tree impossible to check out, or make a path mean something its name does
    not say. Deletions are not listed.
    """
    entries = _added_or_changed(git, base, head)
    if entries is None:
        return None
    return sorted(
        f"{path} (mode {mode})" for mode, path in entries if mode not in PLAIN_MODES
    )


def path_length_problem(path: str) -> str | None:
    """Why the Mac's file system cannot hold ``path``, or None.

    Lengths are counted in bytes of UTF-8. A name git reported in another
    encoding was decoded with replacement characters, each of which counts
    as three bytes: the count can only come out too high, never too low.
    """
    longest = max(len(name.encode("utf-8")) for name in path.split("/"))
    if longest > NAME_MAX_BYTES:
        return f"a name of {longest} bytes, over {NAME_MAX_BYTES}"
    total = len(path.encode("utf-8"))
    if total > PATH_MAX_BYTES:
        return f"{total} bytes, over {PATH_MAX_BYTES}"
    return None


def overlong_entries(git: common.Git, base: str, head: str) -> list[str] | None:
    """Paths ``head`` adds or changes that the Mac's file system cannot hold,
    as ``path... (why)`` with the path made safe to show and cut short; None
    when git cannot list them.

    Such a tree cannot be checked out, which would leave the Mac running an
    older revision than the one recorded as approved. Deletions are not
    listed.
    """
    entries = _added_or_changed(git, base, head)
    if entries is None:
        return None
    found: list[str] = []
    for _, path in entries:
        problem = path_length_problem(path)
        if problem is not None:
            found.append(f"{common.safe_text(path, LONG_PATH_SHOWN)} ({problem})")
    return sorted(found)


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


def _write_records(paths: common.Paths, records: list[dict[str, Any]]) -> None:
    kept = records[-RECORDS_KEPT:]
    common.write_text_atomic(paths.bump_records_file, json.dumps(kept, indent=2))


def blobs_of(git: common.Git, commit: str) -> dict[str, str] | None:
    """The object IDs of the two model files at a commit, when both are
    regular files; else None."""
    blobs: dict[str, str] = {}
    for rel in BUMP_RELS:
        entry = tree_entry(git, commit, rel)
        if entry is None or entry[:2] != REGULAR_ENTRY:
            return None
        blobs[rel] = entry[2]
    return blobs


def record_bump(ctx: common.Context, slug: str, commit: str) -> bool:
    """Remember that this Mac built the bump to ``slug`` as ``commit`` and made
    a live call with it. The record names no pull request yet, and without
    one it promotes nothing. False when the commit's files cannot be read."""
    blobs = blobs_of(ctx.git, commit)
    if blobs is None:
        return False
    record = {
        "slug": slug,
        "commit": commit,
        "blobs": blobs,
        "pull": None,
        "recorded": common.utc_stamp(ctx.now),
    }
    others = [item for item in _read_records(ctx.paths) if item.get("slug") != slug]
    _write_records(ctx.paths, [*others, record])
    return True


def attach_pull(paths: common.Paths, slug: str, commit: str, number: int) -> None:
    """Tie the record for ``slug`` to the pull request opened from ``commit``."""
    records = [
        {**item, "pull": number}
        if item.get("slug") == slug and item.get("commit") == commit
        else item
        for item in _read_records(paths)
    ]
    _write_records(paths, records)


def adopt_pull(paths: common.Paths, slug: str, commit: str, number: int) -> bool:
    """Tie pull request ``number`` to the record for ``slug`` only when that
    record was made for ``commit`` and names no pull request yet: a run that
    stopped between opening the pull request and recording its number. True
    when the record was changed."""
    waiting = [
        item
        for item in _read_records(paths)
        if item.get("slug") == slug
        and item.get("commit") == commit
        and item.get("pull") is None
    ]
    if not waiting:
        return False
    attach_pull(paths, slug, commit, number)
    return True


def drop_record(paths: common.Paths, slug: str | None) -> None:
    """Forget the record for ``slug``: it was used, or its pull request was
    closed. A record never outlives the one merge it was made for."""
    records = _read_records(paths)
    kept = [item for item in records if item.get("slug") != slug]
    if len(kept) != len(records):
        _write_records(paths, kept)


def recorded_bump_problem(
    ctx: common.Context, approved: str, target: str
) -> str | None:
    """None when the bump between ``approved`` and ``target`` is the merge of
    the pull request this Mac opened for it, from the commit it tested, with
    the two files it tested; else why not.

    The files alone are not enough: the same two files can be merged by other
    means after the owner closed that pull request. So GitHub is asked what
    became of it, and its merge commit must lie inside this range.
    """
    slug = manifest_model(ctx.git.show(target, MANIFEST_REL))
    records = [item for item in _read_records(ctx.paths) if item.get("slug") == slug]
    if slug is None or not records:
        return "no model watch on this Mac tested this bump"
    record = records[-1]
    if blobs_of(ctx.git, target) != record.get("blobs"):
        return "the two files are not the ones the model watch tested"
    number = record.get("pull")
    if not isinstance(number, int):
        return "the tested bump has no pull request on record"
    pull = ctx.github.pull(number)
    if pull is None:
        return f"cannot ask GitHub about pull request {number}"
    state = str(pull.get("state", "")).upper()
    if state == "CLOSED":
        drop_record(ctx.paths, slug)
    if state != "MERGED":
        return f"pull request {number} was not merged"
    if pull.get("headRefOid") != record.get("commit"):
        return f"pull request {number} was merged from another commit"
    merge = pull.get("mergeCommit")
    merged = merge.get("oid") if isinstance(merge, dict) else None
    if not isinstance(merged, str) or not common.SHA_RE.fullmatch(merged):
        return f"pull request {number} names no merge commit"
    inside = ctx.git.run(["merge-base", "--is-ancestor", merged, target]).ok
    before = ctx.git.run(["merge-base", "--is-ancestor", merged, approved]).ok
    if not inside or before:
        return f"the merge of pull request {number} is not in this range"
    return None
