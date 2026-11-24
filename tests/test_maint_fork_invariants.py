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
import os
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
if str(MAINT_DIR) not in sys.path:
    sys.path.insert(0, str(MAINT_DIR))

import common  # noqa: E402
import fork_invariants  # noqa: E402
import gh  # noqa: E402
import intake_seal  # noqa: E402
import report  # noqa: E402
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
EXIT_OK, EXIT_USAGE, EXIT_NEEDS_OWNER, EXIT_FAILED = 0, 1, 2, 3
SYSTEM_PYTHON = Path("/usr/bin/python3")
# The interpreters the helper must run under: the one running the tests, and
# the system one (Python 3.9 on macOS). The second is listed even where it is
# missing, so that its absence shows up as a skip with a reason.
INTERPRETERS = pytest.mark.parametrize(
    "python", [sys.executable, str(SYSTEM_PYTHON)], ids=["this-python", "system-python"]
)
# Variables that aim git at another repository.
GIT_REDIRECTS = ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE")


def usable(python: str) -> str:
    """``python``, or an explicit skip when it adds no second version."""
    if python == sys.executable:
        return python
    if not SYSTEM_PYTHON.exists():
        pytest.skip(f"{SYSTEM_PYTHON} is not installed; the oldest Python is not run")
    probe = subprocess.run(
        [python, "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip(f"{SYSTEM_PYTHON} does not start; the oldest Python is not run")
    if probe.stdout.strip() == str(sys.version_info[:2]):
        pytest.skip(f"{SYSTEM_PYTHON} is the version the tests already run under")
    return python


def git(root: Path, *args: str) -> str:
    """git for the tests' own bookkeeping, never aimed anywhere by the
    environment a test sets up for the helper."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_REDIRECTS}
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            # The developer's own ignore file must not hide a fixture file.
            "-c",
            "core.excludesFile=/dev/null",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
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


def cli(python: str, world: World, *argv: object) -> tuple[int, dict[str, Any]]:
    """The command line in a process of its own, under one interpreter."""
    script = str(MAINT_DIR / "fork_invariants.py")
    done = subprocess.run(
        [python, script, "--project-root", str(world.fork), *map(str, argv)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.stdout.strip(), done.stderr
    return done.returncode, json.loads(done.stdout)


# What upstream does -> the outcome `intake` must report for it.
OUTCOME_CASES = {
    "up-to-date": ({}, EXIT_OK),
    "merged": ({"docs/new.md": "new\n"}, EXIT_OK),
    "invariants-failed": (
        {"docs/models.md": f"runs on {MODEL_WORD}\n"},
        EXIT_NEEDS_OWNER,
    ),
    "unsupported-entry": ({".gitattributes": "* -text\n"}, EXIT_NEEDS_OWNER),
    "workflows-changed": ({".github/workflows/new.yml": "name: X\n"}, EXIT_NEEDS_OWNER),
}


@INTERPRETERS
@pytest.mark.parametrize("outcome", sorted(OUTCOME_CASES))
def test_every_outcome_runs_under_every_supported_interpreter(
    world: World, python: str, outcome: str
) -> None:
    """Each branch of the code is its own chance to use something the oldest
    supported Python lacks, so each outcome is run there, not one of them."""
    usable(python)
    changes, expected = OUTCOME_CASES[outcome]
    if changes:
        world.upstream_commits(changes)

    code, payload = cli(python, world, "intake", *world.revisions())

    assert (code, payload["result"]) == (expected, outcome), payload


@INTERPRETERS
def test_check_runs_under_every_supported_interpreter(
    world: World, python: str
) -> None:
    usable(python)

    in_place = cli(python, world, "check")
    of_commit = cli(python, world, "check", "--rev", world.fork_rev)

    assert (in_place[0], in_place[1]["result"]) == (EXIT_OK, "satisfied")
    assert (of_commit[0], of_commit[1]["result"]) == (EXIT_OK, "satisfied")


@INTERPRETERS
def test_intake_and_seal_run_under_every_supported_interpreter(
    world: World, tmp_path: Path, python: str
) -> None:
    """The command line, end to end, in a process of its own: a conflict left
    for editing, finished, and sealed into the two-parent commit."""
    usable(python)
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


@INTERPRETERS
def test_check_passes_on_this_repository(python: str) -> None:
    """The real fork holds: what an intake starts from. It is the commit that
    is judged, not the work tree: an intake starts from a commit, and the
    work tree may hold files that belong to nobody (pytest's own temporary
    directory, when it is put inside the repository). A shallow clone, which
    is what CI checks out, is enough for this."""
    usable(python)
    head = git(REPO_ROOT, "rev-parse", "HEAD")
    done = subprocess.run(
        [python, str(MAINT_DIR / "fork_invariants.py"), "check", "--rev", head],
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
    # For a person: the path is in a code span, as it must be when posted.
    assert f"conflict: `{GUIDE}`: CONFLICT (contents)" in err
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


# A reported name: the supported characters, and `\xNN` for anything else.
INERT_NAME_RE = re.compile(r"(?:[A-Za-z0-9._/+@-]|\\x[0-9a-f]{2}|\\u[0-9a-f]{4})+")


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
        ("100644", "docs/bad\x1b[2Jname.md", "x\n", "a character outside"),
        ("100644", "docs/a'$(touch PWNED)'.md", "x\n", "a character outside"),
        ("100644", "docs/`id`.md", "x\n", "a character outside"),
        ("100644", "docs/two words.md", "x\n", "a character outside"),
        ("100644", "docs/caf\u00e9.md", "x\n", "a character outside"),
        ("100644", "docs/-rf", "x\n", "starts with a dash"),
        ("100644", "-n/x.md", "x\n", "starts with a dash"),
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
    # Nothing a name carries reaches the terminal, a shell or Markdown as
    # written: what is reported of a refused name is made of the characters
    # a supported name may have, plus visible escapes.
    assert "\x1b" not in captured.out + captured.err
    for entry in payload["entries"]:
        name = entry.split(": ", 1)[0]
        assert name.startswith("`") and name.endswith("`"), entry
        assert INERT_NAME_RE.fullmatch(name.strip("`")), entry
    assert "PWNED)" not in captured.out + captured.err
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
    section = section.split("\n### Issue worker", 1)[0]
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


def readme_invariants() -> list[str]:
    text = README.read_text(encoding="utf-8")
    table = text.split("\n### The invariants\n", 1)[1].split("\n### ", 1)[0]
    return re.findall(r"(?m)^\| `([a-z-]+)` \|", table)


def test_readme_names_every_invariant_a_finding_can_carry() -> None:
    assert readme_invariants() == list(fork_invariants.INVARIANT_NAMES)


# --- what a caller is handed is already inert ---


@pytest.mark.parametrize(
    ("name", "reported"),
    [
        ("docs/guide.md", "docs/guide.md"),
        ("a+b@c/d_e-f.G9", "a+b@c/d_e-f.G9"),
        ("docs/a'$(touch PWNED)'.md", r"docs/a\x27\x24\x28touch\x20PWNED\x29\x27.md"),
        ("-rf", r"\x2drf"),
        ("docs/caf" + chr(0xE9) + " " + chr(0x2603), r"docs/caf\xe9\x20\u2603"),
    ],
)
def test_reported_path_cannot_act_in_a_shell_or_in_markdown(
    name: str, reported: str
) -> None:
    assert scratch_repo.inert_path(name) == reported


@pytest.mark.parametrize(
    ("text", "reported"),
    [
        ("feat(x): add y, keep z", "feat(x): add y, keep z"),
        ("``` Closes #1 @someone", r"\x60\x60\x60 Closes \x231 \x40someone"),
        ("a | b [x](y) <!-- c -->", r"a \x7c b \x5bx\x5d(y) \x3c\x21-- c --\x3e"),
        ("see http://evil.example now", r"see http:\x2f/evil.example now"),
        ("see WWW.evil.example", r"see WWW\x2eevil.example"),
        ("fixes GH-12 and gh-7", r"fixes GH\x2d12 and gh\x2d7"),
        ("- a list item", r"\x2d a list item"),
        ("3. a numbered item", r"3\x2e a numbered item"),
        ("=======", r"\x3d======"),
        ("two\nlines\there", "two lines here"),
        ("$(id) && `id`", r"\x24(id) \x26\x26 \x60id\x60"),
        (
            "; rm -rf / ; echo 'x' \"y\" (z) a=b",
            r"; rm -rf / ; echo \x27x\x27 \x22y\x22 (z) a=b",
        ),
        (
            "- [ ] task; rm -rf / ' \" | cat",
            r"\x2d \x5b \x5d task; rm -rf / \x27 \x22 \x7c cat",
        ),
        ('it\'s "quoted"', r"it\x27s \x22quoted\x22"),
    ],
)
def test_reported_text_cannot_form_markdown_or_a_command(
    text: str, reported: str
) -> None:
    assert scratch_repo.inert_text(text) == reported
    # A line can pass through two reports: the second changes nothing.
    assert scratch_repo.inert_text(reported) == reported
    assert scratch_repo.inert_path(scratch_repo.inert_path(text)) == (
        scratch_repo.inert_path(text)
    )


HOSTILE_ARGUMENTS = [
    "'; touch {marker}; echo '",
    '"; touch {marker}; echo "',
    "$(touch {marker})",
    "`touch {marker}`",
    "x' \"$(touch {marker})\" 'y",
    'a\\"; touch {marker}; "',
    "!! && touch {marker}",
]


@pytest.mark.parametrize("hostile", HOSTILE_ARGUMENTS)
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("inert", ["inert_text", "inert_path"])
def test_reported_string_is_one_argument_inside_either_kind_of_quote(
    tmp_path: Path, hostile: str, quote: str, inert: str
) -> None:
    """The contract, run through a real shell: quoted either way, the string
    stays one argument, comes out as it went in, and runs nothing."""
    marker = tmp_path / "ran"
    made = getattr(scratch_repo, inert)(hostile.format(marker=marker))

    done = subprocess.run(
        ["sh", "-c", f"printf '%s|' {quote}{made}{quote} end"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"{made}|end|"
    assert not marker.exists()
    assert "'" not in made and '"' not in made


def test_path_is_posted_in_a_code_span_because_a_name_may_read_as_a_link() -> None:
    name = "www.evil.example/@octocat"

    assert scratch_repo.inert_path(name) == name
    assert scratch_repo.code_span(name) == f"`{name}`"
    assert scratch_repo.code_span("a`b") == r"`a\x60b`"


HOSTILE_SUBJECT = "``` Closes #1 @someone <!-- job:x --> [x](http://e) | GH-2"


def test_upstream_commit_subjects_are_given_only_in_the_inert_form(
    world: World,
) -> None:
    """A subject is upstream's own words. Pasted as it is into a pull request
    under the owner's name it would close a code block, close an issue and
    mention people; the helper's field cannot."""
    first = world.upstream_commits({"docs/one.md": "one\n"}, "docs: a plain subject")
    second = world.upstream_commits({"docs/two.md": "two\n"}, HOSTILE_SUBJECT)

    code, payload, _ = world.intake()

    assert code == EXIT_OK
    assert payload["upstream_commits"] == 2
    assert [entry["commit"] for entry in payload["upstream_log"]] == [second, first]
    assert payload["upstream_log"][1]["subject"] == "docs: a plain subject"
    hostile = payload["upstream_log"][0]["subject"]
    assert hostile == scratch_repo.inert_text(HOSTILE_SUBJECT)
    assert not set("`#@|<>[]!") & set(hostile)
    assert "://" not in hostile and "GH-" not in hostile


def test_finding_quotes_what_a_tool_read_only_in_the_inert_form(world: World) -> None:
    """A checker quotes the tree: here the value upstream put in a config."""
    config = UPSTREAM_FILES[".codex/config.toml"].replace("fixture-model", "x`id`|y")
    world.upstream_commits({".codex/config.toml": config})

    code, payload, err = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    reported = json.dumps(payload["findings"]) + err
    assert "`" not in reported and "|" not in reported
    assert r"x\x60id\x60\x7cy" in reported.replace("\\\\", "\\")


def test_sensitive_changes_name_what_a_later_session_runs_or_loads(
    world: World,
) -> None:
    world.upstream_commits(
        {
            "scripts/tool.sh": "#!/bin/sh\necho tool\n",
            ".claude/hooks/new.py": "print(1)\n",
            ".mcp.json": "{}\n",
            "docs/CLAUDE.md": "# Notes\n",
            "docs/plain.md": "plain\n",
            "AGENTS.md": "# Contract\n",
        }
    )

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert payload["sensitive_changes"] == [
        ".claude/hooks/new.py",
        ".mcp.json",
        "AGENTS.md",
        "docs/CLAUDE.md",
        "scripts/tool.sh",
    ]


# --- the merged tree is scanned as a whole ---


@pytest.mark.parametrize(
    "rel",
    [".GitHub/workflows/evil.yml", "docs/Fork-Only.md", "scripts/Rename-Roles.py"],
)
def test_name_that_collides_only_with_the_forks_own_paths_is_refused(
    world: World, tmp_path: Path, rel: str
) -> None:
    """Upstream's tree is clean by itself and so is the fork's: the two names
    meet only in the merge. On the owner's disk they are one file."""
    upstream_adds_entry(world, "100644", rel, "theirs\n")
    directory = tmp_path / "resolve"

    code, payload, _ = world.intake("--resolve-in", directory)

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unsupported-entry")
    assert "merged tree" in payload["detail"]
    assert any("case-insensitive" in entry for entry in payload["entries"])
    assert world.intake_branches() == []
    assert not directory.exists()


def test_colliding_name_is_refused_before_a_conflicted_tree_is_left(
    world: World, tmp_path: Path
) -> None:
    world.upstream_commits(
        {GUIDE: UPSTREAM_FILES[GUIDE].replace("line two", "line two, upstream's way")}
    )
    upstream_adds_entry(world, "100644", "docs/Fork-Only.md", "theirs\n")
    directory = tmp_path / "resolve"

    code, payload, _ = world.intake("--resolve-in", directory)

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unsupported-entry")
    assert not directory.exists()


def test_merge_base_is_scanned_like_upstreams_tip(world: World) -> None:
    """The base is checked out and transformed as well. Here it holds an
    attributes file that upstream has since removed."""
    world.upstream_commits({".gitattributes": "* -text\n"})
    git(world.fork, "merge", "--quiet", "-s", "ours", "--no-edit", world.upstream_rev)
    world.upstream_commits({".gitattributes": None, "docs/new.md": "new\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unsupported-entry")
    assert "the merge base" in payload["detail"]


@pytest.mark.parametrize(
    ("rel", "reason"),
    [
        ("docs/.gitattributes", "an attributes file"),
        ("docs/two words.md", "a character outside"),
        ("docs/link.md", "mode 120000"),
    ],
)
def test_seal_refuses_an_entry_no_checkout_may_hold(
    world: World, tmp_path: Path, rel: str, reason: str
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    if reason.startswith("mode"):
        (directory / rel).symlink_to("/etc/passwd")
    else:
        write(directory, rel, "* -text\n")

    code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unsupported-entry")
    assert any(reason in entry for entry in payload["entries"])
    assert world.intake_branches() == []


# --- install lines: a protection, not a consistency check ---

EVIL_URL = "https://evil.example/x.git"
FORK_DEFAULT = fork_invariants.DEFAULT_REPO_LINE
UPDATER_VARIANTS = {
    "the fork's URL only in a comment": (
        f'TEMPLATE_REPO="${{ORCHESTRA_TEMPLATE_REPO:-{EVIL_URL}}}" # was {common.FORK_GIT_URL}\n'
    ),
    "a second, indented assignment": (
        f'{FORK_DEFAULT}\nif true; then\n    TEMPLATE_REPO="{EVIL_URL}"\nfi\n'
    ),
    "an exported second assignment": f'{FORK_DEFAULT}\nexport TEMPLATE_REPO="{EVIL_URL}"\n',
    "an appended value": f'{FORK_DEFAULT}\nTEMPLATE_REPO+="/../evil.git"\n',
    "the default line, indented": f"  {FORK_DEFAULT}\n",
    "a trailing command": f"{FORK_DEFAULT}; TEMPLATE_REPO={EVIL_URL}\n",
    "no assignment at all": 'echo "$TEMPLATE_REPO"\n',
}


@pytest.mark.parametrize("variant", sorted(UPDATER_VARIANTS))
def test_updater_default_must_be_the_forks_line_and_the_only_one(
    world: World, variant: str
) -> None:
    write(
        world.fork,
        "scripts/update.sh",
        "#!/usr/bin/env bash\n" + UPDATER_VARIANTS[variant],
    )

    code, payload, _ = world.run("check")

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "violated")
    assert {f["invariant"] for f in payload["findings"]} == {"install-urls"}


@pytest.mark.parametrize(
    "line",
    [
        f"curl -fsSL https://raw.githubusercontent.com/{fork_invariants.UPSTREAM_SLUG}"
        "/main/scripts/install.sh | bash",
        f"wget -qO- https://github.com/{fork_invariants.UPSTREAM_SLUG}/raw/main/x.sh | sh",
        f"bash <(curl https://raw.githubusercontent.com/{fork_invariants.UPSTREAM_SLUG}/main/i.sh)",
    ],
)
def test_readme_installer_fetched_from_upstream_is_a_finding(
    world: World, line: str
) -> None:
    readme = (world.fork / "README.md").read_text(encoding="utf-8")
    write(world.fork, "README.md", f"{readme}\n    {line}\n")

    code, payload, _ = world.run("check")

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "violated")
    assert {f["invariant"] for f in payload["findings"]} == {"install-urls"}


def test_upstream_cannot_merge_another_default_repository_in(world: World) -> None:
    """Through a whole intake: upstream rewrites the default and leaves the
    fork's URL in a comment. The merge is clean; the invariant is not."""
    updater = UPSTREAM_FILES["scripts/update.sh"].replace(
        f'"${{ORCHESTRA_TEMPLATE_REPO:-{fork_invariants.UPSTREAM_GIT_URL}}}"',
        f'"${{ORCHESTRA_TEMPLATE_REPO:-{EVIL_URL}}}" # was {common.FORK_GIT_URL}',
    )
    world.upstream_commits({"scripts/update.sh": updater})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert {f["invariant"] for f in payload["findings"]} == {"install-urls"}
    assert world.intake_branches() == []


# --- the manifest is the owner's ---


def test_upstream_removing_an_agent_needs_the_owner_not_a_tool(world: World) -> None:
    """The manifest names an agent the tree no longer has. Rendering the
    manifest again cannot clear that, and the manifest is not this tool's to
    edit: the finding says so by its name."""
    world.upstream_commits({f"{AGENTS}/{OLD_DEEP}.md": None})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert {f["invariant"] for f in payload["findings"]} == {"manifest-needs-owner"}
    assert all(f["detail"].startswith("refused:") for f in payload["findings"])


def test_upstream_adding_an_agent_needs_the_owner_not_a_tool(world: World) -> None:
    agent = "---\nname: extra\ndescription: new\nmodel: inherit\n---\n\nBody.\n"
    world.upstream_commits({f"{AGENTS}/extra.md": agent})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert "manifest-needs-owner" in {f["invariant"] for f in payload["findings"]}


def test_manifest_override_file_cannot_arrive_with_upstream(world: World) -> None:
    override = json.dumps({"schema_version": 1, "manage_native_settings": False})
    world.upstream_commits({upstream_intake.MANIFEST_OVERRIDE: override + "\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert "fork-owned-untouched" in {f["invariant"] for f in payload["findings"]}
    assert world.intake_branches() == []


def test_seal_refuses_a_manifest_override_written_by_the_resolver(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    write(directory, upstream_intake.MANIFEST_OVERRIDE, '{"schema_version": 1}\n')

    code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "invariants-failed")
    assert "fork-owned-untouched" in {f["invariant"] for f in payload["findings"]}


# --- seal believes nothing the directory says about itself ---


def test_seal_reports_every_path_edited_outside_the_conflicts(
    world: World, tmp_path: Path
) -> None:
    """Edits beside the conflicts are allowed (an invariant fix needs them),
    and each one is named, with the ones a later session would run."""
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    write(directory, "docs/keep.md", "rewritten by the resolver\n")
    write(directory, "scripts/added.sh", "#!/bin/sh\necho added\n")

    code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert payload["edited_by_hand"] == [GUIDE, "docs/keep.md", "scripts/added.sh"]
    assert payload["sensitive_changes"] == ["scripts/added.sh"]
    message = git(world.fork, "show", "-s", "--format=%B", payload["commit"])
    assert "Paths that differ from the automatic merge: 3" in message


def test_rewritten_record_cannot_hide_a_hand_edit_or_a_conflict(
    world: World, tmp_path: Path
) -> None:
    """The record in the directory is the resolver's to rewrite. It says the
    finished tree is the automatic one and that nothing conflicted; `seal`
    builds the automatic merge again and is not told otherwise."""
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    write(directory, "scripts/added.sh", "#!/bin/sh\necho added\n")
    git(directory, "add", "--all")
    record = directory / ".git" / upstream_intake.RECORD_FILE
    state = json.loads(record.read_text(encoding="utf-8"))
    state.update({"auto_tree": git(directory, "write-tree"), "conflicts": []})
    record.write_text(json.dumps(state), encoding="utf-8")

    unanswered, refused, _ = world.seal(directory, {})
    code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert (unanswered, refused["result"]) == (EXIT_NEEDS_OWNER, "unresolved")
    assert refused["unanswered"] == [GUIDE]
    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert payload["edited_by_hand"] == [GUIDE, "scripts/added.sh"]
    assert payload["sensitive_changes"] == ["scripts/added.sh"]


@pytest.mark.parametrize(
    "left",
    ["<<<<<<< fork-main", "||||||| merge-base", "=======", ">>>>>>> upstream-main"],
)
def test_one_marker_line_left_behind_is_enough_to_refuse(
    world: World, tmp_path: Path, left: str
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, f"# Guide\n\nline one\n{left}\nline three\n")

    code, payload, _ = world.seal(directory, {GUIDE: "kept both"})

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "unresolved")
    assert payload["unanswered"] == [GUIDE]


def test_marker_like_text_in_a_file_that_did_not_conflict_is_not_judged(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    write(directory, "docs/keep.md", "Heading\n=======\n\nkept\n")

    code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert (code, payload["result"]) == (EXIT_OK, "merged")


def test_seal_names_the_ignored_files_it_leaves_out(
    world: World, tmp_path: Path
) -> None:
    directory, _ = conflicted(world, tmp_path)
    write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
    write(directory, "__pycache__/resolution.md", "written to an ignored path\n")

    code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert (code, payload["result"]) == (EXIT_OK, "merged")
    assert payload["ignored_files_left_out"] == ["__pycache__/resolution.md"]
    assert "__pycache__/resolution.md" not in world.paths(payload["commit"])


def test_seal_refuses_a_directory_intake_did_not_prepare(
    world: World, tmp_path: Path
) -> None:
    world.upstream_commits({"docs/new.md": "new\n"})
    stranger = tmp_path / "stranger"
    git(tmp_path, "init", "--quiet", str(stranger))

    code, payload, _ = world.seal(stranger, {})

    assert (code, payload["result"]) == (EXIT_USAGE, "refused")
    assert world.intake_branches() == []


# --- outcomes that had no test ---


def test_existing_branch_at_another_commit_is_left_as_it_is(world: World) -> None:
    upstream = world.upstream_commits({"docs/new.md": "new\n"})
    branch = f"bot/upstream-{upstream[:12]}"
    git(world.fork, "branch", branch, world.fork_rev)

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "branch-exists")
    assert git(world.fork, "rev-parse", branch) == world.fork_rev
    assert payload["branch"] == branch
    assert payload["upstream"] == upstream
    assert payload["base"] == git(world.fork, "merge-base", world.fork_rev, upstream)
    assert payload["upstream_commits"] == 1


def test_unrelated_history_has_no_merge_base(world: World) -> None:
    git(world.upstream, "checkout", "--quiet", "--orphan", "rewritten")
    world.upstream_commits({"docs/new.md": "new\n"})
    git(world.fork, "fetch", "--quiet", str(world.upstream), "rewritten")

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "no-single-merge-base")
    assert payload["detail"].startswith("0 merge bases")
    assert world.intake_branches() == []


def test_criss_cross_history_has_two_merge_bases(world: World) -> None:
    """Each side merged the other's earlier commit. No single base exists,
    and none is invented."""
    theirs = world.upstream_commits({"docs/theirs.md": "theirs\n"})
    ours = world.fork_commits({"docs/ours.md": "ours\n"})
    git(world.fork, "merge", "--quiet", "--no-ff", "--no-edit", theirs)
    git(world.upstream, "fetch", "--quiet", str(world.fork), ours)
    git(world.upstream, "merge", "--quiet", "--no-ff", "--no-edit", ours)
    git(world.fork, "fetch", "--quiet", str(world.upstream), "main")
    bases = git(world.fork, "merge-base", "--all", world.fork_rev, world.upstream_rev)
    assert sorted(bases.split()) == sorted([ours, theirs])

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "no-single-merge-base")
    assert payload["detail"].startswith("2 merge bases")
    assert world.intake_branches() == []


def test_tree_with_both_seed_layouts_is_not_transformed(world: World) -> None:
    world.upstream_commits({SEED_STATE: "# Agent State\n\nupstream's own scaffold\n"})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "transform-refused")
    assert "a layout the fork has no rule for" in payload["detail"]
    assert world.intake_branches() == []


