#!/usr/bin/env python3
"""Prepare the merge that takes upstream's branch into the fork.

A plain merge of upstream conflicts wherever one of the fork's mechanical
changes touched a line upstream also changed, which is almost every file.
This module removes that noise before git sees it, and nothing else. With
``T`` the mechanical changes (the role rename by the fork's own codemod, the
seed files moved to ``scripts/scaffold/``, the default install lines pointed
at the fork):

1. ``T`` is applied to the merge base and to upstream's tip, each checked out
   in a scratch repository;
2. ``git merge-tree --write-tree --merge-base=T(base) <fork> T(upstream)``
   merges them. What the fork changed besides ``T``, and what upstream changed
   since the base, are the only two sides left;
3. a clean result that still satisfies the fork's invariants becomes one
   commit with the parents ``[fork, upstream]`` on the local branch
   ``bot/upstream-<sha>``. The second parent is the real upstream tip, so the
   next intake finds it as the merge base and starts from there.

Why later intakes stay correct: the fork's own tree must satisfy the
invariants before anything is merged, so ``T`` changes nothing in it. Every
difference between ``T(base)`` and the fork is therefore kept as the fork's
side, whatever made it, and can only meet upstream's side as a conflict,
never be dropped. A changed ``T`` costs conflicts, not content.

A conflict that survives ``T`` is a real one: it is reported, with what git
said about it, and never resolved here. ``--resolve-in DIR`` leaves the
automatic result in ``DIR`` (markers included, on top of the fork's commit)
for a person or an agent to finish; ``seal`` then builds the same two-parent
commit from that directory, once every reported path is acknowledged by name
with a note and the result passes the same checks as a clean merge. A marker
scan would not be enough: a file deleted on one side, or a binary one,
conflicts without a marker.

What an intake may not change without the owner, compared with the fork
object by object in the final tree: anything under ``.github/`` (pushing a
changed workflow would run it), the model manifest, and the fork's own state
files. Seeds must match the digests the fork pins; ``seal`` accepts a seed
together with its new pin and says so in its result.

Nothing is pushed. The fork's repository changes in one way, at the end: the
finished commit is fetched from the scratch repository into the new branch.

Run through ``fork_invariants.py intake`` and ``fork_invariants.py seal``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import common
import fork_invariants as invariants
import scratch_repo
from scratch_repo import Repo, Scratch, Stop, failed, refuse, shown

MANIFEST = ".claude/skills/_shared/models.json"
WORKFLOWS = ".github"
# Files that must leave an intake exactly as the fork has them: the model
# manifest, and what the fork keeps where upstream keeps its seeds.
FORK_OWNED = (MANIFEST, *invariants.SEEDS)

BRANCH_PREFIX = "bot/upstream-"
STATE_FILE = "orchestra-intake.json"
STATE_VERSION = 1
STATE_SHAS = ("fork", "upstream", "base", "auto_tree")
ZERO_EPOCH = "0"
# Branch names in the scratch repository. merge-tree writes the two it is
# given into the conflict markers.
OURS = "fork-main"
THEIRS = "upstream-main"
BASE = "merge-base"
AUTO = "auto-merge"
SEALED = "intake"
CONFLICT_PREFIX = "CONFLICT"
# The start of a marker line merge-tree writes, as a pattern for `git grep -E`.
MARKER_PATTERN = r"^(<{7}|>{7}|\|{7}) "


@dataclass(frozen=True)
class Conflict:
    """One path git could not merge, and the kinds of conflict it named."""

    path: str
    types: tuple[str, ...]


# =============================================================================
# the mechanical changes (T)
# =============================================================================


def rename_roles(target: invariants.Target) -> int:
    """The fork's role rename, by the fork's own codemod."""
    result = invariants.run_tool(
        target, invariants.RENAME_TOOL, "--project-root", str(target.tree), "--apply"
    )
    reported = " | ".join(invariants.said(result))
    if result.returncode == common.EXIT_NEEDS_OWNER:
        detail = f"{invariants.RENAME_TOOL} refused: {reported}"
        raise Stop(invariants.RESULT_TRANSFORM, common.EXIT_NEEDS_OWNER, detail)
    if result.returncode != 0:
        detail = f"{invariants.RENAME_TOOL} exited {result.returncode}: {reported}"
        failed(scratch_repo.RESULT_TOOL, detail)
    try:
        payload = json.loads(result.stdout)
        return len(payload["files"]) + len(payload["moves"])
    except (ValueError, KeyError, TypeError):
        failed(scratch_repo.RESULT_TOOL, f"{invariants.RENAME_TOOL} printed no result")


def relocate_seeds(scratch: Scratch) -> int:
    """Move upstream's seed files to where the fork keeps them. A tree that
    has both layouts at once is not one the fork has a rule for."""
    moved = 0
    root = scratch.repo.path
    for live, seed in invariants.SEEDS.items():
        if not os.path.lexists(root / live):
            continue
        if os.path.lexists(root / seed) or invariants.plain_file(root, live) is None:
            detail = f"{live} and {seed}: a layout the fork has no rule for"
            raise Stop(invariants.RESULT_TRANSFORM, common.EXIT_NEEDS_OWNER, detail)
        (root / seed).parent.mkdir(parents=True, exist_ok=True)
        scratch.repo.run("--literal-pathspecs", "mv", "--", live, seed)
        moved += 1
    return moved


def point_installs_at_fork(root: Path) -> int:
    """Rewrite upstream's URL to the fork's, on the default install lines
    only. Every other mention of upstream is attribution and stays."""
    changed = 0
    for rel, is_install in invariants.INSTALL_LINES:
        text = invariants.read_text(root, rel)
        if text is None:
            continue
        lines = text.split("\n")
        rewritten = [
            line.replace(invariants.UPSTREAM_GIT_URL, common.FORK_GIT_URL)
            if is_install(line)
            else line
            for line in lines
        ]
        if rewritten != lines:
            changed += sum(1 for at, line in enumerate(lines) if rewritten[at] != line)
            (root / rel).write_bytes("\n".join(rewritten).encode("utf-8"))
    return changed


def transform(scratch: Scratch, tools: Path, tree: str) -> tuple[str, dict[str, int]]:
    """``T(tree)``: the same three steps for the merge base and for upstream's
    tip, so what differs between the results is what upstream changed."""
    scratch.load(tree)
    target = invariants.Target(tools, scratch.repo.path, scratch.repo.env)
    applied = {
        "roles": rename_roles(target),
        "seeds": relocate_seeds(scratch),
        "install_urls": point_installs_at_fork(scratch.repo.path),
    }
    return scratch.snapshot(), applied


# =============================================================================
# merge
# =============================================================================


def parse_merge(output: str) -> tuple[str, list[Conflict]]:
    """Read ``git merge-tree --write-tree -z``: the tree, the paths with
    unmerged stages, and the conflict events. A path counts as conflicted
    when it is in either list; a conflict need not leave a marker."""
    fields = output.split("\0")
    tree = fields[0]
    if common.SHA_RE.fullmatch(tree) is None:
        raise ValueError("no tree")
    index = 1
    types: dict[str, set[str]] = {}
    while index < len(fields) and fields[index]:
        types.setdefault(fields[index].split("\t", 1)[1], set())
        index += 1
    index += 1
    while index < len(fields) and fields[index]:
        count = int(fields[index])
        paths = fields[index + 1 : index + 1 + count]
        kind = fields[index + 1 + count]
        index += count + 3
        if kind.startswith(CONFLICT_PREFIX):
            for path in paths:
                types.setdefault(path, set()).add(kind)
    return tree, [Conflict(path, tuple(sorted(types[path]))) for path in sorted(types)]


def merge(scratch: Scratch, *args: str) -> tuple[str, list[Conflict]]:
    """Merge without touching an index or a work tree. Exit 1 is a conflict;
    anything else that is not 0 is a failure, and its output is not read."""
    result = scratch.repo.run("merge-tree", "--write-tree", "-z", *args, ok=(0, 1))
    try:
        tree, conflicts = parse_merge(result.stdout)
    except (ValueError, IndexError):
        failed(scratch_repo.RESULT_GIT, "git merge-tree printed what this cannot read")
    if (result.returncode == 1) != bool(conflicts):
        detail = "git merge-tree's exit code and its conflict list disagree"
        failed(scratch_repo.RESULT_GIT, detail)
    return tree, conflicts


@dataclass(frozen=True)
class Sides:
    """The two commits one run merges, in the repository that holds both."""

    project: Repo
    fork: str
    upstream: str
    allow_workflows: bool

    @staticmethod
    def of(args: argparse.Namespace) -> Sides:
        project = Repo(args.project_root, scratch_repo.child_env())
        scratch_repo.require_repository(project)
        fork = scratch_repo.commit_of(project, args.fork_rev, "--fork-rev")
        upstream = scratch_repo.commit_of(project, args.upstream_rev, "--upstream-rev")
        return Sides(project, fork, upstream, args.owner_allows_workflow_changes)

    def facts(self) -> dict[str, object]:
        return {"fork": self.fork, "upstream": self.upstream}

    def pins(self) -> dict[str, str] | None:
        """The seed digests the fork pins: the ones an intake is held to."""
        pin_file = self.project.text_at(self.fork, invariants.SEED_PIN_FILE)
        return invariants.seed_pins(pin_file)


def needs_owner(result: str, detail: str, **fields: object) -> NoReturn:
    raise Stop(result, common.EXIT_NEEDS_OWNER, detail, **fields)


def with_facts(stop: Stop, facts: dict[str, object]) -> Stop:
    """The same outcome, carrying what the run had established by then."""
    return Stop(stop.result, stop.exit_code, stop.detail, **{**facts, **stop.fields})


def protected_findings(repo: Repo, sides: Sides, tree: str) -> list[invariants.Finding]:
    """What an intake must not change, compared with the fork object by
    object: the workflows, the model manifest, the fork's own state."""
    found = []
    changed = repo.listed(
        "diff", "--name-only", "-z", "--no-renames", sides.fork, tree, "--", WORKFLOWS
    )
    if changed and not sides.allow_workflows:
        detail = f"changed under {WORKFLOWS}: {', '.join(changed)}"
        found.append(invariants.Finding("workflows-untouched", detail))
    for rel in FORK_OWNED:
        if repo.blob_at(sides.fork, rel) != repo.blob_at(tree, rel):
            detail = f"{rel} is no longer the fork's own file"
            found.append(invariants.Finding("fork-owned-untouched", detail))
    return found


