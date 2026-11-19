"""Tests for ``models_lint.py`` and for the refusal of a retired agent name.

The lint is the gate that keeps a model named in the manifest and nowhere
else. A gate like that is only kept if it is quiet on legitimate text and
cannot be walked around, so the cases below pin both directions: every rule
fires on what it is for; none fires on tier IDs, role names, generated lines
or reasoned exemptions; and the ways to hide text from it (a new top-level
file, a symlink, a NUL byte, a second encoding, one marker for a whole file)
are each a finding.

Every case runs against a scratch git repository under ``tmp_path``, because
the lint scans what git lists. The CLI cases run under every interpreter in
``INTERPRETERS``: ``scripts/check.sh`` invokes the tool with whatever
``python3`` is first on ``PATH``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / ".claude" / "skills" / "_shared"
LINT_SCRIPT = SHARED_DIR / "models_lint.py"
MODELS_SCRIPT = SHARED_DIR / "models.py"
MANIFEST_REL = ".claude/skills/_shared/models.json"
LOCAL_REL = ".claude/models.local.json"
SYSTEM_PYTHON = Path("/usr/bin/python3")
OLDEST_SUPPORTED = (3, 9)
EXIT_BAD_ARGS = 1
EXIT_FINDINGS = 2
EXIT_REFUSED = 2
EXIT_FAILED = 3

RULES_FILE = ".claude/rules/routing.md"
CLEAN_PROSE = (
    "# Routing\n"
    "\n"
    "Routine work goes to `general-purpose-standard`; research goes to\n"
    "`general-purpose-deep`. Escalate to `fable-advisor` (Tier 3 `fable`), and\n"
    "to Codex (Tier 2 `sol`) for planning. A Fable review is saved under\n"
    "reviews/. The model each role runs on is set in the manifest.\n"
)
# One offending line per text rule, for the exemption cases.
OFFENDING = {
    "model-id": "Planning uses gpt-5.6-sol today.",
    "versioned-name": "Redesigned for Opus 4.6.",
    "family-word": "Research runs on Sonnet subagents.",
    "legacy-role": "Delegate to general-purpose-opus.",
}

MANIFEST: dict[str, Any] = {
    "schema_version": 1,
    "claude": {
        "main": "fable",
        "subagent_default": "opus",
        "agents": {
            "general-purpose-standard": "opus",
            "general-purpose-deep": "fable",
        },
    },
    "codex": {"model": "gpt-9.1-test", "reasoning_effort": "high"},
    "superseded": {
        "claude.main": [],
        "claude.subagent_default": ["sonnet"],
        "codex.model": ["gpt-8-old"],
        "codex.reasoning_effort": ["medium"],
    },
}
SETTINGS: dict[str, Any] = {
    "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "opus"},
    "model": "fable",
}
CONFIG = 'model = "gpt-9.1-test"\nmodel_reasoning_effort = "high"\n'


def _version_of(interpreter: Path) -> tuple[int, int] | None:
    if not interpreter.exists():
        return None
    result = subprocess.run(
        [str(interpreter), "-c", "import sys; print(*sys.version_info[:2])"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    major, minor = result.stdout.split()
    return int(major), int(minor)


def _interpreters() -> list[str]:
    """The test interpreter, plus the system one when it is a different version."""
    found = [sys.executable]
    version = _version_of(SYSTEM_PYTHON)
    if version and version >= OLDEST_SUPPORTED and version != sys.version_info[:2]:
        found.append(str(SYSTEM_PYTHON))
    return found


INTERPRETERS = _interpreters()
Runner = Callable[..., subprocess.CompletedProcess[str]]


def _runner(interpreter: str, script: Path) -> Runner:
    def _run(
        root: Path, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [interpreter, str(script), "--project-root", str(root), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    return _run


@pytest.fixture(params=INTERPRETERS, ids=lambda path: Path(path).name)
def lint(request: pytest.FixtureRequest) -> Runner:
    """Invoke the lint under one of the supported interpreters."""
    return _runner(request.param, LINT_SCRIPT)


@pytest.fixture(params=INTERPRETERS, ids=lambda path: Path(path).name)
def models_cli(request: pytest.FixtureRequest) -> Runner:
    """Invoke ``models.py`` under one of the supported interpreters."""
    return _runner(request.param, MODELS_SCRIPT)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def canonical(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def agent_text(name: str, model: str, body: str = "Do the assigned work.\n") -> str:
    return (
        f"---\nname: {name}\n"
        f'description: "The {name} role"\n'
        f"model: {model}\n---\n\n{body}"
    )


def make_tree(root: Path) -> Path:
    """A scratch repository whose text names no model. Nothing is committed:
    the lint scans untracked files too."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    write(root, "AGENTS.md", CLEAN_PROSE)
    write(root, RULES_FILE, CLEAN_PROSE)
    write(root, MANIFEST_REL, canonical(MANIFEST))
    for name, model in MANIFEST["claude"]["agents"].items():
        write(root, f".claude/agents/{name}.md", agent_text(name, model))
    write(root, ".claude/settings.json", canonical(SETTINGS))
    write(root, ".codex/config.toml", CONFIG)
    return root