def test_file_the_codemod_refuses_stops_the_transformation(world: World) -> None:
    """A file that is not text could hide an old role name from the rename;
    the codemod refuses the tree, and nothing is merged on a guess."""
    (world.upstream / "docs" / "blob.bin").write_bytes(b"\x00\x01binary")
    world.upstream_commits({})

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_NEEDS_OWNER, "transform-refused")
    assert "docs/blob.bin" in payload["detail"]
    assert world.intake_branches() == []


def test_checker_that_fails_is_a_failure_not_a_finding(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 is the lint's finding; exit 1 is the lint not working. The
    second must not be reported as something for the owner to decide."""
    real = fork_invariants.run_tool

    def broken_lint(
        target: fork_invariants.Target, rel: str, *args: str
    ) -> common.CommandResult:
        result = real(target, rel, *args)
        if rel != fork_invariants.LINT_TOOL:
            return result
        return common.CommandResult(result.argv, 1, "", "error: the lint broke\n")

    monkeypatch.setattr(fork_invariants, "run_tool", broken_lint)
    world.upstream_commits({"docs/new.md": "new\n"})

    checked, check_payload, _ = world.run("check")
    code, payload, _ = world.intake()

    assert (checked, check_payload["result"]) == (EXIT_FAILED, "tool-failed")
    assert (code, payload["result"]) == (EXIT_FAILED, "tool-failed")
    assert world.intake_branches() == []


def test_git_failure_while_publishing_is_exit_three(world: World) -> None:
    """A ref below the branch's name makes git refuse to create the branch."""
    upstream = world.upstream_commits({"docs/new.md": "new\n"})
    blocker = f"refs/heads/bot/upstream-{upstream[:12]}/in-the-way"
    git(world.fork, "update-ref", blocker, world.fork_rev)

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_FAILED, "git-failed")
    assert world.intake_branches() == [blocker.removeprefix("refs/heads/")]


