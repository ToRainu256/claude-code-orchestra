"""Pin the split between the seeds a project receives and this repository's state.

``scripts/scaffold/STATE.md`` and ``scripts/scaffold/DESIGN.md`` are what
``scripts/install.sh`` and ``scripts/update.sh`` hand to a project that has no
``.claude/STATE.md`` / ``.claude/docs/DESIGN.md`` of its own. They live apart
from the files at those two paths in this repository, which record its own
development and change whenever ``/init``, ``design-tracker`` or
``checkpointing`` runs here.

Three invariants follow, and each has to hold however far the live files drift:

1. the seeds stay pristine — an uninitialised state and a placeholder design;
2. the live files still satisfy the contracts their writers depend on;
3. the installer and the updater read the seeds, never the live files.

The third is proved end to end in ``tests/test_install_script.py``, which
installs and updates from a template whose live files differ from its seeds.
Here it is pinned at the source, so that a list edited back to the old paths
fails without needing a scratch installation to notice.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD_REL = "scripts/scaffold"
SCAFFOLD = REPO_ROOT / SCAFFOLD_REL
SEED_STATE = SCAFFOLD / "STATE.md"
SEED_DESIGN = SCAFFOLD / "DESIGN.md"
STATE_REL = ".claude/STATE.md"
DESIGN_REL = ".claude/docs/DESIGN.md"
# "<destination in a project>": the seed that is copied there.
SEEDS = {STATE_REL: SEED_STATE, DESIGN_REL: SEED_DESIGN}
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"
UPDATE_SCRIPT = REPO_ROOT / "scripts" / "update.sh"
VALIDATE_DOC = REPO_ROOT / ".claude" / "skills" / "_shared" / "validate_doc.py"
LOAD_CONTEXT = REPO_ROOT / ".claude" / "skills" / "context-loader" / "load_context.py"
UNINITIALIZED_MARKER = "_Not initialized yet. Run `/init` to populate._"
SEED_STATE_SECTIONS = ["Main Agent", "Repository Identity", "Progress Tracker"]
# The exact bytes every new installation receives. The structural tests below
# cannot tell a pristine seed from one that gained template-specific prose, so
# the bytes are pinned. The only legitimate reason to change a digest is a
# deliberate edit of that seed (for example, taking an upstream change to it);
# never update one to make an unrelated change pass.
SEED_SHA256 = {
    SEED_STATE: "e799bf9f88a271558ad65dca86ede6581dd36b9189ce68725fc120b8fd27eaee",
    SEED_DESIGN: "c9413c9dffe295745f0e5f205c7a3f87519ebee2607c7cd436e8de586541199a",
}


def run_script(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def shell_array(script: Path, name: str) -> list[str]:
    """The quoted entries of a top-level ``NAME=( ... )`` array in *script*."""
    text = script.read_text(encoding="utf-8")
    blocks = re.findall(rf"(?m)^{re.escape(name)}=\(\n(.*?)^\)$", text, flags=re.S)
    assert len(blocks) == 1, f"{name} is not declared exactly once in {script.name}"
    return re.findall(r'"([^"\n]*)"', blocks[0])


def function_body(script: Path, name: str) -> str:
    """The body of a top-level ``name() { ... }`` shell function in *script*."""
    text = script.read_text(encoding="utf-8")
    bodies = re.findall(
        rf"(?m)^{re.escape(name)}\(\) \{{\n(.*?)^\}}$", text, flags=re.S
    )
    assert len(bodies) == 1, f"{name} is not defined exactly once in {script.name}"
    return bodies[0]


@pytest.fixture
def seeded_project(tmp_path: Path) -> Path:
    """A project holding what a fresh install seeds, at the same paths.

    `load_context.py` also requires `.claude/rules/` to exist, so one rule
    stands in for the template-owned part of an installation.
    """
    for destination, seed in SEEDS.items():
        path = tmp_path / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(seed.read_bytes())
    rules_dir = tmp_path / ".claude" / "rules"
    rules_dir.mkdir()
    (rules_dir / "coding-principles.md").write_text("# rule\n", encoding="utf-8")
    return tmp_path


# --- 1. the seeds stay pristine ---


def test_scaffold_holds_exactly_the_two_seeds_as_real_files() -> None:
    """Anything else placed here would look like a seed and ship nowhere, and a
    link back to the live file would ship that file's content after all.

    Git decides what is in the directory: a file it ignores (OS metadata such
    as `.DS_Store`) never reaches a commit, a clone or an installation, so it
    must not fail the test. Tracked files and untracked files git would add
    both count, so a stray seed candidate still fails."""
    listed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            SCAFFOLD_REL,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert sorted(listed) == [f"{SCAFFOLD_REL}/DESIGN.md", f"{SCAFFOLD_REL}/STATE.md"]
    for seed in SEEDS.values():
        assert seed.is_file()
        assert not seed.is_symlink()


@pytest.mark.parametrize("seed", [SEED_STATE, SEED_DESIGN], ids=["STATE", "DESIGN"])
def test_seed_bytes_match_their_pinned_digest(seed: Path) -> None:
    """Every installation receives these exact bytes. A digest changes only
    with a deliberate edit of the seed; see ``SEED_SHA256``."""
    digest = hashlib.sha256(seed.read_bytes()).hexdigest()

    assert digest == SEED_SHA256[seed], (
        f"{seed.relative_to(REPO_ROOT)} changed. If the edit is deliberate, "
        "update SEED_SHA256; otherwise restore the seed."
    )


@pytest.mark.parametrize(
    ("contract", "seed"),
    [("state-doc", SEED_STATE), ("design-doc", SEED_DESIGN)],
)
def test_seed_satisfies_its_document_contract(contract: str, seed: Path) -> None:
    """A project's first `/init` writes into the seed, so a seed its own
    contract rejects would fail every new installation at that step."""
    result = run_script(VALIDATE_DOC, "--contract", contract, "--file", str(seed))

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["sections_missing"] == []


def test_seed_state_is_an_uninitialised_state() -> None:
    text = SEED_STATE.read_text(encoding="utf-8")

    assert text.splitlines()[0] == "# Agent State"
    assert re.findall(r"(?m)^## (.+)$", text) == SEED_STATE_SECTIONS
    assert text.count(UNINITIALIZED_MARKER) == 1
    # A working block is a session's record. None belongs in a seed.
    assert not re.search(r"(?m)^## Current ", text)


def test_seed_state_names_the_default_main_agent(seeded_project: Path) -> None:
    result = run_script(LOAD_CONTEXT, "--project-root", str(seeded_project))

    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)["state"]
    assert state["present"] is True
    assert state["main_agent"] == "Claude Code"


def test_seed_design_is_reported_as_the_init_placeholder(seeded_project: Path) -> None:
    """`design.placeholder` is what steers a new project to `/init`. A seed
    that carried real prose would be taken for an initialised design."""
    result = run_script(LOAD_CONTEXT, "--project-root", str(seeded_project))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["design"]["present"] is True
    assert payload["design"]["placeholder"] is True
    assert any("/init" in warning for warning in payload["warnings"])


# --- 2. the live files still satisfy their contracts ---


@pytest.mark.parametrize(
    ("contract", "rel_path"),
    [("state-doc", STATE_REL), ("design-doc", DESIGN_REL)],
)
def test_live_document_still_satisfies_its_contract(
    contract: str, rel_path: str
) -> None:
    """The live files are free to diverge from the seeds, not from the shape
    `append_state_block.py`, `refresh_guard.py` and `update_design.py` edit."""
    live = REPO_ROOT / rel_path
    assert live.is_file()
    assert not live.is_symlink()

    result = run_script(VALIDATE_DOC, "--contract", contract, "--file", str(live))

    assert result.returncode == 0, result.stdout + result.stderr


# --- 3. the installer and the updater read the seeds ---


def test_installer_maps_each_seeded_destination_to_its_scaffold_seed() -> None:
    entries = dict(
        entry.split(":", 1)
        for entry in shell_array(INSTALL_SCRIPT, "PROJECT_FILES_IF_MISSING")
    )

    assert entries[STATE_REL] == f"{SCAFFOLD_REL}/STATE.md"
    assert entries[DESIGN_REL] == f"{SCAFFOLD_REL}/DESIGN.md"
    # No entry may read this repository's own state, under any destination.
    assert not {STATE_REL, DESIGN_REL} & set(entries.values())
    for seed in entries.values():
        assert (REPO_ROOT / seed).is_file(), seed


def test_updater_seeds_both_documents_through_the_scaffold_lookup() -> None:
    """`seed_project_file_if_missing` is the only place the updater may copy
    either document from. What that lookup does with and without a scaffold
    directory is exercised in `tests/test_install_script.py`."""
    migrate = function_body(UPDATE_SCRIPT, "migrate_legacy_agent_state")
    assert f'seed_project_file_if_missing "{STATE_REL}" "STATE.md"' in migrate
    assert f'seed_project_file_if_missing "{DESIGN_REL}" "DESIGN.md"' in migrate

    copies_from_template = [
        line.strip()
        for line in UPDATE_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("cp ") and "${TEMPLATE_DIR}/.claude/" in line
    ]
    assert copies_from_template == []


@pytest.mark.parametrize(
    ("script", "array"),
    [
        (INSTALL_SCRIPT, "TEMPLATE_OWNED_DIRS"),
        (INSTALL_SCRIPT, "TEMPLATE_OWNED_FILES"),
        (UPDATE_SCRIPT, "SAFE_DIRS"),
        (UPDATE_SCRIPT, "SAFE_FILES"),
    ],
)
def test_no_distribution_list_ships_the_scaffold_or_the_live_documents(
    script: Path, array: str
) -> None:
    """The seeds reach a project only as the two files they seed, and the live
    documents not at all: a wholesale-copied path that covered either would
    overwrite a project's own state on every update."""
    for entry in shell_array(script, array):
        assert entry not in {STATE_REL, DESIGN_REL, ".claude", ".claude/docs"}
        assert not entry.startswith("scripts/scaffold")
        assert entry != "scripts"