def judged(
    scratch: Scratch,
    tools: Path,
    sides: Sides,
    tree: str,
    pins: dict[str, str] | None,
) -> list[invariants.Finding]:
    """Everything a merged tree must satisfy before it becomes a commit."""
    scratch.load(tree)
    target = invariants.Target(tools, scratch.repo.path, scratch.repo.env, pins)
    return invariants.check_tree(target) + protected_findings(scratch.repo, sides, tree)


def commit_message(sides: Sides, base: str, by_hand: int) -> str:
    """Only SHAs and counts: nothing upstream wrote reaches the message."""
    lines = [
        f"merge upstream/main {common.short_sha(sides.upstream)} into the fork",
        "",
        f"Upstream: {invariants.UPSTREAM_SLUG}@{sides.upstream}",
        f"Fork: {sides.fork}",
        f"Merge-base: {base}",
        "",
        "Prepared by scripts/maint/fork_invariants.py: the fork's mechanical",
        "changes (role rename, seed location, install URLs) were applied to the",
        "merge base and to upstream's tree before the two were merged.",
    ]
    if by_hand:
        lines += ["", f"Paths that differ from the automatic merge: {by_hand}"]
    return "\n".join(lines) + "\n"


def publish(
    sides: Sides, scratch: Scratch, tree: str, message: str
) -> dict[str, object]:
    """Build the two-parent commit and bring it into the fork's repository as
    a new local branch. Returns what the caller reports about it."""
    project, fork, upstream = sides.project, sides.fork, sides.upstream
    stamps = project.text("show", "-s", "--format=%ct", fork, upstream).split()
    commit = scratch.commit(tree, (fork, upstream), message, max(stamps, key=int))
    branch = f"{BRANCH_PREFIX}{common.short_sha(upstream)}"
    ref = f"refs/heads/{branch}"
    if project.has(ref) and project.text("rev-parse", ref) != commit:
        detail = f"{branch} already exists and is another commit"
        needs_owner(invariants.RESULT_BRANCH, detail)
    scratch.branch(SEALED, commit)
    project.run(
        "-c", "protocol.file.allow=always",
        "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
        str(scratch.repo.path), f"refs/heads/{SEALED}:{ref}",
    )  # fmt: skip
    # Read back from the repository that will be pushed from. The second
    # parent is what makes this a merge of upstream and not a copy of it.
    recorded = project.text("rev-list", "--parents", "-n", "1", ref).split()
    kept_tree = project.text("rev-parse", f"{ref}^{{tree}}")
    if recorded != [commit, fork, upstream] or kept_tree != tree:
        detail = f"{branch} is not the merge of the fork and upstream that was built"
        failed(scratch_repo.RESULT_GIT, detail)
    return {
        "result": invariants.RESULT_MERGED,
        "detail": f"{branch} is the merge of upstream {common.short_sha(upstream)}",
        "commit": commit,
        "branch": branch,
        "tree": tree,
    }