def test_branch_that_is_not_the_merge_is_not_kept(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit is read back from the repository that will be pushed from.
    Here what arrives there is the fork's own commit, not the merge."""
    world.upstream_commits({"docs/new.md": "new\n"})
    fork = world.fork_rev
    real = scratch_repo.Scratch.branch

    def wrong_commit(self: scratch_repo.Scratch, name: str, commit: str) -> None:
        real(self, name, fork if name == upstream_intake.SEALED else commit)

    monkeypatch.setattr(scratch_repo.Scratch, "branch", wrong_commit)

    code, payload, _ = world.intake()

    assert (code, payload["result"]) == (EXIT_FAILED, "git-failed")
    assert world.intake_branches() == []


def test_existing_resolve_directory_is_refused_and_left_alone(
    world: World, tmp_path: Path
) -> None:
    world.upstream_commits({"docs/new.md": "new\n"})
    directory = tmp_path / "resolve"
    write(directory, "mine.txt", "not the helper's to remove\n")

    code, payload, _ = world.intake("--resolve-in", directory)

    assert (code, payload["result"]) == (EXIT_USAGE, "refused")
    assert (directory / "mine.txt").read_text(encoding="utf-8").startswith("not the")
    assert world.intake_branches() == []


@pytest.mark.parametrize(
    ("changes", "result"),
    [
        ({"docs/new.md": "new\n"}, "merged"),
        ({SEED_STATE: "both layouts\n"}, "transform-refused"),
    ],
)
def test_resolve_directory_is_removed_when_nothing_is_left_to_resolve(
    world: World, tmp_path: Path, changes: dict[str, str], result: str
) -> None:
    world.upstream_commits(changes)
    directory = tmp_path / "resolve"

    _, payload, _ = world.intake("--resolve-in", directory)

    assert payload["result"] == result
    assert not directory.exists()


def test_check_of_a_revision_takes_only_a_full_sha(world: World) -> None:
    for name in ("main", "HEAD", world.fork_rev[:12]):
        code, payload, _ = world.run("check", "--rev", name)
        assert (code, payload["result"]) == (EXIT_USAGE, "refused"), name


def test_merge_tree_exit_code_and_conflict_list_must_agree(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 1 with no conflict named, or exit 0 with one, is git saying two
    things: neither is taken as a clean merge."""
    project = scratch_repo.Repo(world.fork, scratch_repo.child_env())
    scratch = scratch_repo.Scratch.create(tmp_path / "scratch", project)
    tree = git(world.fork, "rev-parse", "HEAD^{tree}")
    real = scratch_repo.Repo.run

    def says_conflict(self: scratch_repo.Repo, *args: str, **kwargs: Any) -> Any:
        if args[0] != "merge-tree":
            return real(self, *args, **kwargs)
        return common.CommandResult(("git",), 1, f"{tree}\0", "")

    monkeypatch.setattr(scratch_repo.Repo, "run", says_conflict)

    with pytest.raises(scratch_repo.Stop) as stopped:
        upstream_intake.merge(scratch, world.fork_rev, world.fork_rev)

    assert (stopped.value.result, stopped.value.exit_code) == (
        "git-failed",
        EXIT_FAILED,
    )


# --- nothing of the fork's repository is used or changed but one ref ---

HOOKS = (
    "post-checkout",
    "post-commit",
    "post-index-change",
    "post-merge",
    "pre-commit",
    "reference-transaction",
)
ISOLATION_CASES = {
    "merged": {"docs/new.md": "new\n"},
    "conflicts": {GUIDE: UPSTREAM_FILES[GUIDE].replace("line two", "upstream's line")},
    "invariants-failed": {"docs/models.md": f"runs on {MODEL_WORD}\n"},
    "unsupported-entry": {".gitattributes": "* -text\n"},
    "sealed": {GUIDE: UPSTREAM_FILES[GUIDE].replace("line two", "upstream's line")},
}


def plant_hooks(git_dir: Path, marker: Path) -> None:
    for name in HOOKS:
        hook = git_dir / "hooks" / name
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)


def snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("case", sorted(ISOLATION_CASES))
def test_helper_ignores_inherited_git_variables_hooks_and_leaves_config_alone(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Whatever the outcome: git variables aimed at another repository are
    not followed, no hook of the fork's repository runs, and its
    configuration, index and work tree are byte for byte what they were."""
    world.upstream_commits(ISOLATION_CASES[case])
    revisions = world.revisions()
    directory = tmp_path / "resolve"
    decoy = tmp_path / "decoy"
    git(tmp_path, "init", "--quiet", str(decoy))
    marker = tmp_path / "a-hook-ran"
    config = (world.fork / ".git" / "config").read_bytes()
    index = (world.fork / ".git" / "index").read_bytes()
    refs = git(world.fork, "for-each-ref")
    plant_hooks(world.fork / ".git", marker)
    plant_hooks(decoy / ".git", marker)
    decoy_before = snapshot(decoy)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "decoy-index"))

    code, payload, _ = world.run("intake", *revisions, "--resolve-in", directory)
    if case == "sealed":
        write(directory, GUIDE, UPSTREAM_FILES[GUIDE])
        plant_hooks(directory / ".git", marker)
        code, payload, _ = world.seal(directory, {GUIDE: "took upstream's"})

    assert payload["result"] == ("merged" if case == "sealed" else case), payload
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert snapshot(decoy) == decoy_before
    assert (world.fork / ".git" / "config").read_bytes() == config
    assert (world.fork / ".git" / "index").read_bytes() == index
    shutil.rmtree(world.fork / ".git" / "hooks")
    assert git(world.fork, "status", "--porcelain") == ""
    added = set(git(world.fork, "for-each-ref").split("\n")) - set(refs.split("\n"))
    expected = 1 if code == EXIT_OK else 0
    assert len(added) == expected, added


