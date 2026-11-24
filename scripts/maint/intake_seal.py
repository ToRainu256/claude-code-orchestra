#!/usr/bin/env python3
"""Commit a merge of upstream that someone finished by hand.

``intake --resolve-in DIR`` leaves a merge it could not finish: conflicts, or
a clean result that breaks an invariant. ``seal`` turns the finished directory
into the same two-parent commit a clean intake makes, under the same checks.

It believes nothing the directory says about itself. The automatic merge is
built again from the two commits (it is deterministic), so:

* the conflicted paths are the ones the merge has, not the ones a file in the
  directory lists. Each must be acknowledged by name in ``--resolutions``,
  with a note; a marker scan alone would pass a file deleted on one side, or
  a binary one, because neither leaves a marker;
* ``edited_by_hand`` is every path that differs from the automatic result,
  conflicted or not. Edits outside the conflicts are allowed, because fixing
  an invariant needs them (the codemod, the manifest rendered again, a seed's
  new pin); they are never unreported;
* ``sensitive_changes`` names what the merge changes among the paths a later
  session executes or loads, whoever changed them.

The record ``intake`` leaves in the directory says only which two commits it
was prepared for, so that a directory for another pair is refused early.

To check a pull request from outside, the owner runs ``intake --resolve-in``
on the same two commits and compares:
``git -C DIR diff auto-merge <pull request commit>``.

Run through ``fork_invariants.py seal``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import fork_invariants as invariants
import upstream_intake as intake
from scratch_repo import Repo, Scratch, Stop, inert_path, inert_text, refuse

# The start of a line merge-tree writes around a conflict, as a pattern for
# `git grep -E`: either side's marker, the base marker, or the separator.
MARKER_PATTERN = r"^(<{7} |>{7} |\|{7} |={7}$)"


def require_record(directory: Path, sides: intake.Sides) -> None:
    """Refuse a directory that ``intake`` did not prepare for these commits."""
    try:
        text = (directory / ".git" / intake.RECORD_FILE).read_text("utf-8")
        record = json.loads(text)
    except (OSError, ValueError):
        refuse("--resolve-in is not a directory that intake --resolve-in prepared")
    if not isinstance(record, dict) or record.get("version") != intake.RECORD_VERSION:
        refuse("the intake record in --resolve-in is damaged")
    if record.get("fork") != sides.fork or record.get("upstream") != sides.upstream:
        refuse("--resolve-in was prepared for another fork or upstream commit")


def load_resolutions(path: Path) -> dict[str, str]:
    """The notes that acknowledge each conflict: path -> what was decided."""
    try:
        notes = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        refuse("--resolutions must be a JSON object: path -> one-line note")
    if not isinstance(notes, dict) or not all(
        isinstance(note, str) and note.strip() for note in notes.values()
    ):
        refuse("--resolutions must map every path to a note that is not empty")
    return notes


def require_answers(conflicts: tuple[str, ...], notes: dict[str, str]) -> None:
    """Every conflict the merge has is answered by name, and nothing else is
    passed off as one."""
    unanswered = sorted(set(conflicts) - set(notes))
    unknown = sorted(set(notes) - set(conflicts))
    if unanswered or unknown:
        intake.needs_owner(
            invariants.RESULT_UNRESOLVED,
            "--resolutions must name exactly the conflicted paths",
            unanswered=[inert_path(path) for path in unanswered],
            not_conflicts=[inert_path(path) for path in unknown],
        )


def finished_tree(edited: Repo, conflicts: tuple[str, ...]) -> str:
    """Stage the directory as it stands and return its tree. A conflicted
    path that still holds a marker line stops the run."""
    edited.run("add", "--all")
    tree = edited.text("write-tree")
    if not conflicts:
        return tree
    found = edited.run(
        "--literal-pathspecs", "grep", "-z", "-l", "-I", "-E", "-e", MARKER_PATTERN,
        tree, "--", *conflicts, ok=(0, 1),
    )  # fmt: skip
    marked = [entry.split(":", 1)[1] for entry in found.stdout.split("\0") if entry]
    if marked:
        still = [inert_path(path) for path in marked]
        intake.needs_owner(
            invariants.RESULT_UNRESOLVED, "conflict markers remain", unanswered=still
        )
    return tree


def seal(
    sides: intake.Sides, notes: dict[str, str], resolve_in: Path, work: Path
) -> dict[str, object]:
    known = intake.preflight(sides)
    base = str(known["base"])
    try:
        edited = Scratch(Repo(resolve_in, sides.project.env))
        tools = invariants.extract_tools(sides.project, sides.fork, work / "tools")
        # Borrows the directory's objects, which borrow the fork's: the tree
        # being sealed and the automatic one end up comparable in one place.
        scratch = Scratch.create(work / "tree", edited.repo)
        automatic, found, _ = intake.merge_transformed(scratch, tools, sides, base)
        conflicts = tuple(conflict.path for conflict in found)
        require_answers(conflicts, notes)
        # `git add` leaves ignored files out. They are named, so a resolution
        # written to an ignored path is not lost without a word.
        ignored = edited.repo.listed(
            "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
        )
        tree = finished_tree(edited.repo, conflicts)
        findings = intake.judged(scratch, tools, sides, tree, None)
        if findings:
            detail = f"{len(findings)} finding(s) remain"
            listed = invariants.as_json(findings)
            intake.needs_owner(invariants.RESULT_INVARIANTS, detail, findings=listed)
        by_hand = intake.changed_paths(scratch.repo, automatic, tree)
        message = intake.commit_message(sides, base, by_hand=len(by_hand))
        pins_after = invariants.seed_pins(
            edited.repo.text_at(tree, invariants.SEED_PIN_FILE)
        )
        return {
            **known,
            **intake.publish(sides, edited, tree, message),
            "ignored_files_left_out": [inert_path(path) for path in ignored],
            "edited_by_hand": [inert_path(path) for path in by_hand],
            "resolutions": {
                inert_path(path): inert_text(notes[path]) for path in conflicts
            },
            "seed_pins_changed": sides.pins() != pins_after,
        }
    except Stop as stop:
        raise intake.with_facts(stop, known) from None


def cmd_seal(args: argparse.Namespace, work: Path) -> dict[str, object]:
    sides = intake.Sides.of(args)
    require_record(args.resolve_in, sides)
    notes = load_resolutions(args.resolutions)
    try:
        return {**sides.facts(), **seal(sides, notes, args.resolve_in, work)}
    except Stop as stop:
        raise intake.with_facts(stop, sides.facts()) from None