def payload_of(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    # json.loads rejects trailing data: stdout holds exactly one JSON object.
    return json.loads(result.stdout)


def findings_of(result: subprocess.CompletedProcess[str]) -> list[tuple[Any, ...]]:
    return [
        (item["file"], item["line"], item["rule"], item["text"])
        for item in payload_of(result)["findings"]
    ]


def rules_at(result: subprocess.CompletedProcess[str], rel: str) -> set[str]:
    return {rule for file, _line, rule, _text in findings_of(result) if file == rel}


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and ".git" not in path.parts
    }


# --- a clean tree ---------------------------------------------------------------


def test_lint_passes_a_tree_that_names_roles_and_tiers_only(
    tmp_path: Path, lint: Runner
) -> None:
    make_tree(tmp_path)
    before = snapshot(tmp_path)

    result = lint(tmp_path)

    assert result.returncode == 0, result.stderr
    payload = payload_of(result)
    assert payload["ok"] is True and payload["result"] == "clean"
    assert payload["findings"] == []
    assert payload["allowed"] == []
    assert payload["files_scanned"] == 7
    assert result.stderr == ""
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "line",
    [
        "Escalate to `fable-advisor`; the tier ID is `fable`.",
        "Fable's judgment is not self-certifying.",
        "Tier 2 -- `sol` (Long-Duration Executor)",
        "subagent_type='general-purpose-deep'",
        "The my-opus-router and opus_mode identifiers are not model names.",
        "An octopus wrote sonnets about gpt-like tools and claude-code.",
        "See .claude/skills/_shared/models.json for the model.",
        "Clone claude-code-orchestra; read .claude/agents/fable-advisor.md.",
        "The file claude-sonnets.md collects poems.",
        # A longer identifier that only contains a retired role name is not
        # that role: the codemod would not rewrite it, so the gate must not
        # ask for it.
        "Keep general-purpose-opus-plus and my-general-purpose-sonnet as they are.",
    ],
)
def test_lint_is_quiet_on_legitimate_text(
    tmp_path: Path, lint: Runner, line: str
) -> None:
    make_tree(tmp_path)
    write(tmp_path, RULES_FILE, CLEAN_PROSE + line + "\n")

    result = lint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert payload_of(result)["findings"] == []