# What the scratch repository's own configuration must say, written out: a
# test that read the list from the code would pass with an entry removed.
SCRATCH_CONFIG = {
    "core.hooksPath": "/dev/null",
    "core.fsmonitor": "false",
    "core.autocrlf": "false",
    "core.attributesFile": "/dev/null",
    "core.excludesFile": "/dev/null",
    "submodule.recurse": "false",
    "gc.auto": "0",
    "maintenance.auto": "false",
    "protocol.allow": "never",
}


def test_scratch_repository_carries_its_own_configuration(
    world: World, tmp_path: Path
) -> None:
    """The fork's tools start git themselves in the scratch tree. What holds
    that git to the rules is the scratch repository's own configuration."""
    directory, _ = conflicted(world, tmp_path)

    assert dict(scratch_repo.SCRATCH_CONFIG) == SCRATCH_CONFIG
    for key, value in SCRATCH_CONFIG.items():
        assert git(directory, "config", "--local", "--get", key) == value, key
    attributes = (directory / ".git" / "info" / "attributes").read_text("utf-8")
    assert attributes == scratch_repo.SCRATCH_ATTRIBUTES
    assert "!merge" in attributes.split() and "-filter" in attributes.split()
    assert not (directory / ".git" / "hooks").exists()


def test_tool_that_starts_its_own_process_in_the_scratch_tree_runs_no_hook(
    world: World, tmp_path: Path
) -> None:
    """A tool's own call carries none of the helper's options. A hook that
    somehow sat in the scratch repository still must not run."""
    project = scratch_repo.Repo(world.fork, scratch_repo.child_env())
    scratch = scratch_repo.Scratch.create(tmp_path / "scratch", project)
    scratch.load(world.fork_rev)
    marker = tmp_path / "a-hook-ran"
    plant_hooks(scratch.repo.path / ".git", marker)
    write(scratch.repo.path, GUIDE, "changed by a tool\n")

    done = subprocess.run(
        ["git", "-C", str(scratch.repo.path), "add", "--update"],
        capture_output=True,
        text=True,
        env=scratch.repo.env,
    )

    assert done.returncode == 0, done.stderr
    assert not marker.exists()