# =============================================================================
# intake
# =============================================================================


def preflight(sides: Sides) -> dict[str, object]:
    """What is decided before any tree is checked out: exactly one merge
    base, no workflow change, no entry that must not be checked out."""
    project, fork, upstream = sides.project, sides.fork, sides.upstream
    bases = project.run("merge-base", "--all", fork, upstream, ok=(0, 1)).stdout.split()
    if len(bases) != 1:
        detail = f"{len(bases)} merge bases; exactly one is needed"
        needs_owner(invariants.RESULT_NO_BASE, detail)
    base = bases[0]
    ahead = project.text("rev-list", "--count", f"{fork}..{upstream}")
    known: dict[str, object] = {"base": base, "upstream_commits": int(ahead)}
    workflows = project.listed(
        "diff", "--name-only", "-z", "--no-renames", base, upstream, "--", WORKFLOWS
    )
    if workflows and not sides.allow_workflows:
        paths = [shown(path) for path in workflows]
        detail = f"upstream changed files under {WORKFLOWS}"
        needs_owner(invariants.RESULT_WORKFLOWS, detail, **known, paths=paths)
    for name, tree in (("the merge base", base), ("upstream's tip", upstream)):
        entries = scratch_repo.unsupported_entries(project, tree)[
            : invariants.DETAIL_LINES
        ]
        if entries:
            detail = f"{name} holds entries this does not check out"
            needs_owner(invariants.RESULT_UNSUPPORTED, detail, **known, entries=entries)
    return known