# --- each rule fires ------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "rule", "text"),
    [
        ("Planning uses gpt-5.6-sol today.", "model-id", "gpt-5.6-sol"),
        ("/codex:rescue --model gpt-5.5-mini now", "model-id", "gpt-5.5-mini"),
        ('model: str = "gpt-4",', "model-id", "gpt-4"),
        ("Pinned to claude-opus-4-6 here.", "model-id", "claude-opus-4-6"),
        ("It answered as claude-fable-5-2.", "model-id", "claude-fable-5-2"),
        # The family after the version, and an alias with no version at all.
        ("Pinned to claude-3-opus.", "model-id", "claude-3-opus"),
        ("Use claude-3-5-haiku-latest.", "model-id", "claude-3-5-haiku-latest"),
        ("Or claude-opus-latest instead.", "model-id", "claude-opus-latest"),
        ("Redesigned for Opus 4.6.", "versioned-name", "Opus 4.6"),
        ("Handled by Claude (Opus 4.7+).", "versioned-name", "Opus 4.7+"),
        ("Needs Sonnet-5 or later.", "versioned-name", "Sonnet-5"),
        ("Only GPT-5 can do it.", "versioned-name", "GPT-5"),
        ("Fable 5.2 is the main model.", "versioned-name", "Fable 5.2"),
        ("Researcher (Opus 1M context)", "versioned-name", "Opus 1"),
        ("Tuned for opus 4.6 only.", "versioned-name", "opus 4.6"),
        ("Fable  5.2 with two spaces.", "versioned-name", "Fable  5.2"),
        ("Opus\t4.6 with a tab.", "versioned-name", "Opus\t4.6"),
        ("Research runs on Opus subagents.", "family-word", "Opus"),
        ("Mechanical work stays on Sonnet.", "family-word", "Sonnet"),
        ("pass `--model sonnet` to pin it", "family-word", "sonnet"),
        ('"model": "haiku"', "family-word", "haiku"),
        ("Set the SONNET tier here.", "family-word", "SONNET"),
        ("Mythos is next.", "family-word", "Mythos"),
        # A suffix hyphenated onto the word does not hide it.
        ("An Opus-class model does this.", "family-word", "Opus"),
        ("Give it to the haiku-tier subagent.", "family-word", "haiku"),
        # No spaces around the name: the text beside it is not ASCII.
        ("モデルはgpt-5.6-solです", "model-id", "gpt-5.6-sol"),
        ("モデルはOpus 4.6です", "versioned-name", "Opus 4.6"),
        ("モデルはsonnetです", "family-word", "sonnet"),
        (
            "Delegate to general-purpose-sonnet.",
            "legacy-role",
            "general-purpose-sonnet",
        ),
        (
            "subagent_type='general-purpose-opus'",
            "legacy-role",
            "general-purpose-opus",
        ),
        # Spelled another way, it is still the retired name.
        ("Ask General-Purpose-Opus first.", "legacy-role", "General-Purpose-Opus"),
        ("ROLE = general_purpose_sonnet", "legacy-role", "general_purpose_sonnet"),
    ],
)
def test_each_rule_reports_file_line_rule_and_text(
    tmp_path: Path, lint: Runner, line: str, rule: str, text: str
) -> None:
    make_tree(tmp_path)
    write(tmp_path, RULES_FILE, CLEAN_PROSE + line + "\n")
    line_number = CLEAN_PROSE.count("\n") + 1

    result = lint(tmp_path)

    assert result.returncode == EXIT_FINDINGS, result.stdout
    # Exactly one finding: a versioned name is not also a family word, and a
    # retired role name is not also the family word inside it.
    assert findings_of(result) == [(RULES_FILE, line_number, rule, text)]
    payload = payload_of(result)
    assert payload["ok"] is False and payload["result"] == "findings"
    assert f"lint: {RULES_FILE}:{line_number}: {rule}: " in result.stderr
    assert "hint: name the role, not the model" in result.stderr
    # The failure message does not teach the way around the gate.
    assert "models-lint" not in result.stderr


def test_every_occurrence_on_a_line_is_reported(tmp_path: Path, lint: Runner) -> None:
    make_tree(tmp_path)
    write(tmp_path, RULES_FILE, "Sonnet first, then Opus 4.6, then gpt-5.5.\n")

    result = lint(tmp_path)

    assert sorted(findings_of(result)) == [
        (RULES_FILE, 1, "family-word", "Sonnet"),
        (RULES_FILE, 1, "model-id", "gpt-5.5"),
        (RULES_FILE, 1, "versioned-name", "Opus 4.6"),
    ]


# --- what is scanned: the tree, not a list ---------------------------------------


@pytest.mark.parametrize(
    "rel", ["MODELS.md", "docs/models.md", "NOTES", "examples/demo/run.sh"]
)
def test_a_new_file_anywhere_is_scanned_without_editing_a_list(
    tmp_path: Path, lint: Runner, rel: str
) -> None:
    make_tree(tmp_path)
    write(tmp_path, rel, "Built for Opus 4.6.\n")

    result = lint(tmp_path)

    assert result.returncode == EXIT_FINDINGS
    assert findings_of(result) == [(rel, 1, "versioned-name", "Opus 4.6")]