# --- the job marker ---


def test_job_marker_is_defined_once_and_the_status_comment_uses_it() -> None:
    assert common.JOB_MARKER_PREFIX == "<!-- job:"
    assert report.MARKER == "<!-- job:local-maint -->"
    assert common.job_marker("issue-worker", "body-sha256:ab") == (
        "<!-- job:issue-worker body-sha256:ab -->"
    )


def test_issue_opened_by_the_local_job_starts_with_the_job_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issue worker refuses a body that starts with the marker. An issue
    a job opens under the owner's name quotes text the owner did not write."""
    sent: list[tuple[str, ...]] = []

    def record(self: gh.GitHub, *args: str) -> common.CommandResult:
        sent.append(args)
        return common.CommandResult(("gh",), 0, "", "")

    monkeypatch.setattr(gh.GitHub, "_repo", record)

    gh.GitHub({}, common.Log(None)).create_issue("Title", "Body text", "a-label")

    body = sent[0][sent[0].index("--body") + 1]
    assert body == "<!-- job:local-maint -->\nBody text"
    assert body.startswith(common.JOB_MARKER_PREFIX)


def test_seal_lives_in_its_own_module_and_is_reached_from_the_command_line() -> None:
    assert fork_invariants.COMMANDS["seal"] is fork_invariants._cmd_seal
    assert callable(intake_seal.cmd_seal)