def merge_transformed(
    scratch: Scratch, tools: Path, sides: Sides, base: str
) -> tuple[str, list[Conflict], dict[str, object]]:
    """Hold the fork to its invariants, apply T to the other two sides, and
    merge. Returns the tree, its conflicts, and what was measured on the way."""
    scratch.load(sides.fork)
    own = invariants.check_tree(
        invariants.Target(tools, scratch.repo.path, scratch.repo.env)
    )
    if own:
        detail = "the fork's own tree fails its invariants; fix that first"
        needs_owner(invariants.RESULT_FORK, detail, findings=invariants.as_json(own))
    base_tree, applied_to_base = transform(scratch, tools, base)
    upstream_tree, applied = transform(scratch, tools, sides.upstream)
    scratch.branch(OURS, sides.fork)
    for name, tree in ((BASE, base_tree), (THEIRS, upstream_tree)):
        scratch.branch(name, scratch.commit(tree, (), f"T({name})\n", ZERO_EPOCH))
    plain = merge(scratch, sides.fork, sides.upstream)[1]
    tree, conflicts = merge(scratch, f"--merge-base={BASE}", OURS, THEIRS)
    measured: dict[str, object] = {
        "transformed": {"merge_base": applied_to_base, "upstream": applied},
        "conflicts_without_transform": len(plain),
        "conflicts": [
            {"path": shown(c.path), "types": [shown(kind) for kind in c.types]}
            for c in conflicts
        ],
    }
    return tree, conflicts, measured