def test_tracked_and_untracked_files_are_both_scanned(
    tmp_path: Path, lint: Runner
) -> None:
    make_tree(tmp_path)
    write(tmp_path, "tracked.md", "Runs on Opus.\n")
    git(tmp_path, "add", "tracked.md")
    write(tmp_path, "untracked.md", "Runs on Sonnet.\n")

    result = lint(tmp_path)

    assert sorted(findings_of(result)) == [
        ("tracked.md", 1, "family-word", "Opus"),
        ("untracked.md", 1, "family-word", "Sonnet"),
    ]


def test_ignored_files_are_not_part_of_the_tree(tmp_path: Path, lint: Runner) -> None:
    make_tree(tmp_path)
    write(tmp_path, ".gitignore", "scratch/\n")
    write(tmp_path, "scratch/notes.md", "Built for Opus 4.6.\n")

    result = lint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert payload_of(result)["findings"] == []


def test_a_file_deleted_but_not_yet_staged_is_not_an_error(
    tmp_path: Path, lint: Runner
) -> None:
    make_tree(tmp_path)
    write(tmp_path, "gone.md", "plain text\n")
    git(tmp_path, "add", "gone.md")
    (tmp_path / "gone.md").unlink()

    assert lint(tmp_path).returncode == 0


def test_the_project_root_must_be_the_top_level_of_a_work_tree(
    tmp_path: Path, lint: Runner
) -> None:
    """Below the top level git lists paths relative to the subdirectory, and
    no exemption written from the top would match them."""
    root = make_tree(tmp_path / "repo")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    for wrong in (root / ".claude", plain):
        result = lint(wrong)
        assert result.returncode == EXIT_BAD_ARGS, result.stdout
        assert payload_of(result)["ok"] is False


def test_the_root_may_be_spelled_through_a_symlink_or_in_another_case(
    tmp_path: Path, lint: Runner
) -> None:
    root = make_tree(tmp_path / "Repo")
    os.symlink(root, tmp_path / "alias")
    spellings = [tmp_path / "alias"]
    if (tmp_path / "REPO").exists():
        # Only a case-insensitive filesystem has this second spelling.
        spellings.append(tmp_path / "REPO")

    for spelling in spellings:
        assert lint(spelling).returncode == 0, spelling


def test_a_sparse_checkout_is_an_error_not_a_partial_pass(
    tmp_path: Path, lint: Runner
) -> None:
    """Tracked files outside the sparse cone are not on disk to be read."""
    make_tree(tmp_path)
    git(tmp_path, "config", "core.sparseCheckout", "true")

    result = lint(tmp_path)

    assert result.returncode == EXIT_FAILED
    assert "sparse checkout" in payload_of(result)["error"]


