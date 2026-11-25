"""``install-project``: the template installed into a new project, the project
listed and adopted, all behind one typed ``yes``.

Every project here is a real git repository in a temp directory. The fork
carries the template this checkout ships, the real installer and the real
updater; the job's clone, state root and home are scratch. Nothing reaches
the network or the real home directory, and no real project is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import maint_support as support
import pytest
from maint_support import common, distribute, install_project, staging

LAUNCHD = support.MAINT_DIR / "launchd.sh"
STAMP_REL = ".claude/orchestra-version"
RULE_REL = ".claude/rules/testing.md"


@pytest.fixture
def fork(tmp_path: Path) -> support.Fork:
    return support.make_fork(tmp_path, real_template=True)


def plain_project(fork: support.Fork, name: str = "fresh") -> Path:
    """A clean repository with one commit and no template."""
    project = fork.root / "projects" / name
    project.mkdir(parents=True)
    subprocess.run([support.REAL_GIT, "init", "--quiet", str(project)], check=True)
    support.git(project, "checkout", "--quiet", "-b", "main")
    (project / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    support.git(project, "add", "-A")
    support.git(project, "commit", "--quiet", "-m", "first")
    return project


def registry(fork: support.Fork) -> Path:
    return distribute.registry_path(fork.paths)


def ledger_file(fork: support.Fork) -> Path:
    return fork.paths.state_dir / distribute.LEDGER_FILE


def install(
    fork: support.Fork, project: Path | str, answer: str = "yes"
) -> tuple[int, str, list[str]]:
    """Run the owner's command the way a terminal would: (exit code, what was
    printed, the prompts that were asked)."""
    said: list[str] = []
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    code = install_project.run(fork.context(), str(project), ask, said.append)
    return code, "\n".join(said), prompts


def porcelain(project: Path) -> list[str]:
    done = subprocess.run(
        [support.REAL_GIT, "-C", str(project), "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in done.stdout.split("\n") if line]


def git_bytes(project: Path) -> dict[str, bytes]:
    refs = support.git(project, "for-each-ref", "--format=%(objectname) %(refname)")
    return {
        "HEAD": (project / ".git" / "HEAD").read_bytes(),
        "index": (project / ".git" / "index").read_bytes(),
        "refs": refs.encode(),
    }


def registry_lines(fork: support.Fork) -> list[str]:
    return registry(fork).read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------
# yes, and anything else
# --------------------------------------------------------------------------


def test_install_project_shows_the_change_and_on_yes_installs_lists_and_adopts(
    fork: support.Fork,
) -> None:
    project = plain_project(fork)
    approved = str(fork.approved())
    before = git_bytes(project)

    code, said, prompts = install(fork, project)

    assert code == common.EXIT_OK, said
    assert f"Project:   {project}" in said
    assert f"Revision:  {approved}" in said
    assert "Installed: template none" in said
    assert "  create     1  .claude/orchestra-version" in said
    assert "  create     1  AGENTS.md" in said
    assert "  create     1  .gitignore" in said
    assert "before anything is written:" in said
    assert prompts == [
        f"Type yes to install {approved[:12]} into this project, list it, and "
        "let scheduled runs keep it updated: "
    ]
    # Installed from the approved revision, left uncommitted, nothing staged.
    stamp = (project / STAMP_REL).read_text(encoding="utf-8")
    assert stamp == support.git(fork.seed, "show", f"{approved}:VERSION") + "\n"
    assert (project / RULE_REL).is_file()
    assert git_bytes(project) == before
    assert all(line.startswith("?? ") for line in porcelain(project)), porcelain(
        project
    )
    # Listed once, and adopted for this very directory.
    assert registry_lines(fork) == [str(project)]
    adoption = json.loads(ledger_file(fork).read_text(encoding="utf-8"))["adopted"]
    assert adoption[str(project)]["revision"] == approved
    assert adoption[str(project)]["interrupted"] == ""


@pytest.mark.parametrize("answer", ["", "no", "y", "YES", "yes please"])
def test_install_project_with_any_other_answer_changes_nothing(
    fork: support.Fork, answer: str
) -> None:
    project = plain_project(fork)
    before = support.tree_digest(project)

    code, said, prompts = install(fork, project, answer=answer)

    assert code == common.EXIT_OK
    assert len(prompts) == 1
    assert "not confirmed; nothing was changed" in said
    assert support.tree_digest(project) == before
    assert not registry(fork).exists()
    assert not ledger_file(fork).exists()


def test_install_project_never_lists_a_project_twice(fork: support.Fork) -> None:
    project = plain_project(fork)
    # Listed already (by hand, without a final line break), among others.
    registry(fork).write_text(f"# mine\n/elsewhere/other\n{project}", encoding="utf-8")

    code, said, _ = install(fork, project)

    assert code == common.EXIT_OK, said
    assert "(it is listed already)" in said
    assert registry_lines(fork) == ["# mine", "/elsewhere/other", str(project)]


def test_install_project_keeps_every_byte_of_the_registry_it_adds_to(
    fork: support.Fork,
) -> None:
    project = plain_project(fork)
    registry(fork).write_bytes(b"# mine\n/elsewhere/other")

    code, said, _ = install(fork, project)

    assert code == common.EXIT_OK, said
    expected = f"# mine\n/elsewhere/other\n{project}\n".encode()
    assert registry(fork).read_bytes() == expected


def test_list_in_registry_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "targets.txt"

    first = install_project.list_in_registry(path, "/a/project")
    second = install_project.list_in_registry(path, "/a/project")

    assert first and second
    assert path.read_text(encoding="utf-8") == "/a/project\n"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_list_in_registry_refuses_a_link_and_writes_nothing_through_it(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("the owner's own file\n", encoding="utf-8")
    path = tmp_path / "targets.txt"
    path.symlink_to(elsewhere)

    with pytest.raises(OSError, match="is a link"):
        install_project.list_in_registry(path, "/a/project")

    assert path.is_symlink()
    assert elsewhere.read_text(encoding="utf-8") == "the owner's own file\n"


# --------------------------------------------------------------------------
# What has to hold before anything is asked
# --------------------------------------------------------------------------

Scenario = Callable[[support.Fork, Path], str]


def _relative(fork: support.Fork, project: Path) -> str:
    return os.path.relpath(project, Path.cwd())


def _control(fork: support.Fork, project: Path) -> str:
    return f"{project}\nother"


def _spaced(fork: support.Fork, project: Path) -> str:
    return f"{project} "


def _link(fork: support.Fork, project: Path) -> str:
    link = fork.root / "projects" / "link-to-fresh"
    link.symlink_to(project)
    return str(link)


def _slash(fork: support.Fork, project: Path) -> str:
    return f"{project}/"


def _missing(fork: support.Fork, project: Path) -> str:
    return str(fork.root / "projects" / "nowhere")


def _subdirectory(fork: support.Fork, project: Path) -> str:
    inner = project / "docs"
    inner.mkdir()
    (inner / "a.md").write_text("a\n", encoding="utf-8")
    support.git(project, "add", "-A")
    support.git(project, "commit", "--quiet", "-m", "docs")
    return str(inner)


def _dirty(fork: support.Fork, project: Path) -> str:
    (project / "notes.txt").write_text("not committed\n", encoding="utf-8")
    return str(project)


def _no_commit(fork: support.Fork, project: Path) -> str:
    bare = fork.root / "projects" / "empty"
    bare.mkdir()
    subprocess.run([support.REAL_GIT, "init", "--quiet", str(bare)], check=True)
    return str(bare)


def _installed(fork: support.Fork, project: Path) -> str:
    return str(support.make_project(fork.root / "projects" / "installed"))


def _owned_path(fork: support.Fork, project: Path) -> str:
    # A template-owned path the project already has: the installer refuses
    # it (without --force), and nothing is written.
    (project / ".claude" / "rules").mkdir(parents=True)
    (project / ".claude" / "rules" / "own.md").write_text("# own\n", encoding="utf-8")
    support.git(project, "add", "-A")
    support.git(project, "commit", "--quiet", "-m", "own rules")
    return str(project)


def _filter(fork: support.Fork, project: Path) -> str:
    support.git(project, "config", "filter.x.clean", "touch PWNED")
    return str(project)


def _registry_link(fork: support.Fork, project: Path) -> str:
    elsewhere = fork.root / "elsewhere.txt"
    elsewhere.write_text("the owner's own file\n", encoding="utf-8")
    registry(fork).symlink_to(elsewhere)
    return str(project)


def _job_dir(fork: support.Fork, project: Path) -> str:
    return str(fork.paths.clone)


def _hidden(fork: support.Fork, project: Path) -> str:
    # A tracked file git was told not to look at: a new installation wants
    # a tree that is clean in every sense.
    support.git(project, "update-index", "--assume-unchanged", "README.md")
    return str(project)


PRECONDITIONS: dict[str, tuple[Scenario, int, str]] = {
    "relative": (_relative, common.EXIT_USAGE, "is not an absolute path"),
    "control-character": (_control, common.EXIT_USAGE, "holds a control character"),
    "white-space": (_spaced, common.EXIT_USAGE, "white space"),
    "link": (_link, common.EXIT_NEEDS_OWNER, "is a link"),
    "trailing-slash": (_slash, common.EXIT_NEEDS_OWNER, "not written as its real path"),
    "missing": (_missing, common.EXIT_NEEDS_OWNER, "does not exist"),
    "subdirectory": (
        _subdirectory,
        common.EXIT_NEEDS_OWNER,
        "does not hold its own git repository",
    ),
    "dirty": (_dirty, common.EXIT_NEEDS_OWNER, "has 1 uncommitted paths"),
    "no-commit": (_no_commit, common.EXIT_NEEDS_OWNER, "has no commit yet"),
    "already-installed": (
        _installed,
        common.EXIT_NEEDS_OWNER,
        "already installed there",
    ),
    "template-owned-path": (
        _owned_path,
        common.EXIT_NEEDS_OWNER,
        "refused by the installer",
    ),
    "filter": (_filter, common.EXIT_NEEDS_OWNER, "sets the git configuration key"),
    "registry-link": (_registry_link, common.EXIT_NEEDS_OWNER, "is a link"),
    "job-directory": (_job_dir, common.EXIT_NEEDS_OWNER, "the job's own directory"),
    "hidden-from-git": (_hidden, common.EXIT_NEEDS_OWNER, "has 1 uncommitted paths"),
}


@pytest.mark.parametrize("case", sorted(PRECONDITIONS))
def test_install_project_refuses_before_it_asks_and_changes_nothing(
    fork: support.Fork, case: str
) -> None:
    project = plain_project(fork)
    scenario, expected, words = PRECONDITIONS[case]
    raw = scenario(fork, project)
    listing = registry(fork)
    registry_before = os.readlink(listing) if listing.is_symlink() else None
    before = support.tree_digest(fork.root / "projects")

    code, said, prompts = install(fork, raw)

    assert code == expected, said
    assert words in said
    assert prompts == []
    assert support.tree_digest(fork.root / "projects") == before
    assert not ledger_file(fork).exists()
    if registry_before is None:
        assert not listing.exists()
    else:
        assert os.readlink(listing) == registry_before
        assert (fork.root / "elsewhere.txt").read_text() == "the owner's own file\n"
    assert not (project / "PWNED").exists()


def test_install_project_names_what_the_installer_refused(fork: support.Fork) -> None:
    project = plain_project(fork)
    _owned_path(fork, project)

    _, said, _ = install(fork, project)

    assert "Installation stopped before making changes" in said
    assert "(.claude/rules)" in said


def test_install_project_without_an_approved_revision_is_a_usage_error(
    fork: support.Fork,
) -> None:
    project = plain_project(fork)
    fork.paths.approved_file.unlink()

    code, said, prompts = install(fork, project)

    assert code == common.EXIT_USAGE
    assert "no approved revision" in said
    assert prompts == []


# --------------------------------------------------------------------------
# After the answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meanwhile",
    ["a-new-file", "a-commit", "a-filter", "a-template-path", "approval-moved"],
)
def test_install_project_looks_again_after_the_answer_and_writes_nothing_if_it_moved(
    fork: support.Fork, meanwhile: str
) -> None:
    project = plain_project(fork)
    said: list[str] = []

    def answer_after_something_happened(_prompt: str) -> str:
        if meanwhile == "a-new-file":
            (project / "notes.txt").write_text("new\n", encoding="utf-8")
        elif meanwhile == "a-commit":
            support.git(project, "commit", "--quiet", "--allow-empty", "-m", "work")
        elif meanwhile == "a-filter":
            support.git(project, "config", "filter.x.clean", "touch PWNED")
        elif meanwhile == "a-template-path":
            # Empty, so git does not see it; the installer would refuse it.
            (project / ".claude" / "rules").mkdir(parents=True)
        else:
            fork.approve("1" * 40)
        return "yes"

    code = install_project.run(
        fork.context(), str(project), answer_after_something_happened, said.append
    )

    assert code != common.EXIT_OK, said
    assert "nothing was written" in "\n".join(said) or meanwhile == "approval-moved"
    assert not (project / STAMP_REL).exists()
    assert not (project / "PWNED").exists()
    assert not registry(fork).exists()
    assert not ledger_file(fork).exists()


def test_scheduled_runs_leave_an_uncommitted_installation_alone_until_committed(
    fork: support.Fork,
) -> None:
    project = plain_project(fork)
    assert install(fork, project)[0] == common.EXIT_OK

    # Same template as written there: uncommitted, not an alarm.
    first = distribute.run(fork.context())
    rows = json.loads(ledger_file(fork).read_text(encoding="utf-8"))["last"]
    assert (first.result, first.exit_code) == ("up-to-date", common.EXIT_OK)
    assert [row["state"] for row in rows] == [distribute.UNCOMMITTED]

    # A newer approved template waits for the owner's commit.
    sha = fork.commit({RULE_REL: "# newer rules\n", "VERSION": "9.9.9\n"}, "rules")
    fork.fetch()
    fork.approve(sha)
    before = support.tree_digest(project)
    waiting = distribute.run(fork.context())
    rows = json.loads(ledger_file(fork).read_text(encoding="utf-8"))["last"]
    assert (waiting.result, waiting.exit_code) == ("waiting", common.EXIT_OK)
    assert [row["state"] for row in rows] == [distribute.WAITING_DIRTY]
    assert support.tree_digest(project) == before

    # Committed: the next run brings the newer template, left uncommitted.
    support.git(project, "add", "-A")
    support.git(project, "commit", "--quiet", "-m", "install the template")
    updated = distribute.run(fork.context())
    assert updated.result == "distributed", updated.detail
    assert (project / RULE_REL).read_text(encoding="utf-8") == "# newer rules\n"


# --------------------------------------------------------------------------
# At a terminal, through run.py and launchd.sh
# --------------------------------------------------------------------------


def run_py(fork: support.Fork) -> Path:
    """The dispatcher inside the job's clone, checked out the way the
    launcher leaves it."""
    approved = str(fork.approved())
    support.git(
        fork.paths.clone, "checkout", "--quiet", "--force", "-B", "approved", approved
    )
    return fork.paths.clone / "scripts" / "maint" / "run.py"


def test_install_project_without_a_terminal_refuses_and_changes_nothing(
    fork: support.Fork,
) -> None:
    project = plain_project(fork)
    script = run_py(fork)
    before = support.tree_digest(project)

    done = subprocess.run(
        [sys.executable, str(script), "install-project", str(project)],
        capture_output=True,
        text=True,
        check=False,
        env=fork.env,
        input="yes\n",
    )

    assert done.returncode == common.EXIT_USAGE
    assert "needs a terminal" in done.stdout
    assert support.tree_digest(project) == before
    assert not registry(fork).exists()
    assert not ledger_file(fork).exists()


def test_install_project_at_a_terminal_installs_lists_and_adopts(
    fork: support.Fork,
) -> None:
    project = plain_project(fork)
    script = run_py(fork)

    done = support.run_with_terminal(
        [sys.executable, str(script), "install-project", str(project)],
        fork.env,
        "yes\n",
    )

    assert done.returncode == common.EXIT_OK, done.stdout + done.stderr
    assert "now holds" in done.stdout
    assert (project / STAMP_REL).is_file()
    assert registry_lines(fork) == [str(project)]
    assert str(project) in json.loads(ledger_file(fork).read_text())["adopted"]


def launchd(
    home: Path, args: list[str], answers: str | None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    argv = ["/bin/bash", str(LAUNCHD), *args]
    if answers is not None:
        return support.run_with_terminal(argv, env, answers)
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, env=env, input="yes\n"
    )


def test_launchd_install_project_goes_through_the_installed_launcher_at_a_terminal(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "some project"
    project.mkdir()
    link = tmp_path / "a link"
    link.symlink_to(project)
    record = tmp_path / "launcher-called"
    stub = home / ".claude" / "orchestra" / "bin" / "orchestra-maint"
    stub.parent.mkdir(parents=True)
    support.write_script(
        stub,
        f'#!/bin/bash\nprintf "%s|" "$@" > "{record}"\n'
        f'echo "tty=$([ -t 0 ] && echo yes)" >> "{record}"\nexit 7\n',
    )

    piped = launchd(home, ["install-project", str(project)], None)
    relative = launchd(home, ["install-project", "some project"], "yes\n")
    linked = launchd(home, ["install-project", str(link)], "yes\n")
    missing = launchd(home, ["install-project", str(tmp_path / "nowhere")], "yes\n")
    unnamed = launchd(home, ["install-project"], "yes\n")
    assert not record.exists()
    done = launchd(home, ["install-project", str(project)], "yes\n")

    assert piped.returncode == 1 and "needs a terminal" in piped.stderr
    assert relative.returncode == 1 and "absolute path" in relative.stderr
    assert linked.returncode == 1 and "or a link" in linked.stderr
    assert missing.returncode == 1 and "not a directory" in missing.stderr
    assert unnamed.returncode == 1 and "usage:" in unnamed.stderr
    assert done.returncode == 7
    # Named exactly as given, never resolved.
    assert record.read_text(encoding="utf-8") == f"install-project|{project}|tty=yes\n"


def test_install_project_before_the_job_is_installed_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()

    done = launchd(tmp_path / "home", ["install-project", str(project)], "yes\n")

    assert done.returncode == 1
    assert "not installed" in done.stderr


def test_the_installer_files_are_staged_and_checked_like_the_updaters() -> None:
    assert staging.extra_files(staging.UPDATE) == ()
    assert set(staging.SETTINGS_CANDIDATES) <= set(staging.INSTALL_FILES)
    installer = support.INSTALLER.read_text(encoding="utf-8")
    for rel in staging.INSTALL_FILES:
        assert rel in installer, rel
