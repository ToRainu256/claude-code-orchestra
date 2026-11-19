"""Behavioral tests for the fork invariants and the upstream-intake merge.

Every merge case runs on real repositories under ``tmp_path``: an "upstream"
that still speaks upstream's vocabulary, and a "fork" made from it the way the
real fork was made: the repository's own role codemod applied, the seed files
copied to ``scripts/scaffold/`` with their digests pinned, the default install
lines pointed at the fork, and the fork's tools committed. Upstream then moves
on, the fork fetches it, and ``fork_invariants.py intake`` is run from the
fork's checkout, as the routine runs it.

Nothing reaches the network or the developer's checkout. The model values in
the fixture are placeholders, not the template's own: a bump of the real
manifest must not change what these tests prove. The old role names are read
from the codemod, never written here, so this file stays clear of the rename
and of the lint that enforces it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAINT_DIR = REPO_ROOT / "scripts" / "maint"
ROUTINES_DIR = MAINT_DIR / "routines"
if str(MAINT_DIR) not in sys.path:
    sys.path.insert(0, str(MAINT_DIR))

import common  # noqa: E402
import fork_invariants  # noqa: E402
import scratch_repo  # noqa: E402
import upstream_intake  # noqa: E402


def _load_codemod() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rename_roles_for_intake", REPO_ROOT / fork_invariants.RENAME_TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolve their own module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


(OLD_STANDARD, NEW_STANDARD), (OLD_DEEP, NEW_DEEP) = _load_codemod().RENAMES.items()

AGENTS = ".claude/agents"
ROUTING = ".claude/rules/routing.md"
GUIDE = "docs/guide.md"
LIVE_STATE = ".claude/STATE.md"
SEED_STATE = "scripts/scaffold/STATE.md"
FORK_GUIDE_LINE = "line two, as the fork words it"
# A word the model-name lint refuses anywhere outside the manifest.
MODEL_WORD = "haiku"
MANIFEST = {
    "schema_version": 1,
    "claude": {
        "main": "inherit",
        "subagent_default": "inherit",
        "agents": {NEW_STANDARD: "inherit", NEW_DEEP: "inherit"},
    },
    "codex": {"model": "fixture-model", "reasoning_effort": "high"},
    "superseded": {
        "claude.main": [],
        "claude.subagent_default": [],
        "codex.model": [],
        "codex.reasoning_effort": [],
    },
}
UPSTREAM_FILES = {
    ".gitignore": "__pycache__/\n",
    f"{AGENTS}/{OLD_STANDARD}.md": (
        f"---\nname: {OLD_STANDARD}\ndescription: routine work\n"
        "model: inherit\n---\n\nImplement.\n"
    ),
    f"{AGENTS}/{OLD_DEEP}.md": (
        f"---\nname: {OLD_DEEP}\ndescription: research\nmodel: inherit\n---\n\nAnalyse.\n"
    ),
    ROUTING: (
        f"# Routing\n\nUse `{OLD_STANDARD}` for routine work.\n"
        f"Use `{OLD_DEEP}` for research.\n\nKeep reports short.\n"
    ),
    LIVE_STATE: "# Agent State\n\nseed state\n",
    ".claude/docs/DESIGN.md": "# Design\n\nseed design\n",
    ".claude/settings.json": (
        '{\n  "env": {\n    "CLAUDE_CODE_SUBAGENT_MODEL": "inherit"\n  },\n'
        '  "model": "inherit"\n}\n'
    ),
    ".codex/config.toml": (
        'sandbox_mode = "read-only"\nmodel = "fixture-model"\n'
        'model_reasoning_effort = "high"\n'
    ),
    "scripts/update.sh": (
        "#!/usr/bin/env bash\n"
        f'TEMPLATE_REPO="${{ORCHESTRA_TEMPLATE_REPO:-{fork_invariants.UPSTREAM_GIT_URL}}}"\n'
        'echo "$TEMPLATE_REPO"\n'
    ),
    "README.md": (
        "# Template\n\n"
        f"Forked from https://github.com/{fork_invariants.UPSTREAM_SLUG}.\n\n"
        f"    git clone --depth 1 {fork_invariants.UPSTREAM_GIT_URL} template\n\n"
        "The other edition:\n\n"
        f"    git clone --depth 1 --branch other {fork_invariants.UPSTREAM_GIT_URL} t\n"
    ),
    GUIDE: "# Guide\n\nline one\nline two\nline three\n\nend\n",
    "docs/keep.md": "kept\n",
}
EXIT_OK, EXIT_USAGE, EXIT_NEEDS_OWNER = 0, 1, 2
SYSTEM_PYTHON = Path("/usr/bin/python3")


def interpreters() -> list[str]:
    """The test interpreter, plus the system one when it is another version:
    the helper must stay runnable on the macOS system Python 3.9."""
    found = [sys.executable]
    if SYSTEM_PYTHON.exists():
        probe = subprocess.run(
            [str(SYSTEM_PYTHON), "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode == 0 and probe.stdout.strip() != str(sys.version_info[:2]):
            found.append(str(SYSTEM_PYTHON))
    return found


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pin_file(state: str, design: str) -> str:
    """A stand-in for tests/test_scaffold.py: the pins in the same form."""
    return (
        "SEED_SHA256 = {\n"
        f'    SEED_STATE: "{sha256(state)}",\n'
        f'    SEED_DESIGN: "{sha256(design)}",\n'
        "}\n"
    )


def make_fork(fork: Path) -> None:
    """Turn a clone of upstream into the fork, with the fork's own tools."""
    for rel in fork_invariants.FORK_TOOLS:
        target = fork / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, target)
    write(fork, upstream_intake.MANIFEST, json.dumps(MANIFEST, indent=2) + "\n")
    codemod = [sys.executable, "-B", str(fork / fork_invariants.RENAME_TOOL)]
    subprocess.run(
        [*codemod, "--project-root", str(fork), "--apply"],
        check=True,
        capture_output=True,
    )
    for live, seed in fork_invariants.SEEDS.items():
        write(fork, seed, UPSTREAM_FILES[live])
    write(fork, LIVE_STATE, "# Agent State\n\nthe fork's own state\n")
    write(
        fork,
        fork_invariants.SEED_PIN_FILE,
        pin_file(UPSTREAM_FILES[LIVE_STATE], UPSTREAM_FILES[".claude/docs/DESIGN.md"]),
    )
    for rel in (fork_invariants.UPDATER, fork_invariants.README):
        text = (fork / rel).read_text(encoding="utf-8")
        lines = [
            line.replace(fork_invariants.UPSTREAM_GIT_URL, common.FORK_GIT_URL)
            if "--branch" not in line
            else line
            for line in text.split("\n")
        ]
        write(fork, rel, "\n".join(lines))
    guide = UPSTREAM_FILES[GUIDE].replace("line two", FORK_GUIDE_LINE)
    write(fork, GUIDE, guide)
    write(fork, ".github/workflows/ci.yml", "name: CI\n")
    write(fork, "docs/fork-only.md", "only the fork has this\n")


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Upstream with one commit, and the fork made from it. Built once."""
    root = tmp_path_factory.mktemp("intake-template")
    upstream = root / "upstream"
    upstream.mkdir()
    git(upstream, "init", "--quiet", "--initial-branch=main")
    for rel, text in UPSTREAM_FILES.items():
        write(upstream, rel, text)
    commit_all(upstream, "upstream: base")
    fork = root / "fork"
    git(root, "clone", "--quiet", str(upstream), str(fork))
    make_fork(fork)
    commit_all(fork, "fork: rename roles, scaffold, tools")
    return root


@dataclass
class World:
    """One test's upstream and fork, and the helper run from the fork."""

    fork: Path
    upstream: Path
    capsys: pytest.CaptureFixture[str]

    @property
    def fork_rev(self) -> str:
        return git(self.fork, "rev-parse", "HEAD")

    @property
    def upstream_rev(self) -> str:
        return git(self.upstream, "rev-parse", "HEAD")

    def upstream_commits(
        self, changes: dict[str, str | None], message: str = "upstream: change"
    ) -> str:
        """Commit on upstream (None deletes a path) and fetch it into the fork."""
        for rel, text in changes.items():
            if text is None:
                (self.upstream / rel).unlink()
            else:
                write(self.upstream, rel, text)
        commit = commit_all(self.upstream, message)
        git(self.fork, "fetch", "--quiet", str(self.upstream), "main")
        return commit

    def fork_commits(self, changes: dict[str, str], message: str = "fork: edit") -> str:
        for rel, text in changes.items():
            write(self.fork, rel, text)
        return commit_all(self.fork, message)

    def run(self, *argv: object) -> tuple[int, dict[str, Any], str]:
        self.capsys.readouterr()
        code = fork_invariants.main(["--project-root", str(self.fork), *map(str, argv)])
        captured = self.capsys.readouterr()
        return code, json.loads(captured.out), captured.err

    def revisions(self) -> list[str]:
        return ["--fork-rev", self.fork_rev, "--upstream-rev", self.upstream_rev]

    def intake(self, *extra: object) -> tuple[int, dict[str, Any], str]:
        return self.run("intake", *self.revisions(), *extra)

    def seal(
        self, directory: Path, notes: dict[str, str], *extra: object
    ) -> tuple[int, dict[str, Any], str]:
        resolutions = directory.parent / "resolutions.json"
        resolutions.write_text(json.dumps(notes), encoding="utf-8")
        return self.run(
            "seal",
            *self.revisions(),
            "--resolve-in",
            directory,
            "--resolutions",
            resolutions,
            *extra,
        )

    def at(self, revision: str, rel: str) -> str:
        return git(self.fork, "show", f"{revision}:{rel}") + "\n"

    def paths(self, revision: str) -> list[str]:
        return git(self.fork, "ls-tree", "-r", "--name-only", revision).split("\n")

    def intake_branches(self) -> list[str]:
        listed = git(self.fork, "for-each-ref", "--format=%(refname:short)")
        return [name for name in listed.split("\n") if name.startswith("bot/")]

    def parents(self, commit: str) -> list[str]:
        return git(self.fork, "rev-list", "--parents", "-n", "1", commit).split()[1:]