def test_an_exported_git_dir_does_not_redirect_the_scan(
    tmp_path: Path, lint: Runner
) -> None:
    """Run from a hook or a wrapper, the lint must still list this tree."""
    root = make_tree(tmp_path / "repo")
    write(root, RULES_FILE, "Runs on Opus.\n")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    env = {**os.environ, "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}

    result = lint(root, env=env)

    assert findings_of(result) == [(RULES_FILE, 1, "family-word", "Opus")]


def test_an_empty_scan_is_an_error_not_a_pass(tmp_path: Path, lint: Runner) -> None:
    git(tmp_path, "init", "-q", "-b", "main")

    result = lint(tmp_path)

    assert result.returncode == EXIT_FAILED
    assert payload_of(result)["ok"] is False
    assert "no file to scan" in result.stderr


def test_lint_does_not_need_a_valid_manifest(tmp_path: Path, lint: Runner) -> None:
    """The gate must still say where the model names are when the manifest is
    the thing that is broken."""
    make_tree(tmp_path)
    write(tmp_path, MANIFEST_REL, "{ not json")
    write(tmp_path, RULES_FILE, "Runs on Opus.\n")

    result = lint(tmp_path)

    assert findings_of(result) == [(RULES_FILE, 1, "family-word", "Opus")]


def test_a_file_that_cannot_be_read_is_an_error_not_a_pass(
    tmp_path: Path,
    models_lint_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file the scan could not open holds text nobody checked. The read is
    denied inside the interpreter, which holds for any user."""
    make_tree(tmp_path)
    real_read = Path.read_bytes

    def deny(path: Path) -> bytes:
        if path.name == "routing.md":
            raise PermissionError(13, "Permission denied", str(path))
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", deny)

    status = models_lint_module.main(["--project-root", str(tmp_path)])

    assert status == EXIT_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "Permission denied" in payload["error"]


# --- symlinks: what a link points at must have been scanned ------------------------


def _link_outside(root: Path) -> None:
    outside = write(root.parent, "outside.md", "Built for Opus 4.6.\n")
    os.symlink(outside, root / ".claude/rules/link.md")


def _link_to_ignored(root: Path) -> None:
    write(root, ".gitignore", "scratch/\n")
    write(root, "scratch/notes.md", "Built for Opus 4.6.\n")
    os.symlink(root / "scratch/notes.md", root / ".claude/rules/link.md")


def _link_to_exempt(root: Path) -> None:
    write(root, ".claude/docs/research/note.md", "Measured on Opus 4.6.\n")
    os.symlink("../docs/research/note.md", root / ".claude/rules/link.md")


def _link_to_less_strict(root: Path) -> None:
    write(root, "tests/fixture.md", "model: opus\n")
    os.symlink("../../tests/fixture.md", root / ".claude/rules/link.md")


def _link_to_generated_file(root: Path) -> None:
    os.symlink("../../.codex/config.toml", root / ".claude/rules/link.md")


def _link_to_allowed_binary(root: Path) -> None:
    (root / "summary.png").write_bytes(b"\x89PNG\x00 Opus 4.6")
    os.symlink("../../summary.png", root / ".claude/rules/link.md")


def _link_to_directory(root: Path) -> None:
    os.symlink(root.parent, root / ".claude/rules/link.md")


def _dangling_link(root: Path) -> None:
    os.symlink("nowhere.md", root / ".claude/rules/link.md")


UNVOUCHED_LINKS = {
    "outside the project": _link_outside,
    "an ignored file": _link_to_ignored,
    "an exempt file": _link_to_exempt,
    "a file held to fewer rules": _link_to_less_strict,
    "a file whose model lines are excused as generated": _link_to_generated_file,
    "an allow-listed binary": _link_to_allowed_binary,
    "a directory": _link_to_directory,
    "nothing": _dangling_link,
}


@pytest.mark.parametrize("target", sorted(UNVOUCHED_LINKS))
def test_a_symlink_to_something_unscanned_is_a_finding(
    tmp_path: Path, lint: Runner, target: str
) -> None:
    """Claude Code follows the link and loads the text. If the gate did not
    read that text under some name, the link is a way around it."""
    root = make_tree(tmp_path / "repo")
    UNVOUCHED_LINKS[target](root)

    result = lint(root)

    assert result.returncode == EXIT_FINDINGS, result.stdout
    assert rules_at(result, ".claude/rules/link.md") == {"symlink"}


def test_a_symlink_to_a_scanned_file_is_fine(tmp_path: Path, lint: Runner) -> None:
    make_tree(tmp_path)
    os.symlink("routing.md", tmp_path / ".claude/rules/alias.md")

    result = lint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert payload_of(result)["findings"] == []


def test_a_file_below_a_symlinked_directory_is_a_finding(
    tmp_path: Path, lint: Runner
) -> None:
    """The index still lists the file; the work tree now reaches it through a
    link to somewhere else."""
    root = make_tree(tmp_path / "repo")
    write(root, "d/x.md", "plain text\n")
    git(root, "add", "d/x.md")
    (root / "d/x.md").unlink()
    (root / "d").rmdir()
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere, "x.md", "Built for Opus 4.6.\n")
    os.symlink(elsewhere, root / "d")

    result = lint(root)

    assert result.returncode == EXIT_FINDINGS
    assert rules_at(result, "d/x.md") == {"symlink"}


# --- files that are not text -------------------------------------------------------

NOT_TEXT = {
    "a NUL byte": b"Delegate to general-purpose-opus.\n" + b"\0",
    "UTF-16": "Built for Opus 4.6.\n".encode("utf-16"),
    "an invalid byte": b"Delegate to general-purpose-opus.\n\xff\n",
}


@pytest.mark.parametrize("kind", sorted(NOT_TEXT))
def test_a_file_that_is_not_text_is_a_finding(
    tmp_path: Path, lint: Runner, kind: str
) -> None:
    """One NUL byte makes git and a reviewer treat a rule file as binary while
    Claude Code still loads it. The gate reports the file instead of skipping
    it, and still reports what it can read in it."""
    make_tree(tmp_path)
    (tmp_path / RULES_FILE).write_bytes(NOT_TEXT[kind])

    result = lint(tmp_path)

    assert result.returncode == EXIT_FINDINGS, result.stdout
    assert "not-text" in rules_at(result, RULES_FILE)
    assert f"lint: {RULES_FILE}:1: not-text: " in result.stderr
    if kind != "UTF-16":
        assert "legacy-role" in rules_at(result, RULES_FILE)


def test_only_an_allow_listed_binary_is_left_unread(
    tmp_path: Path, lint: Runner, models_lint_module: ModuleType
) -> None:
    make_tree(tmp_path)
    allowed_binary = [path for path, _reason in models_lint_module.BINARY_ALLOWLIST]
    assert allowed_binary == ["summary.png"]
    image = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR Opus 4.6"
    (tmp_path / "summary.png").write_bytes(image)
    (tmp_path / "other.png").write_bytes(image)

    result = lint(tmp_path)

    assert payload_of(result)["binary"] == ["summary.png"]
    assert rules_at(result, "summary.png") == set()
    assert "not-text" in rules_at(result, "other.png")


def test_a_special_file_is_a_finding_and_is_never_opened(
    tmp_path: Path, lint: Runner
) -> None:
    """A FIFO where a tracked file was would block the read for ever."""
    make_tree(tmp_path)
    write(tmp_path, "notes.md", "plain text\n")
    git(tmp_path, "add", "notes.md")
    (tmp_path / "notes.md").unlink()
    os.mkfifo(tmp_path / "notes.md")

    result = lint(tmp_path)

    assert result.returncode == EXIT_FINDINGS
    assert rules_at(result, "notes.md") == {"not-text"}


def test_a_file_replaced_by_a_directory_is_scanned_as_its_files(
    tmp_path: Path, lint: Runner
) -> None:
    """Until it is staged git lists both the old file and the new files under
    its name. The files are scanned; the old entry is not a blind spot."""
    make_tree(tmp_path)
    write(tmp_path, "guide", "plain text\n")
    git(tmp_path, "add", "guide")
    (tmp_path / "guide").unlink()
    write(tmp_path, "guide/intro.md", "Runs on Opus.\n")

    result = lint(tmp_path)

    assert findings_of(result) == [("guide/intro.md", 1, "family-word", "Opus")]


def test_a_nested_repository_is_a_finding_not_a_blind_spot(
    tmp_path: Path, lint: Runner
) -> None:
    """git lists a nested repository as one entry and none of its files."""
    make_tree(tmp_path)
    nested = tmp_path / "vendor"
    nested.mkdir()
    git(nested, "init", "-q", "-b", "main")
    write(nested, "notes.md", "Built for Opus 4.6.\n")

    result = lint(tmp_path)

    assert result.returncode == EXIT_FINDINGS
    assert rules_at(result, "vendor/") == {"not-text"}


# --- exemptions ----------------------------------------------------------------


def test_every_exemption_states_its_reason(models_lint_module: ModuleType) -> None:
    lint_module = models_lint_module
    assert lint_module.EXEMPTIONS
    for path, rules, reason in lint_module.EXEMPTIONS:
        assert path and not path.startswith("/")
        assert rules and rules <= lint_module.TEXT_RULES
        assert len(reason.split()) >= 3, f"{path} has no real reason: {reason!r}"
    for _file_re, _line_re, _extent, reason in lint_module.GENERATED_LINES:
        assert len(reason.split()) >= 3
    for path, reason in lint_module.BINARY_ALLOWLIST:
        assert path and len(reason.split()) >= 3


def test_each_exemption_is_honoured_for_exactly_its_rules(
    tmp_path: Path, lint: Runner, models_lint_module: ModuleType
) -> None:
    """A file at an exempt path may hold what its entry excuses, and nothing
    more: a test fixture may name a model, not a retired role."""
    paths = [path for path, _rules, _reason in models_lint_module.EXEMPTIONS]
    assert len(paths) == len(set(paths))
    for index, exempt_path in enumerate(paths):
        root = make_tree(tmp_path / str(index))
        rel = exempt_path + "sample.md" if exempt_path.endswith("/") else exempt_path
        write(root, rel, "\n".join(OFFENDING.values()) + "\n")
        excused = models_lint_module.exempt_rules(rel)
        assert excused, f"{rel} matched no exemption"

        result = lint(root)

        assert rules_at(result, rel) == set(OFFENDING) - excused, rel
        assert result.returncode == (EXIT_FINDINGS if set(OFFENDING) - excused else 0)


def test_a_test_fixture_may_name_a_model_but_not_a_retired_role(
    tmp_path: Path, lint: Runner
) -> None:
    make_tree(tmp_path)
    write(tmp_path, "tests/test_sample.py", "\n".join(OFFENDING.values()) + "\n")

    result = lint(tmp_path)

    assert findings_of(result) == [
        ("tests/test_sample.py", 4, "legacy-role", "general-purpose-opus")
    ]


def test_generated_lines_are_exempt_and_the_rest_of_the_file_is_not(
    tmp_path: Path, lint: Runner
) -> None:
    """The lines `apply` writes hold model values by design. Everything else
    in those files is prose or configuration somebody typed."""
    make_tree(tmp_path)
    write(
        tmp_path,
        ".claude/agents/general-purpose-deep.md",
        agent_text(
            "general-purpose-deep", "opus", "This role runs on Opus.\nmodel: sonnet\n"
        ),
    )
    settings = {
        **SETTINGS,
        "statusMessage": "Sonnet is thinking",
        "profiles": {"fast": {"model": "haiku"}},
    }
    write(tmp_path, ".claude/settings.json", canonical(settings))
    write(
        tmp_path,
        ".codex/config.toml",
        "# was gpt-5.6-sol\n" + CONFIG + '[profiles.fast]\nmodel = "gpt-7-fast"\n',
    )

    result = lint(tmp_path)

    assert sorted(findings_of(result)) == [
        (".claude/agents/general-purpose-deep.md", 7, "family-word", "Opus"),
        # The same words outside the place `apply` writes them: a `model:`
        # line in the body, a nested settings key, a key inside a TOML table.
        (".claude/agents/general-purpose-deep.md", 8, "family-word", "sonnet"),
        (".claude/settings.json", 6, "family-word", "Sonnet"),
        (".claude/settings.json", 9, "family-word", "haiku"),
        (".codex/config.toml", 1, "model-id", "gpt-5.6-sol"),
        (".codex/config.toml", 5, "model-id", "gpt-7-fast"),
    ]


# --- the inline marker -----------------------------------------------------------


def test_a_marker_excuses_one_line_and_every_use_is_reported(
    tmp_path: Path, lint: Runner
) -> None:
    make_tree(tmp_path)
    write(
        tmp_path,
        RULES_FILE,
        "Upstream calls it the Opus edition. <!-- models-lint: allow quotes upstream -->\n"
        "This line carries the marker and nothing to excuse. models-lint: allow no reason\n",
    )

    result = lint(tmp_path)

    assert result.returncode == 0, result.stdout
    payload = payload_of(result)
    assert payload["findings"] == []
    assert payload["allowed"] == [
        {
            "file": RULES_FILE,
            "line": 1,
            "reason": "quotes upstream",
            "text": ["Opus"],
        }
    ]
    assert f"allowed: {RULES_FILE}:1: quotes upstream" in result.stderr


def test_a_marker_needs_a_reason_and_never_excuses_a_retired_role(
    tmp_path: Path, lint: Runner
) -> None:
    """A retired role name is always the codemod's to rewrite, so no marker
    can keep one: the two gates must not disagree."""
    make_tree(tmp_path)
    write(
        tmp_path,
        RULES_FILE,
        "Runs on Opus. <!-- models-lint: allow -->\n"
        "Ask general-purpose-opus on Sonnet. # models-lint: allow old name\n",
    )

    result = lint(tmp_path)

    assert result.returncode == EXIT_FINDINGS
    assert findings_of(result) == [
        (RULES_FILE, 1, "family-word", "Opus"),
        (RULES_FILE, 2, "legacy-role", "general-purpose-opus"),
    ]
    assert [item["text"] for item in payload_of(result)["allowed"]] == [["Sonnet"]]


@pytest.mark.parametrize(
    "separator",
    ["\r", "\r\n", " ", " ", "\x0b", "\x0c", "\x85"],
    ids=["CR", "CRLF", "LS", "PS", "VT", "FF", "NEL"],
)
def test_one_marker_cannot_excuse_a_file_without_newlines(
    tmp_path: Path, lint: Runner, separator: str
) -> None:
    """With bare carriage returns or Unicode separators a whole file is one
    line to anything that splits on LF only, and one marker would excuse it."""
    make_tree(tmp_path)
    lines = [
        "Runs on Opus.",
        "The Haiku edition. models-lint: allow quotes upstream",
        "Then gpt-5.6-sol.",
    ]
    write(tmp_path, RULES_FILE, separator.join(lines) + separator)

    result = lint(tmp_path)

    assert findings_of(result) == [
        (RULES_FILE, 1, "family-word", "Opus"),
        (RULES_FILE, 3, "model-id", "gpt-5.6-sol"),
    ]
    assert [item["line"] for item in payload_of(result)["allowed"]] == [2]


# --- this repository ---------------------------------------------------------------

# The lines this repository excuses with a marker, as (file, line). Adding one
# means editing this list, where a reviewer sees it.
ALLOWED_IN_THIS_REPOSITORY: list[tuple[str, int]] = []


def test_this_repository_is_clean_and_excuses_exactly_the_pinned_lines() -> None:
    result = subprocess.run(
        [sys.executable, str(LINT_SCRIPT)], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    payload = payload_of(result)
    allowed = [(item["file"], item["line"]) for item in payload["allowed"]]
    assert allowed == ALLOWED_IN_THIS_REPOSITORY
    assert payload["binary"] == ["summary.png"]


# --- a retired agent name in an override -----------------------------------------


def test_an_override_that_uses_a_retired_agent_name_is_refused_with_the_fix(
    tmp_path: Path, models_cli: Runner
) -> None:
    """The agent no longer exists under that name. Dropping the entry would
    discard the project's choice silently, so nothing is applied and the
    message is the exact edit."""
    make_tree(tmp_path)
    override = {
        "schema_version": 1,
        "claude": {"agents": {"general-purpose-opus": "haiku"}},
    }
    write(tmp_path, LOCAL_REL, canonical(override))
    before = snapshot(tmp_path)

    applied = models_cli(tmp_path, "apply")
    checked = models_cli(tmp_path, "check")

    assert applied.returncode == EXIT_REFUSED
    assert snapshot(tmp_path) == before
    refused = payload_of(applied)["refused"]
    assert [item["file"] for item in refused] == [
        ".claude/agents/general-purpose-opus.md"
    ]
    reason = refused[0]["reason"]
    assert "renamed to general-purpose-deep" in reason
    assert f"In {LOCAL_REL}" in reason
    assert 'rename the key "general-purpose-opus"' in reason
    assert "refused: .claude/agents/general-purpose-opus.md" in applied.stderr
    assert checked.returncode == EXIT_REFUSED

    # The fix the message names is the whole fix.
    override["claude"]["agents"] = {"general-purpose-deep": "haiku"}
    write(tmp_path, LOCAL_REL, canonical(override))
    assert models_cli(tmp_path, "apply").returncode == 0
    assert "\nmodel: haiku\n" in read(
        tmp_path, ".claude/agents/general-purpose-deep.md"
    )
    assert models_cli(tmp_path, "check").returncode == 0


def test_a_retired_agent_name_in_the_manifest_names_the_manifest(
    tmp_path: Path, models_cli: Runner
) -> None:
    make_tree(tmp_path)
    manifest = json.loads(canonical(MANIFEST))
    manifest["claude"]["agents"]["general-purpose-sonnet"] = "opus"
    write(tmp_path, MANIFEST_REL, canonical(manifest))

    result = models_cli(tmp_path, "apply")

    assert result.returncode == EXIT_REFUSED
    reason = payload_of(result)["refused"][0]["reason"]
    assert "renamed to general-purpose-standard" in reason
    assert f"In {MANIFEST_REL}" in reason


def test_an_unknown_agent_name_keeps_the_general_message(
    tmp_path: Path, models_cli: Runner
) -> None:
    make_tree(tmp_path)
    override = {"schema_version": 1, "claude": {"agents": {"gamma": "haiku"}}}
    write(tmp_path, LOCAL_REL, canonical(override))

    result = models_cli(tmp_path, "apply")

    assert result.returncode == EXIT_REFUSED
    reason = payload_of(result)["refused"][0]["reason"]
    assert "renamed" not in reason
    assert f"{LOCAL_REL} assigns claude.agents.gamma" in reason