def leave_for_resolution(
    scratch: Scratch, sides: Sides, base: str, tree: str, conflicts: list[Conflict]
) -> None:
    """Leave the automatic result checked out on top of the fork's commit, so
    the difference to finish is the one a reviewer will see. A branch holds
    the result, so it outlives a garbage collection."""
    scratch.load(tree)
    scratch.repo.run("update-ref", "--no-deref", "HEAD", sides.fork)
    held = scratch.commit(tree, (sides.fork,), "automatic merge result\n", ZERO_EPOCH)
    scratch.branch(AUTO, held)
    state = {
        "version": STATE_VERSION,
        "fork": sides.fork,
        "upstream": sides.upstream,
        "base": base,
        "auto_tree": tree,
        "conflicts": [conflict.path for conflict in conflicts],
    }
    record = scratch.repo.path / ".git" / STATE_FILE
    record.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def unfinished(
    conflicts: list[Conflict],
    findings: list[invariants.Finding],
    known: dict[str, object],
) -> NoReturn:
    """A merge that needs judgment: say which kind, with everything known."""
    if conflicts:
        detail = f"{len(conflicts)} path(s) conflict after the mechanical changes"
        needs_owner(invariants.RESULT_CONFLICTS, detail, **known)
    detail = f"the merge is clean, but {len(findings)} finding(s) remain"
    listed = invariants.as_json(findings)
    needs_owner(invariants.RESULT_INVARIANTS, detail, **known, findings=listed)


def take_in(sides: Sides, resolve_in: Path | None, work: Path) -> dict[str, object]:
    known = preflight(sides)
    base = str(known["base"])
    tools = invariants.extract_tools(sides.project, sides.fork, work / "tools")
    keep = False
    try:
        scratch = Scratch.create(resolve_in or work / "tree", sides.project)
        tree, conflicts, measured = merge_transformed(scratch, tools, sides, base)
        known = {**known, **measured}
        # A conflicted tree is not judged: it is not a candidate for anything.
        findings = (
            [] if conflicts else judged(scratch, tools, sides, tree, sides.pins())
        )
        if conflicts or findings:
            keep = resolve_in is not None
            if keep:
                leave_for_resolution(scratch, sides, base, tree, conflicts)
                known = {**known, "resolve_in": shown(str(resolve_in))}
            unfinished(conflicts, findings, known)
        message = commit_message(sides, base, by_hand=0)
        return {**known, **publish(sides, scratch, tree, message)}
    finally:
        if resolve_in is not None and not keep:
            shutil.rmtree(resolve_in, ignore_errors=True)


def cmd_intake(args: argparse.Namespace, work: Path) -> dict[str, object]:
    sides = Sides.of(args)
    if args.resolve_in is not None and os.path.lexists(args.resolve_in):
        refuse("--resolve-in must name a directory that does not exist yet")
    contained = sides.project.run(
        "merge-base", "--is-ancestor", sides.upstream, sides.fork, ok=(0, 1)
    )
    if contained.ok:
        detail = "the fork already contains upstream's tip"
        return {
            **sides.facts(),
            "result": invariants.RESULT_UP_TO_DATE,
            "detail": detail,
        }
    try:
        return {**sides.facts(), **take_in(sides, args.resolve_in, work)}
    except Stop as stop:
        raise with_facts(stop, sides.facts()) from None