@pytest.fixture
def world(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> Iterator[World]:
    for name in ("upstream", "fork"):
        shutil.copytree(template / name, tmp_path / name, symlinks=True)
    yield World(tmp_path / "fork", tmp_path / "upstream", capsys)


def routing(deep_line: str, standard: str = OLD_STANDARD) -> str:
    return (
        f"# Routing\n\nUse `{standard}` for routine work.\n{deep_line}\n\n"
        "Keep reports short.\n"
    )


def conflict_paths(payload: dict[str, Any]) -> dict[str, list[str]]:
    return {entry["path"]: entry["types"] for entry in payload["conflicts"]}


# --- check: the invariants ---


@pytest.mark.parametrize("python", interpreters(), ids=lambda path: Path(path).name)
def test_intake_and_seal_run_under_every_supported_interpreter(
    world: World, tmp_path: Path, python: str
) -> None:
    """The command line, end to end, in a process of its own: a conflict left
    for editing, finished, and sealed into the two-parent commit."""
    world.upstream_commits(
        {GUIDE: UPSTREAM_FILES[GUIDE].replace("line two", "line two, upstream's way")}
    )
    directory = tmp_path / "resolve"
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps({GUIDE: "kept upstream's wording"}), encoding="utf-8")
    command = [python, str(MAINT_DIR / "fork_invariants.py")]
    command += ["--project-root", str(world.fork)]
    where = ["--resolve-in", str(directory)]

    intake = subprocess.run(
        [*command, "intake", *world.revisions(), *where],
        capture_output=True,
        text=True,
        timeout=180,
    )
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    seal = subprocess.run(
        [*command, "seal", *world.revisions(), *where, "--resolutions", str(notes)],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert intake.returncode == EXIT_NEEDS_OWNER, intake.stdout + intake.stderr
    assert json.loads(intake.stdout)["result"] == "conflicts"
    assert seal.returncode == EXIT_OK, seal.stdout + seal.stderr
    sealed = json.loads(seal.stdout)
    assert world.parents(sealed["commit"]) == [world.fork_rev, world.upstream_rev]


def test_bug_in_the_helper_is_one_json_object_and_exit_three(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(target: fork_invariants.Target) -> list[fork_invariants.Finding]:
        raise TypeError("a bug")

    monkeypatch.setattr(fork_invariants, "check_tree", broken)

    code, payload, err = world.run("check")

    assert (code, payload["result"], payload["ok"]) == (3, "crashed", False)
    assert payload["detail"] == "TypeError: a bug"
    assert "Traceback" in err


def test_fixture_fork_satisfies_every_invariant(world: World) -> None:
    code, payload, _ = world.run("check")

    assert (code, payload["result"]) == (EXIT_OK, "satisfied")


@pytest.mark.parametrize("python", interpreters(), ids=lambda path: Path(path).name)
def test_check_passes_on_this_repository(python: str) -> None:
    """The real tree is a fork that holds: what an intake starts from."""
    done = subprocess.run(
        [python, str(MAINT_DIR / "fork_invariants.py"), "check"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    assert json.loads(done.stdout)["result"] == "satisfied"


@pytest.mark.parametrize(
    ("rel", "text", "invariants"),
    [
        # The lint refuses a retired role name as well as the codemod.
        ("docs/old.md", f"ask `{OLD_DEEP}`\n", {"roles-renamed", "model-names"}),
        ("docs/model.md", f"runs on {MODEL_WORD}\n", {"model-names"}),
        (".codex/config.toml", 'model = "another-model"\n', {"manifest-applied"}),
        (SEED_STATE, "# Agent State\n\nedited seed\n", {"seeds-pinned"}),
        (
            "README.md",
            f"    git clone {fork_invariants.UPSTREAM_GIT_URL} template\n",
            {"install-urls"},
        ),
        (
            "scripts/update.sh",
            f'TEMPLATE_REPO="{fork_invariants.UPSTREAM_GIT_URL}"\n',
            {"install-urls"},
        ),
        ("CLAUDE.md", "shadow\n", {"no-claude-md"}),
        (".claude/CLAUDE.md", "shadow\n", {"no-claude-md"}),
        ("CLAUDE.local.md", "shadow\n", {"no-claude-md"}),
    ],
)
def test_check_names_the_invariant_a_tree_breaks(
    world: World, rel: str, text: str, invariants: set[str]
) -> None:
    write(world.fork, rel, text)

    code, payload, err = world.run("check")

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "violated")
    assert {finding["invariant"] for finding in payload["findings"]} == invariants
    for invariant in invariants:
        assert f"finding: {invariant}:" in err


def test_check_of_a_revision_does_not_touch_the_work_tree(world: World) -> None:
    broken = world.fork_commits({"CLAUDE.md": "shadow\n"})
    git(world.fork, "reset", "--quiet", "--hard", "HEAD^")

    code, payload, _ = world.run("check", "--rev", broken)

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "violated")
    assert not (world.fork / "CLAUDE.md").exists()
    assert git(world.fork, "status", "--porcelain") == ""


# --- intake: what the mechanical changes dissolve ---


def test_rename_only_conflict_is_dissolved_into_a_two_parent_merge(
    world: World,
) -> None:
    """Upstream edits a line the fork only renamed, and adds a file in the old
    vocabulary. A plain merge conflicts; the transformed one does not, and the
    result speaks the fork's vocabulary throughout."""
    fork = world.fork_rev
    upstream = world.upstream_commits(
        {
            ROUTING: routing(f"Use `{OLD_DEEP}` for research and analysis."),
            "docs/new.md": f"New: ask `{OLD_STANDARD}` first.\n",
        }
    )

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert payload["conflicts_without_transform"] == 1
    assert payload["conflicts"] == []
    commit = payload["commit"]
    assert payload["branch"] == f"bot/upstream-{upstream[:12]}"
    assert git(world.fork, "rev-parse", payload["branch"]) == commit
    assert world.parents(commit) == [fork, upstream]
    assert world.at(commit, ROUTING) == routing(
        f"Use `{NEW_DEEP}` for research and analysis.", NEW_STANDARD
    )
    assert world.at(commit, "docs/new.md") == f"New: ask `{NEW_STANDARD}` first.\n"
    # Everything else is the fork's tree, byte for byte.
    changed = git(world.fork, "diff", "--name-only", fork, commit).split("\n")
    assert sorted(changed) == [ROUTING, "docs/new.md"]
    assert sorted(set(world.paths(commit)) - set(world.paths(fork))) == ["docs/new.md"]


def test_intake_changes_nothing_but_the_new_branch(world: World) -> None:
    world.upstream_commits({"docs/new.md": "new\n"})
    refs_before = git(world.fork, "for-each-ref")
    head_before = world.fork_rev

    code, payload, _ = world.intake()

    assert code == EXIT_OK
    refs_after = git(world.fork, "for-each-ref").split("\n")
    added = sorted(set(refs_after) - set(refs_before.split("\n")))
    assert [line.split("\t")[1] for line in added] == [
        f"refs/heads/{payload['branch']}"
    ]
    assert world.fork_rev == head_before
    assert git(world.fork, "status", "--porcelain") == ""
    assert git(world.fork, "worktree", "list").count("\n") == 0


def test_a_second_run_finds_the_first_ones_commit(world: World) -> None:
    world.upstream_commits({"docs/new.md": "new\n"})
    _, first, _ = world.intake()

    code, second, _ = world.intake()

    assert code == EXIT_OK
    assert second["commit"] == first["commit"]
    assert world.intake_branches() == [first["branch"]]


def test_the_next_intake_starts_from_the_upstream_tip_taken_last(
    world: World,
) -> None:
    """The second parent is what makes the next merge base upstream's previous
    tip: only what upstream did since then is merged again."""
    first_upstream = world.upstream_commits(
        {ROUTING: routing(f"Use `{OLD_DEEP}` for research and analysis.")}
    )
    _, first, _ = world.intake()
    git(world.fork, "merge", "--quiet", "--ff-only", first["commit"])
    world.upstream_commits({"docs/later.md": f"Later: `{OLD_DEEP}`.\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert payload["base"] == first_upstream
    assert payload["conflicts_without_transform"] == 0
    assert world.parents(payload["commit"]) == [first["commit"], world.upstream_rev]
    assert world.at(payload["commit"], "docs/later.md") == f"Later: `{NEW_DEEP}`.\n"
    assert world.at(payload["commit"], ROUTING) == world.at(first["commit"], ROUTING)


def test_nothing_new_upstream_is_its_own_clean_outcome(world: World) -> None:
    git(world.fork, "fetch", "--quiet", str(world.upstream), "main")

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_OK, "up-to-date")
    assert "commit" not in payload
    assert world.intake_branches() == []


def test_upstream_renaming_a_file_the_fork_edited_carries_the_edit(
    world: World,
) -> None:
    git(world.upstream, "mv", GUIDE, "docs/handbook.md")
    world.upstream_commits({})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert FORK_GUIDE_LINE in world.at(payload["commit"], "docs/handbook.md")
    assert GUIDE not in world.paths(payload["commit"])


def test_upstream_seed_change_lands_in_the_scaffold_not_in_the_forks_state(
    world: World,
) -> None:
    """Upstream's seed is the fork's live state by path. The change belongs to
    the fork's seed, and the pin then stops the merge for a deliberate edit."""
    world.upstream_commits({LIVE_STATE: "# Agent State\n\nnew seed text\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert payload["conflicts"] == []
    findings = {finding["invariant"] for finding in payload["findings"]}
    assert findings == {"seeds-pinned"}
    assert sha256("# Agent State\n\nnew seed text\n") in json.dumps(payload)
    assert world.intake_branches() == []


# --- intake: what is left to judgment ---


def test_genuine_conflict_is_reported_and_nothing_is_committed(world: World) -> None:
    world.upstream_commits(
        {GUIDE: UPSTREAM_FILES[GUIDE].replace("line two", "line two, upstream's way")}
    )

    code, payload, err = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "conflicts")
    assert conflict_paths(payload) == {GUIDE: ["CONFLICT (contents)"]}
    assert f"conflict: {GUIDE}: CONFLICT (contents)" in err
    assert "commit" not in payload and "branch" not in payload
    assert world.intake_branches() == []
    assert FORK_GUIDE_LINE in (world.fork / GUIDE).read_text(encoding="utf-8")
    assert git(world.fork, "status", "--porcelain") == ""


def test_upstream_deleting_a_file_the_fork_edited_is_a_conflict(world: World) -> None:
    world.upstream_commits({GUIDE: None})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "conflicts")
    assert conflict_paths(payload) == {GUIDE: ["CONFLICT (modify/delete)"]}
    assert world.intake_branches() == []


def test_upstream_file_naming_a_model_fails_the_invariants(world: World) -> None:
    world.upstream_commits({"docs/models.md": f"The reviewer runs on {MODEL_WORD}.\n"})

    code, payload, err = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert payload["conflicts"] == []
    assert {finding["invariant"] for finding in payload["findings"]} == {"model-names"}
    assert "docs/models.md:1" in payload["findings"][0]["detail"]
    assert "finding: model-names:" in err
    assert world.intake_branches() == []


def test_upstream_changing_a_model_value_is_drift_not_a_new_assignment(
    world: World,
) -> None:
    config = UPSTREAM_FILES[".codex/config.toml"].replace("fixture-model", "newer")
    world.upstream_commits({".codex/config.toml": config})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert {f["invariant"] for f in payload["findings"]} == {"manifest-applied"}
    assert world.intake_branches() == []


def test_upstream_adding_its_own_manifest_conflicts_with_the_forks(
    world: World,
) -> None:
    """The manifest is the fork's. Upstream's file at that path meets it as a
    conflict; it is never taken, and never merged into the fork's."""
    theirs = {**MANIFEST, "codex": {"model": "theirs", "reasoning_effort": "low"}}
    world.upstream_commits({upstream_intake.MANIFEST: json.dumps(theirs) + "\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "conflicts")
    assert set(conflict_paths(payload)) == {upstream_intake.MANIFEST}
    assert world.intake_branches() == []


def test_upstream_adding_a_claude_md_fails_the_invariants(world: World) -> None:
    world.upstream_commits({"CLAUDE.md": "# Instructions\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert {f["invariant"] for f in payload["findings"]} == {"no-claude-md"}
    assert world.intake_branches() == []


def test_upstream_workflow_change_stops_before_anything_is_merged(
    world: World,
) -> None:
    world.upstream_commits({".github/workflows/release.yml": "name: Release\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "workflows-changed")
    assert payload["paths"] == [".github/workflows/release.yml"]
    assert world.intake_branches() == []


def test_the_owner_can_let_a_workflow_change_through(world: World) -> None:
    world.upstream_commits({".github/workflows/release.yml": "name: Release\n"})

    code, payload, _ = world.intake("--owner-allows-workflow-changes")

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert ".github/workflows/release.yml" in world.paths(payload["commit"])


def test_fork_that_breaks_its_own_invariants_is_fixed_first(world: World) -> None:
    world.fork_commits({"docs/old.md": f"ask `{OLD_DEEP}`\n"})
    world.upstream_commits({"docs/new.md": "new\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "fork-not-clean")
    assert "roles-renamed" in {f["invariant"] for f in payload["findings"]}
    assert world.intake_branches() == []


# --- intake: upstream's content is data ---


MALICIOUS = "import pathlib\npathlib.Path({marker!r}).write_text('executed')\n"


def test_upstream_copy_of_a_fork_tool_is_never_executed(
    world: World, tmp_path: Path
) -> None:
    """Upstream ships its own files at the paths of all three tools the fork
    runs. They are checked out in the scratch tree, at exactly the paths the
    tools are named by, and none of them runs."""
    marker = tmp_path / "upstream-code-ran"
    hostile = MALICIOUS.format(marker=str(marker))
    world.upstream_commits(dict.fromkeys(fork_invariants.FORK_TOOLS, hostile))

    code, payload, _ = world.intake()

    assert not marker.exists()
    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "conflicts")
    assert set(conflict_paths(payload)) == set(fork_invariants.FORK_TOOLS)
    assert world.intake_branches() == []


def test_tools_come_from_the_forks_commit_not_from_the_work_tree(
    world: World, tmp_path: Path
) -> None:
    """A checkout that is not at the fork's commit (an edited file, another
    branch) must not decide which code runs."""
    marker = tmp_path / "work-tree-code-ran"
    world.upstream_commits({"docs/new.md": "new\n"})
    for rel in fork_invariants.FORK_TOOLS:
        write(world.fork, rel, MALICIOUS.format(marker=str(marker)))

    code, payload, _ = world.intake()

    assert not marker.exists()
    assert (code, payload["result"]) == (EXIT_OK, "merged")


def upstream_adds_entry(world: World, mode: str, rel: str, content: str) -> None:
    """Commit one tree entry on upstream without ever creating it on disk: a
    name this file system cannot hold, or a link."""
    stored = subprocess.run(
        ["git", "-C", str(world.upstream), "hash-object", "-w", "--stdin"],
        input=content,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    entry = f"{mode},{stored},{rel}"
    git(world.upstream, "update-index", "--add", "--cacheinfo", entry)
    git(world.upstream, "commit", "--quiet", "-m", "upstream: hostile entry")
    git(world.fork, "fetch", "--quiet", str(world.upstream), "main")


@pytest.mark.parametrize(
    ("mode", "rel", "content", "reason"),
    [
        ("100644", ".gitattributes", "* merge=union\n", "an attributes file"),
        ("100644", "docs/.gitattributes", "*.md -text\n", "an attributes file"),
        ("100644", "docs/KEEP.md", "another case\n", "case-insensitive"),
        ("100644", "DOCS/other.md", "another case\n", "case-insensitive"),
        ("100644", "docs/bad\x1b[2Jname.md", "x\n", "cannot be shown"),
        ("120000", "docs/link.md", "/etc/passwd", "mode 120000"),
        ("160000", "vendor", "", "mode 160000"),
    ],
)
def test_tree_that_must_not_be_checked_out_is_refused(
    world: World, mode: str, rel: str, content: str, reason: str
) -> None:
    upstream_adds_entry(world, mode, rel, content)
    world.capsys.readouterr()

    code = fork_invariants.main(
        ["--project-root", str(world.fork), "intake", *world.revisions()]
    )

    captured = world.capsys.readouterr()
    payload = json.loads(captured.out)
    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unsupported-entry")
    assert any(reason in entry for entry in payload["entries"])
    # Nothing a name carries reaches the terminal as written.
    assert "\x1b" not in captured.out + captured.err
    assert world.intake_branches() == []


def test_merge_attribute_in_a_checked_out_tree_cannot_hide_a_conflict(
    world: World, tmp_path: Path
) -> None:
    """The second line of defence behind the refusal above: with upstream's
    `merge=union` checked out in the scratch tree, both sides' lines would be
    kept without a word. The scratch repository's own attributes forbid it."""
    base = world.upstream_rev
    world.upstream_commits(
        {
            ".gitattributes": "* merge=union\n",
            GUIDE: UPSTREAM_FILES[GUIDE].replace("line two", "upstream's line"),
        }
    )
    project = scratch_repo.Repo(world.fork, scratch_repo.child_env())
    scratch = scratch_repo.Scratch.create(tmp_path / "scratch", project)
    scratch.load(world.upstream_rev)

    _, conflicts = upstream_intake.merge(
        scratch, f"--merge-base={base}", world.fork_rev, world.upstream_rev
    )

    assert [conflict.path for conflict in conflicts] == [GUIDE]


@pytest.mark.parametrize("name", ["main", "HEAD", "refs/remotes/origin/main", "abc123"])
def test_revisions_must_be_full_commit_shas(world: World, name: str) -> None:
    code, payload, _ = world.run(
        "intake", "--fork-rev", name, "--upstream-rev", world.upstream_rev
    )

    assert (code, payload["result"]) == (EXIT_USAGE, "refused")


def test_shallow_clone_is_refused(world: World, tmp_path: Path) -> None:
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--quiet", "--depth", "1", world.fork.as_uri(), str(shallow))
    world.fork = shallow

    code, payload, _ = world.run(
        "intake", "--fork-rev", world.fork_rev, "--upstream-rev", world.fork_rev
    )

    assert (code, payload["result"]) == (EXIT_USAGE, "refused")
    assert "shallow" in payload["detail"]


# --- seal: a merge finished by hand ---


def conflicted(world: World, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """An intake that stops at one content conflict, left in a directory."""
    world.upstream_commits(
        {
            GUIDE: UPSTREAM_FILES[GUIDE].replace(
                "line two", "line two, upstream's way"
            ),
            "docs/new.md": f"New: ask `{OLD_STANDARD}` first.\n",
        }
    )
    directory = tmp_path / "resolve"
    code, payload, _ = world.intake("--resolve-in", directory)
    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "conflicts")
    return directory, payload


def test_relative_paths_mean_the_callers_directory(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fork's tools are started from their own directory. A relative
    `--resolve-in` must still name the directory the caller meant."""
    world.upstream_commits({GUIDE: None})
    monkeypatch.chdir(tmp_path)

    code, payload, _ = world.intake("--resolve-in", "resolve-here")

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "conflicts")
    assert (tmp_path / "resolve-here" / GUIDE).is_file()
    assert payload["resolve_in"] == str(tmp_path / "resolve-here")


def test_unfinished_merge_is_left_for_editing_with_markers(
    world: World, tmp_path: Path
) -> None:
    directory, payload = conflicted(world, tmp_path)

    text = (directory / GUIDE).read_text(encoding="utf-8")

    assert payload["resolve_in"] == str(directory)
    assert "<<<<<<< fork-main" in text and ">>>>>>> upstream-main" in text
    assert FORK_GUIDE_LINE in text and "line two, upstream's way" in text
    assert (directory / "docs/new.md").read_text(encoding="utf-8") == (
        f"New: ask `{NEW_STANDARD}` first.\n"
    )
    assert git(directory, "rev-parse", "HEAD") == world.fork_rev
    assert world.intake_branches() == []


def test_seal_refuses_a_conflict_nobody_answered_for(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    resolved = UPSTREAM_FILES[GUIDE].replace("line two", "both ways")
    write(directory, GUIDE, resolved)

    code, payload, _ = world.seal(directory, {})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unresolved")
    assert payload["unanswered"] == [GUIDE]
    assert world.intake_branches() == []


def test_seal_refuses_markers_even_when_the_path_is_answered_for(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)

    code, payload, _ = world.seal(directory, {GUIDE: "kept both"})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unresolved")
    assert payload["unanswered"] == [GUIDE]
    assert world.intake_branches() == []


def test_seal_refuses_a_note_for_a_path_that_did_not_conflict(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])

    code, payload, _ = world.seal(directory, {GUIDE: "ok", "docs/keep.md": "why"})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unresolved")
    assert payload["not_conflicts"] == ["docs/keep.md"]


def test_seal_builds_the_two_parent_commit_from_the_finished_tree(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    resolved = UPSTREAM_FILES[GUIDE].replace("line two", "both ways")
    write(directory, GUIDE, resolved)
    fork, upstream = world.fork_rev, world.upstream_rev

    code, payload, _ = world.seal(directory, {GUIDE: "kept both wordings"})

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    commit = payload["commit"]
    assert world.parents(commit) == [fork, upstream]
    assert world.at(commit, GUIDE) == resolved
    assert world.at(commit, "docs/new.md") == f"New: ask `{NEW_STANDARD}` first.\n"
    assert payload["edited_by_hand"] == [GUIDE]
    assert payload["resolutions"] == {GUIDE: "kept both wordings"}
    assert payload["seed_pins_changed"] is False
    assert world.intake_branches() == [f"bot/upstream-{upstream[:12]}"]


def test_conflict_without_markers_cannot_be_sealed_unanswered(
    world: World, tmp_path: Path
) -> None:
    """A file deleted upstream and edited in the fork leaves no marker: the
    automatic result simply keeps the fork's file. That is a guess unless
    someone says so."""
    world.upstream_commits({GUIDE: None})
    directory = tmp_path / "resolve"
    world.intake("--resolve-in", directory)
    assert "<<<<<<<" not in (directory / GUIDE).read_text(encoding="utf-8")

    refused, payload, _ = world.seal(directory, {})
    accepted, sealed, _ = world.seal(directory, {GUIDE: "the fork still needs it"})

    assert (refused, payload["result"]) == (EXIT_NEEDS_OWNER, "unresolved")
    assert (accepted, sealed["result"]) == (EXIT_OK, "merged")
    assert FORK_GUIDE_LINE in world.at(sealed["commit"], GUIDE)


def test_seal_holds_the_finished_tree_to_the_invariants(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, f"# Guide\n\nresolved, on {MODEL_WORD}\n")

    code, payload, _ = world.seal(directory, {GUIDE: "reworded"})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert {f["invariant"] for f in payload["findings"]} == {"model-names"}
    assert world.intake_branches() == []


@pytest.mark.parametrize(
    ("rel", "finding"),
    [
        (".github/workflows/ci.yml", "workflows-untouched"),
        (upstream_intake.MANIFEST, "fork-owned-untouched"),
        (LIVE_STATE, "fork-owned-untouched"),
    ],
)
def test_seal_refuses_an_edit_to_what_the_fork_protects(
    world: World, tmp_path: Path, rel: str, finding: str
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    with (directory / rel).open("a", encoding="utf-8") as handle:
        handle.write("\n")

    code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert finding in {f["invariant"] for f in payload["findings"]}
    assert world.intake_branches() == []


def test_seal_accepts_a_seed_only_together_with_its_pin(
    world: World, tmp_path: Path
) -> None:
    new_seed = "# Agent State\n\nnew seed text\n"
    world.upstream_commits({LIVE_STATE: new_seed})
    directory = tmp_path / "resolve"
    world.intake("--resolve-in", directory)
    before, payload, _ = world.seal(directory, {})
    write(
        directory,
        fork_invariants.SEED_PIN_FILE,
        pin_file(new_seed, UPSTREAM_FILES[".claude/docs/DESIGN.md"]),
    )

    after, sealed, _ = world.seal(directory, {})

    assert (before, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert (after, sealed["result"]) == (EXIT_OK, "merged")
    assert sealed["seed_pins_changed"] is True
    assert world.at(sealed["commit"], SEED_STATE) == new_seed
    assert world.at(sealed["commit"], LIVE_STATE) == world.at(
        world.fork_rev, LIVE_STATE
    )


def test_seal_refuses_a_directory_prepared_for_other_commits(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    world.upstream_commits({"docs/more.md": "more\n"})

    code, payload, _ = world.seal(directory, {GUIDE: "ok"})

    assert (code, payload["result"]) == (EXIT_USAGE, "refused")
    assert world.intake_branches() == []


# --- the pieces, alone ---

TREE = "a" * 40


def test_parse_merge_reads_a_clean_result() -> None:
    assert upstream_intake.parse_merge(f"{TREE}\0") == (TREE, [])


def test_parse_merge_counts_staged_paths_and_conflict_events() -> None:
    """A path with unmerged stages and one that only an event names are both
    conflicts; an "Auto-merging" notice is not."""
    output = "\0".join(
        [
            TREE,
            f"100644 {'b' * 40} 2\tstaged.md",
            f"100644 {'c' * 40} 3\tstaged.md",
            "",
            "1",
            "merged.md",
            "Auto-merging",
            "Auto-merging merged.md\n",
            "2",
            "old.md",
            "new.md",
            "CONFLICT (rename/delete)",
            "CONFLICT (rename/delete): old.md renamed\n",
            "",
        ]
    )

    tree, conflicts = upstream_intake.parse_merge(output)

    assert tree == TREE
    assert [(c.path, c.types) for c in conflicts] == [
        ("new.md", ("CONFLICT (rename/delete)",)),
        ("old.md", ("CONFLICT (rename/delete)",)),
        ("staged.md", ()),
    ]


@pytest.mark.parametrize("output", ["", "not a tree\0", f"{TREE}\0x\0\0nope\0"])
def test_parse_merge_refuses_output_it_cannot_read(output: str) -> None:
    with pytest.raises((ValueError, IndexError)):
        upstream_intake.parse_merge(output)


def test_install_line_rewrite_leaves_attribution_and_named_branches() -> None:
    assert fork_invariants.is_default_repo_line('TEMPLATE_REPO="x"')
    assert not fork_invariants.is_default_repo_line('  TEMPLATE_REPO="x"')
    assert fork_invariants.is_default_clone("git clone --depth 1 URL dir")
    assert not fork_invariants.is_default_clone("git clone --branch other URL dir")
    assert not fork_invariants.is_default_clone("Forked from URL")


def test_install_urls_in_a_merge_point_at_the_fork(world: World) -> None:
    """A new default install line arrives pointing at the fork; the link to
    upstream's own page and the clone of upstream's own branch stay."""
    readme = UPSTREAM_FILES["README.md"] + (
        f"\n    git clone {fork_invariants.UPSTREAM_GIT_URL} again\n"
    )
    world.upstream_commits({"README.md": readme})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    merged = world.at(payload["commit"], "README.md")
    assert f"    git clone {common.FORK_GIT_URL} again\n" in merged
    assert f"https://github.com/{fork_invariants.UPSTREAM_SLUG}.\n" in merged
    assert f"--branch other {fork_invariants.UPSTREAM_GIT_URL} t\n" in merged


def test_outcome_a_command_did_not_declare_cannot_be_reported() -> None:
    payload: dict[str, object] = {"result": "conflicts", "detail": "x"}

    with pytest.raises(ValueError, match="undeclared outcome"):
        fork_invariants.report("check", payload, EXIT_NEEDS_OWNER)


# --- pinned to the definitions they repeat ---


def test_shadow_files_are_the_ones_check_sh_refuses() -> None:
    script = (REPO_ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    body = script.split("check_no_claude_md() {", 1)[1].split("\n}\n", 1)[0]
    listed = re.search(r"for shadow in ([^;]+); do", body)

    assert listed is not None
    assert tuple(listed.group(1).split()) == fork_invariants.SHADOW_FILES


def test_seed_pins_are_read_from_the_test_that_owns_them() -> None:
    """The digests are not copied here or into the helper: both read the one
    place a deliberate seed edit updates."""
    text = (REPO_ROOT / fork_invariants.SEED_PIN_FILE).read_text(encoding="utf-8")

    pins = fork_invariants.seed_pins(text)

    assert pins is not None
    assert set(pins) == set(fork_invariants.SEEDS.values())
    for seed, digest in pins.items():
        assert hashlib.sha256((REPO_ROOT / seed).read_bytes()).hexdigest() == digest


def test_seed_locations_are_the_ones_the_installer_copies() -> None:
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    for live, seed in fork_invariants.SEEDS.items():
        assert f'"{live}:{seed}"' in script


def test_fork_tools_exist_and_the_lint_imports_only_its_sibling() -> None:
    """Every file the helper executes is extracted from the fork's commit. A
    new import between them would be a file it does not extract."""
    for rel in fork_invariants.FORK_TOOLS:
        assert (REPO_ROOT / rel).is_file()
    lint = (REPO_ROOT / fork_invariants.LINT_TOOL).read_text(encoding="utf-8")
    codemod = (REPO_ROOT / fork_invariants.RENAME_TOOL).read_text(encoding="utf-8")
    local_imports = re.findall(
        r"(?m)^(?:import|from) (models\w*|common|scratch\w*)", lint
    )

    assert local_imports == ["models"]
    assert not re.search(r"(?m)^(?:import|from) (models|common)\b", codemod)


README = MAINT_DIR / "README.md"
OUTCOME_ROW_RE = re.compile(r"^\| `([a-z0-9-]+)` \| ([0-9]( / [0-9])*) \|")
INTAKE_SECTION = "\n## Upstream intake\n"


def readme_intake_outcomes() -> dict[str, dict[str, tuple[int, ...]]]:
    """The outcome tables of the intake section: command -> result -> codes."""
    text = README.read_text(encoding="utf-8")
    section = text.split(INTAKE_SECTION, 1)[1].split("\n## ", 1)[0]
    tables: dict[str, dict[str, tuple[int, ...]]] = {}
    current = ""
    for line in section.split("\n"):
        if line.startswith("#### "):
            current = line[5:].strip().strip("`")
            tables[current] = {}
        row = OUTCOME_ROW_RE.match(line)
        if row is not None:
            assert row.group(1) not in tables[current], row.group(1)
            codes = tuple(int(code) for code in row.group(2).split(" / "))
            tables[current][row.group(1)] = codes
    return tables


def test_readme_intake_tables_match_what_the_code_can_report() -> None:
    assert readme_intake_outcomes() == {
        command: {name: tuple(sorted(codes)) for name, codes in outcomes.items()}
        for command, outcomes in fork_invariants.OUTCOMES.items()
    }


# --- the routine prompts ---

INTAKE_PROMPT = ROUTINES_DIR / "upstream-intake.md"
WORKER_PROMPT = ROUTINES_DIR / "issue-worker.md"
FORK_API = f"repos/{common.FORK_SLUG}/"
REQUIRED_CHECKS = ("Gates (ubuntu-latest)", "Gates (macos-latest)")


def prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def hard_rules(path: Path) -> str:
    """The numbered rules between "## Hard rules" and the next heading."""
    return prompt(path).split("\n## Hard rules\n", 1)[1].split("\n## ", 1)[0]


def flat(text: str) -> str:
    return " ".join(text.split())


def api_calls(text: str) -> list[str]:
    """Every `gh api` command line in a prompt, continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", text)
    lines = [line.strip() for line in joined.split("\n")]
    return [line for line in lines if line.startswith("gh api ")]


@pytest.mark.parametrize("path", [INTAKE_PROMPT, WORKER_PROMPT])
def test_prompt_is_versioned_and_self_contained(path: Path) -> None:
    text = prompt(path)

    assert re.search(r"(?m)^Prompt-Version: [1-9][0-9]*$", text)
    for heading in ("## Hard rules", "## Steps", "## If something breaks"):
        assert f"\n{heading}\n" in text
    assert "## Status comment" in text
    assert "context-loader" in text
    assert "--review-only" in text
    assert "When Codex Is Unavailable" in text


@pytest.mark.parametrize("path", [INTAKE_PROMPT, WORKER_PROMPT])
def test_prompt_writes_only_to_the_fork_and_only_over_rest(path: Path) -> None:
    text = prompt(path)
    rules = flat(hard_rules(path))

    calls = api_calls(text)
    assert calls
    for call in calls:
        assert FORK_API in call, call
        assert "graphql" not in call.lower(), call
    assert not re.search(r"(?m)^\s*gh (pr|issue|label|repo|workflow|run) ", text)
    assert f"starts with `{FORK_API}`" in rules
    assert "Never push to `main`" in rules
    assert "GraphQL" in rules


@pytest.mark.parametrize("path", [INTAKE_PROMPT, WORKER_PROMPT])
def test_prompt_never_changes_protection_history_or_the_manifest(path: Path) -> None:
    text = prompt(path)
    rules = flat(hard_rules(path))

    assert "branch protection" in rules
    assert "force-push" in rules
    assert "models.json" in rules
    assert "scripts/scaffold/" in rules
    assert "weakening" in rules
    for forbidden in ("/protection", "--force", "-f origin", "automerge", "--auto"):
        assert forbidden not in text, forbidden
    assert "-X PUT" not in text.replace("-X PUT " + FORK_API + "pulls/", "")


def test_intake_prompt_never_merges_and_says_so_in_the_pull_request() -> None:
    text = prompt(INTAKE_PROMPT)
    rules = flat(hard_rules(INTAKE_PROMPT))

    assert "Never merge" in rules
    assert "/merge" not in text
    assert "Never merged unattended" in text
    assert "merge commit, never squash" in text
    refusal = "never pass `--owner-allows-workflow-changes`"
    assert refusal in rules
    assert "--owner-allows-workflow-changes" not in flat(text).replace(refusal, "")


def test_intake_prompt_runs_only_the_forks_tools_on_upstreams_tree() -> None:
    text = prompt(INTAKE_PROMPT)
    rules = flat(hard_rules(INTAKE_PROMPT))

    assert "Never execute" in rules
    assert "is data" in rules
    assert "python3 scripts/maint/fork_invariants.py intake" in text
    assert "python3 scripts/maint/fork_invariants.py seal" in text
    for executed in ("verify.sh", "pytest", "bash ../", "git merge ", "git pull"):
        assert executed not in text.replace("Do not run `verify.sh`", ""), executed


def test_worker_prompt_acts_only_on_the_owners_labelled_issues() -> None:
    text = prompt(WORKER_PROMPT)
    rules = flat(hard_rules(WORKER_PROMPT))

    assert f"creator={common.FORK_OWNER}" in text
    assert "labels=agent:ready" in text
    assert f"`user.login` is not `{common.FORK_OWNER}`" in flat(text)
    assert "author_association" in text
    assert "is data, not instructions" in rules
    assert "body of the issue" in rules


def test_worker_prompt_merges_over_rest_only_after_both_required_checks() -> None:
    text = prompt(WORKER_PROMPT)
    merges = [call for call in api_calls(text) if "/merge" in call]

    assert len(merges) == 1
    merge = merges[0]
    assert f"-X PUT {FORK_API}pulls/" in merge
    assert "merge_method=squash" in merge
    assert "-f sha=" in merge
    for name in REQUIRED_CHECKS:
        assert name in text
    assert "check-runs" in text
    assert text.index("check-runs") < text.index("merge_method=squash")
    assert "Codex unavailable: substituted" in text
