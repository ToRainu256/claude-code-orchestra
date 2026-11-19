"""Behavioral tests for the role-rename codemod (``scripts/rename-roles.py``).

The codemod rewrites files across the whole repository, so the cases below pin
what makes that safe: it rewrites whole role names and nothing else, a second
run changes nothing, history is never rewritten, and it refuses rather than
guesses at what it cannot do safely: a file that is not text, anything behind
a symlink, a root that is not the top of the repository.

Every case runs in a scratch git repository under ``tmp_path``. The CLI cases
run under every interpreter in ``INTERPRETERS``, because the script must stay
runnable on the macOS system Python 3.9.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "rename-roles.py"
SYSTEM_PYTHON = Path("/usr/bin/python3")
OLDEST_SUPPORTED = (3, 9)
EXIT_BAD_ARGS = 1
EXIT_PENDING = 2
EXIT_FAILED = 3

OLD_STANDARD = "general-purpose-sonnet"
OLD_DEEP = "general-purpose-opus"
NEW_STANDARD = "general-purpose-standard"
NEW_DEEP = "general-purpose-deep"
AGENTS = ".claude/agents"
HISTORY = ("TEMPLATE_DESIGN_LOG.md", ".claude/docs/reviews/audit.md")


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


@pytest.fixture(params=INTERPRETERS, ids=lambda path: Path(path).name)
def run(request: pytest.FixtureRequest) -> Runner:
    """Invoke the codemod under one of the supported interpreters."""

    def _run(
        root: Path, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [request.param, str(SCRIPT), "--project-root", str(root), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    return _run


@pytest.fixture(scope="module")
def codemod() -> ModuleType:
    """The codemod imported by path (its file name is not a module name)."""
    spec = importlib.util.spec_from_file_location("rename_roles", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolve their own module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def agent_text(name: str) -> str:
    return (
        f"---\nname: {name}\nmodel: opus\n---\n\n"
        f"Always pass --caller {name} to the wrapper.\n"
    )


def commit_all(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


def make_repo(root: Path) -> Path:
    """A repository that still uses the old role names everywhere."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test User")
    write(root, f"{AGENTS}/{OLD_STANDARD}.md", agent_text(OLD_STANDARD))
    write(root, f"{AGENTS}/{OLD_DEEP}.md", agent_text(OLD_DEEP))
    write(
        root,
        "docs/routing.md",
        f"intro\nRoutine work goes to `{OLD_STANDARD}`.\n"
        f"Use {OLD_DEEP} for research.\noutro\n",
    )
    write(
        root,
        "TEMPLATE_DESIGN_LOG.md",
        f"| Split into {OLD_STANDARD} and {OLD_DEEP} |\n",
    )
    write(root, ".claude/docs/reviews/audit.md", f"Found `{OLD_DEEP}` in the router.\n")
    commit_all(root, "base")
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file in the work tree: proves "nothing was written"."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and ".git" not in path.parts
    }


def status(root: Path) -> str:
    return git(root, "status", "--porcelain").stdout