# =============================================================================
# seal
# =============================================================================


@dataclass(frozen=True)
class Record:
    """What ``intake --resolve-in`` wrote down about the merge it left."""

    base: str
    auto_tree: str
    conflicts: tuple[str, ...]


def load_record(directory: Path, sides: Sides) -> Record:
    """The record in ``directory``, held to the commits the caller names."""
    try:
        state = json.loads((directory / ".git" / STATE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        refuse("--resolve-in is not a directory that intake --resolve-in prepared")
    conflicts = state.get("conflicts") if isinstance(state, dict) else None
    well_formed = (
        isinstance(state, dict)
        and state.get("version") == STATE_VERSION
        and all(
            isinstance(state.get(key), str) and common.SHA_RE.fullmatch(state[key])
            for key in STATE_SHAS
        )
        and isinstance(conflicts, list)
        and all(isinstance(path, str) for path in conflicts)
    )
    if not well_formed:
        refuse("the intake record in --resolve-in is damaged")
    if state["fork"] != sides.fork or state["upstream"] != sides.upstream:
        refuse("--resolve-in was prepared for another fork or upstream commit")
    return Record(state["base"], state["auto_tree"], tuple(conflicts))


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
    """Every conflict is answered by name. A marker scan alone would pass a
    file deleted on one side, or a binary one: neither leaves a marker."""
    unanswered = sorted(set(conflicts) - set(notes))
    unknown = sorted(set(notes) - set(conflicts))
    if unanswered or unknown:
        needs_owner(
            invariants.RESULT_UNRESOLVED,
            "--resolutions must name exactly the conflicted paths",
            unanswered=[shown(path) for path in unanswered],
            not_conflicts=[shown(path) for path in unknown],
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
        still = [shown(path) for path in marked]
        needs_owner(
            invariants.RESULT_UNRESOLVED, "conflict markers remain", unanswered=still
        )
    return tree


def seal(
    sides: Sides, record: Record, notes: dict[str, str], resolve_in: Path, work: Path
) -> dict[str, object]:
    require_answers(record.conflicts, notes)
    edited = Scratch(Repo(resolve_in, sides.project.env))
    # `git add` leaves ignored files out. They are named, so a resolution
    # written to an ignored path is not lost without a word.
    ignored = edited.repo.listed(
        "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    )
    tree = finished_tree(edited.repo, record.conflicts)
    tools = invariants.extract_tools(sides.project, sides.fork, work / "tools")
    scratch = Scratch.create(work / "tree", edited.repo)
    findings = judged(scratch, tools, sides, tree, None)
    if findings:
        detail = f"{len(findings)} finding(s) remain"
        listed = invariants.as_json(findings)
        needs_owner(invariants.RESULT_INVARIANTS, detail, findings=listed)
    by_hand = edited.repo.listed(
        "diff", "--name-only", "-z", "--no-renames", record.auto_tree, tree
    )
    message = commit_message(sides, record.base, by_hand=len(by_hand))
    pins_after = invariants.seed_pins(
        edited.repo.text_at(tree, invariants.SEED_PIN_FILE)
    )
    return {
        **publish(sides, edited, tree, message),
        "ignored_files_left_out": [shown(path) for path in ignored],
        "edited_by_hand": [shown(path) for path in by_hand],
        "resolutions": {shown(path): shown(notes[path]) for path in record.conflicts},
        "seed_pins_changed": sides.pins() != pins_after,
    }


def cmd_seal(args: argparse.Namespace, work: Path) -> dict[str, object]:
    sides = Sides.of(args)
    record = load_record(args.resolve_in, sides)
    notes = load_resolutions(args.resolutions)
    facts = {**sides.facts(), "base": record.base}
    try:
        return {**facts, **seal(sides, record, notes, args.resolve_in, work)}
    except Stop as stop:
        raise with_facts(stop, facts) from None