def payload_of(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    # json.loads rejects trailing data: stdout holds exactly one JSON object.
    return json.loads(result.stdout)


# --- the transform -------------------------------------------------------------


def test_rename_text_replaces_whole_role_names_only(codemod: ModuleType) -> None:
    text = (
        f"`{OLD_DEEP}` and {OLD_STANDARD}.md and subagent_type='{OLD_DEEP}'\n"
        f"my-{OLD_DEEP} {OLD_DEEP}-plus {OLD_DEEP}2 x{OLD_STANDARD}\n"
    )

    renamed, count = codemod.rename_text(text)

    assert renamed == (
        f"`{NEW_DEEP}` and {NEW_STANDARD}.md and subagent_type='{NEW_DEEP}'\n"
        f"my-{OLD_DEEP} {OLD_DEEP}-plus {OLD_DEEP}2 x{OLD_STANDARD}\n"
    )
    assert count == 3
    assert codemod.rename_text(renamed) == (renamed, 0)


def test_mapping_is_the_decided_one(codemod: ModuleType) -> None:
    assert codemod.RENAMES == {OLD_STANDARD: NEW_STANDARD, OLD_DEEP: NEW_DEEP}


def test_every_allowlist_entry_states_its_reason(codemod: ModuleType) -> None:
    assert codemod.ALLOWLIST, "the allow-list is empty"
    for path, reason in (*codemod.ALLOWLIST, *codemod.BINARY_ALLOWLIST):
        assert path and not path.startswith("/")
        assert len(reason.split()) >= 3, f"{path} has no real reason: {reason!r}"


# --- check and apply -----------------------------------------------------------


def test_check_reports_what_is_pending_and_writes_nothing(
    tmp_path: Path, run: Runner
) -> None:
    root = make_repo(tmp_path / "repo")
    before = snapshot(root)

    result = run(root)

    assert result.returncode == EXIT_PENDING, result.stderr
    payload = payload_of(result)
    assert payload["ok"] is False and payload["result"] == "pending"
    assert {move["from"] for move in payload["moves"]} == {
        f"{AGENTS}/{OLD_STANDARD}.md",
        f"{AGENTS}/{OLD_DEEP}.md",
    }
    assert payload["files"]["docs/routing.md"] == 2
    assert "TEMPLATE_DESIGN_LOG.md" not in payload["files"]
    assert snapshot(root) == before
    assert status(root) == ""


def test_apply_moves_the_definitions_and_rewrites_every_reference(
    tmp_path: Path, run: Runner
) -> None:
    root = make_repo(tmp_path / "repo")

    result = run(root, "--apply")

    assert result.returncode == 0, result.stderr
    assert payload_of(result)["result"] == "applied"
    assert not (root / f"{AGENTS}/{OLD_STANDARD}.md").exists()
    assert not (root / f"{AGENTS}/{OLD_DEEP}.md").exists()
    assert read(root, f"{AGENTS}/{NEW_STANDARD}.md") == agent_text(NEW_STANDARD)
    assert read(root, f"{AGENTS}/{NEW_DEEP}.md") == agent_text(NEW_DEEP)
    assert read(root, "docs/routing.md") == (
        f"intro\nRoutine work goes to `{NEW_STANDARD}`.\n"
        f"Use {NEW_DEEP} for research.\noutro\n"
    )
    # git sees a move, not an unrelated delete and add.
    assert f"{AGENTS}/{OLD_DEEP}.md -> {AGENTS}/{NEW_DEEP}.md" in status(root)


def test_apply_twice_changes_nothing_the_second_time(
    tmp_path: Path, run: Runner
) -> None:
    root = make_repo(tmp_path / "repo")
    assert run(root, "--apply").returncode == 0
    after_first = snapshot(root)

    second = run(root, "--apply")

    assert second.returncode == 0, second.stderr
    assert payload_of(second)["result"] == "no-op"
    assert payload_of(second)["files"] == {}
    assert snapshot(root) == after_first
    check = run(root)
    assert check.returncode == 0
    assert payload_of(check)["result"] == "clean"


def test_apply_never_rewrites_history_or_ignored_files(
    tmp_path: Path, run: Runner
) -> None:
    root = make_repo(tmp_path / "repo")
    write(root, ".gitignore", "ignored.txt\n")
    write(root, "ignored.txt", f"{OLD_DEEP}\n")
    write(root, "untracked.md", f"{OLD_DEEP}\n")
    # A file that happens to sit where a careless temporary name would land.
    write(root, "docs/routing.md.rename-roles-tmp", "somebody's own notes\n")
    history = {rel: read(root, rel) for rel in HISTORY}

    assert run(root, "--apply").returncode == 0

    for rel, text in history.items():
        assert read(root, rel) == text
    assert read(root, "ignored.txt") == f"{OLD_DEEP}\n"
    assert read(root, "docs/routing.md.rename-roles-tmp") == "somebody's own notes\n"
    # A new file nobody has committed yet is still this repository's text.
    assert read(root, "untracked.md") == f"{NEW_DEEP}\n"
    assert run(root).returncode == 0


def test_apply_rewrites_a_path_with_a_space_and_a_non_ascii_name(
    tmp_path: Path, run: Runner
) -> None:
    root = make_repo(tmp_path / "repo")
    rel = "docs/my notes é.md"
    write(root, rel, f"Ask {OLD_DEEP}.\n")
    commit_all(root, "add notes")

    result = run(root, "--apply")

    assert result.returncode == 0, result.stderr
    assert read(root, rel) == f"Ask {NEW_DEEP}.\n"
    assert payload_of(result)["files"][rel] == 1


def test_apply_keeps_the_file_mode(tmp_path: Path, run: Runner) -> None:
    root = make_repo(tmp_path / "repo")
    script = write(root, "tools/route.sh", f"#!/bin/sh\necho {OLD_DEEP}\n")
    script.chmod(0o755)
    commit_all(root, "add script")

    assert run(root, "--apply").returncode == 0

    assert read(root, "tools/route.sh") == f"#!/bin/sh\necho {NEW_DEEP}\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert not list(root.rglob("*.rename-roles-tmp"))


def test_both_definitions_present_is_reported_and_nothing_is_overwritten(
    tmp_path: Path, run: Runner
) -> None:
    """Both names present means two definitions somebody has to reconcile;
    picking one would silently discard the other. Check says so too."""
    root = make_repo(tmp_path / "repo")
    write(root, f"{AGENTS}/{NEW_DEEP}.md", "a different definition\n")
    before = snapshot(root)

    checked = run(root)
    applied = run(root, "--apply")

    assert checked.returncode == EXIT_PENDING
    assert payload_of(checked)["result"] == "pending"
    assert NEW_DEEP in payload_of(checked)["collisions"][0]
    assert applied.returncode == EXIT_PENDING
    payload = payload_of(applied)
    assert payload["ok"] is False and payload["result"] == "refused"
    assert NEW_DEEP in payload["collisions"][0]
    assert snapshot(root) == before


def test_apply_reads_everything_before_it_changes_anything(
    tmp_path: Path, codemod: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that cannot be read stops the run with nothing moved, instead
    of leaving the definitions renamed and their references not."""
    root = make_repo(tmp_path / "repo")
    before = snapshot(root)
    real_read = Path.read_bytes

    def deny(path: Path) -> bytes:
        if path.name == "routing.md":
            raise PermissionError(13, "Permission denied", str(path))
        return real_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", deny)
        exit_code = codemod.main(["--project-root", str(root), "--apply"])

    assert exit_code == EXIT_FAILED
    assert snapshot(root) == before
    assert status(root) == ""


# --- files that are not text -----------------------------------------------------

NOT_TEXT = {
    "a NUL byte in valid UTF-8": b"\x00" + OLD_DEEP.encode() + b"\x00",
    "a NUL byte in binary data": b"\xff\xfe" + OLD_DEEP.encode() + b"\x00",
    "UTF-16": f"Ask {OLD_DEEP}.\n".encode("utf-16"),
    "Latin-1": b"caf\xe9 " + OLD_DEEP.encode() + b"\n",
}


@pytest.mark.parametrize("kind", sorted(NOT_TEXT))
def test_a_file_that_is_not_text_stops_check_and_apply(
    tmp_path: Path, run: Runner, kind: str
) -> None:
    """A NUL byte or a second encoding is also how a file would be hidden from
    the rename. It is reported, never skipped, and never rewritten."""
    root = make_repo(tmp_path / "repo")
    (root / "docs/odd.md").write_bytes(NOT_TEXT[kind])
    commit_all(root, "add an odd file")
    before = snapshot(root)

    checked = run(root)
    applied = run(root, "--apply")

    assert checked.returncode == EXIT_PENDING
    assert payload_of(checked)["unreadable"] == ["docs/odd.md"]
    assert "unreadable: docs/odd.md" in checked.stderr
    assert applied.returncode == EXIT_PENDING
    payload = payload_of(applied)
    assert payload["result"] == "refused"
    assert payload["unreadable"] == ["docs/odd.md"]
    assert snapshot(root) == before
    assert status(root) == ""


def test_an_allow_listed_binary_is_left_alone(tmp_path: Path, run: Runner) -> None:
    root = make_repo(tmp_path / "repo")
    image = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + OLD_DEEP.encode()
    (root / "summary.png").write_bytes(image)
    commit_all(root, "add the image")

    result = run(root, "--apply")

    assert result.returncode == 0, result.stderr
    assert payload_of(result)["unreadable"] == []
    assert (root / "summary.png").read_bytes() == image
    assert run(root).returncode == 0


def test_a_special_file_is_reported_and_never_opened(
    tmp_path: Path, run: Runner
) -> None:
    """A FIFO where a tracked file was would block the read for ever."""
    root = make_repo(tmp_path / "repo")
    (root / "docs/routing.md").unlink()
    os.mkfifo(root / "docs/routing.md")

    result = run(root)

    assert result.returncode == EXIT_PENDING
    assert payload_of(result)["unreadable"] == ["docs/routing.md"]


def test_apply_handles_a_file_name_at_the_length_limit(
    tmp_path: Path, run: Runner
) -> None:
    """The temporary file's name must not be longer than the file's own."""
    root = make_repo(tmp_path / "repo")
    rel = "docs/" + "n" * 247 + ".md"
    write(root, rel, f"Ask {OLD_DEEP}.\n")
    commit_all(root, "add a long name")

    result = run(root, "--apply")

    assert result.returncode == 0, result.stderr
    assert read(root, rel) == f"Ask {NEW_DEEP}.\n"


@pytest.mark.parametrize("kind", ["ignored", "directory"])
def test_only_a_listed_regular_file_is_moved_as_a_definition(
    tmp_path: Path, run: Runner, kind: str
) -> None:
    """Whatever else sits at an old path is not this repository's definition:
    it is reported, and nothing is renamed around it."""
    root = make_repo(tmp_path / "repo")
    old = root / f"{AGENTS}/{OLD_DEEP}.md"
    git(root, "rm", "-q", "--cached", f"{AGENTS}/{OLD_DEEP}.md")
    if kind == "ignored":
        write(root, ".gitignore", f"{AGENTS}/{OLD_DEEP}.md\n")
    else:
        old.unlink()
        write(root, f"{AGENTS}/{OLD_DEEP}.md/notes.md", "plain text\n")
    commit_all(root, "old path is no longer a tracked definition")
    before = snapshot(root)

    result = run(root, "--apply")

    assert result.returncode == EXIT_PENDING
    payload = payload_of(result)
    assert payload["result"] == "refused"
    assert len(payload["collisions"]) == 1
    assert OLD_DEEP in payload["collisions"][0]
    assert snapshot(root) == before
    assert status(root) == ""


# --- symlinks, and where the root is -----------------------------------------------


def test_a_symlink_stays_a_symlink_and_its_target_is_not_rewritten(
    tmp_path: Path, run: Runner
) -> None:
    root = make_repo(tmp_path / "repo")
    outside = write(tmp_path, "outside.md", f"{OLD_DEEP}\n")
    os.symlink(outside, root / "link.md")
    os.symlink("routing.md", root / "docs/alias.md")

    result = run(root, "--apply")

    assert result.returncode == 0, result.stderr
    assert sorted(payload_of(result)["symlinks"]) == ["docs/alias.md", "link.md"]
    assert (root / "link.md").is_symlink()
    assert os.readlink(root / "link.md") == str(outside)
    assert (root / "docs/alias.md").is_symlink()
    assert os.readlink(root / "docs/alias.md") == "routing.md"
    assert outside.read_text(encoding="utf-8") == f"{OLD_DEEP}\n"
    # The real file behind the inside link was rewritten once, as itself.
    assert f"Use {NEW_DEEP} for research." in read(root, "docs/routing.md")


def test_apply_does_not_write_through_a_symlinked_directory(
    tmp_path: Path, run: Runner
) -> None:
    """The index still lists the file; the work tree now reaches it through a
    link to a directory outside the repository."""
    root = make_repo(tmp_path / "repo")
    write(root, "d/x.md", f"Ask {OLD_DEEP}.\n")
    commit_all(root, "add d")
    (root / "d/x.md").unlink()
    (root / "d").rmdir()
    elsewhere = tmp_path / "elsewhere"
    outside = write(elsewhere, "x.md", f"Ask {OLD_DEEP}.\n")
    os.symlink(elsewhere, root / "d")

    result = run(root, "--apply")

    assert result.returncode == 0, result.stderr
    assert "d/x.md" in payload_of(result)["symlinks"]
    assert outside.read_text(encoding="utf-8") == f"Ask {OLD_DEEP}.\n"
    assert (root / "d").is_symlink()


def test_definitions_behind_a_symlinked_directory_are_not_moved(
    tmp_path: Path, run: Runner
) -> None:
    root = make_repo(tmp_path / "repo")
    real = tmp_path / "shared-agents"
    (root / AGENTS).rename(real)
    os.symlink(real, root / AGENTS)
    before = snapshot(real)

    checked = run(root)
    applied = run(root, "--apply")

    assert checked.returncode == EXIT_PENDING
    assert applied.returncode == EXIT_PENDING
    payload = payload_of(applied)
    assert payload["result"] == "refused"
    assert len(payload["collisions"]) == 2
    assert "symlink" in payload["collisions"][0]
    assert snapshot(real) == before
    assert f"Use {OLD_DEEP} for research." in read(root, "docs/routing.md")


def test_the_project_root_must_be_the_top_level_of_the_repository(
    tmp_path: Path, run: Runner
) -> None:
    """From a subdirectory git lists paths relative to it, the allow-list
    matches nothing, and the history under it would be rewritten."""
    root = make_repo(tmp_path / "repo")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    before = snapshot(root)

    for wrong in (root / ".claude", root / "docs", plain):
        for args in ((), ("--apply",)):
            result = run(wrong, *args)
            assert result.returncode == EXIT_BAD_ARGS, result.stdout
            assert payload_of(result)["ok"] is False
    assert snapshot(root) == before
    assert status(root) == ""
    # The top level spelled through a symlink is still the top level.
    os.symlink(root, tmp_path / "alias")
    assert run(tmp_path / "alias").returncode == EXIT_PENDING


def test_an_exported_git_dir_does_not_redirect_the_run(
    tmp_path: Path, run: Runner
) -> None:
    """Run from a hook or a wrapper, the codemod must still act on this tree
    and leave the other repository alone."""
    root = make_repo(tmp_path / "repo")
    other = make_repo(tmp_path / "other")
    other_before = snapshot(other)
    env = {**os.environ, "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}

    result = run(root, "--apply", env=env)

    assert result.returncode == 0, result.stderr
    assert read(root, f"{AGENTS}/{NEW_DEEP}.md") == agent_text(NEW_DEEP)
    assert snapshot(other) == other_before
    assert status(other) == ""


# --- CLI contract --------------------------------------------------------------


def test_usage_errors_stay_on_the_json_contract(tmp_path: Path, run: Runner) -> None:
    root = make_repo(tmp_path / "repo")

    result = run(root, "--definitely-not-a-real-flag")

    assert result.returncode == EXIT_BAD_ARGS
    assert payload_of(result)["ok"] is False


def test_the_tool_offers_check_and_apply_and_nothing_about_merges(
    tmp_path: Path, run: Runner
) -> None:
    """Conflict handling for upstream intake is deliberately not this tool's
    job; a flag that suggested otherwise would be used."""
    result = run(tmp_path, "--help")

    assert result.returncode == 0
    assert "--apply" in result.stdout
    for word in ("conflict", "merge", "resolve"):
        assert word not in result.stdout.lower()


# --- this repository -----------------------------------------------------------


def test_this_repository_has_nothing_left_to_rename() -> None:
    """The legacy-name gate: an old role name outside the allow-list fails,
    and so does a file the codemod could not read as text."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    payload = payload_of(result)
    assert payload["result"] == "clean"
    assert payload["unreadable"] == []
    assert payload["symlinks"] == []


def test_the_codemod_is_a_template_tool_that_is_never_shipped() -> None:
    """Downstream the agents arrive already renamed; the installer and the
    updater copy an explicit list of paths, and this script is not on it."""
    for script in ("scripts/install.sh", "scripts/update.sh"):
        assert SCRIPT.name not in (REPO_ROOT / script).read_text(encoding="utf-8")


def test_the_codemod_and_the_lint_agree(
    codemod: ModuleType, models_module: ModuleType, models_lint_module: ModuleType
) -> None:
    """The lint must excuse exactly what the codemod leaves alone. If the lint
    excused less, the gate would demand an edit the codemod refuses to make;
    if it excused more, an old name could survive both."""
    assert codemod.RENAMES == models_module.LEGACY_AGENT_NAMES
    lint_exempt = {
        path
        for path, _rules, _reason in models_lint_module.EXEMPTIONS
        if models_lint_module.RULE_LEGACY_ROLE in models_lint_module.exempt_rules(path)
    }
    assert {path for path, _reason in codemod.ALLOWLIST} == lint_exempt
    assert codemod.BINARY_ALLOWLIST == models_lint_module.BINARY_ALLOWLIST
    assert codemod.GIT_ENV_DROPPED == models_lint_module.GIT_ENV_DROPPED
