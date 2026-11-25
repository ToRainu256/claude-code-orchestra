"""The owner's ``/orche-install`` skill: its frontmatter, and its helper.

The helper is run against a temp ``HOME`` holding a stand-in for the job's
launcher and for the clone's ``launchd.sh``, and against scratch repositories.
``open`` is a recording stand-in: no real terminal window is opened, and the
one-shot script the window would run is executed here instead, with a
stand-in ``launchd.sh`` that records what it was given.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import maint_support as support
import pytest

SKILL = support.MAINT_DIR / "skills" / "orche-install"
HELPER = SKILL / "orche-install.sh"
# A name a shell would act on, were it ever parsed by one.
HOSTILE = "pro ject $(touch PWNED) `touch PWNED` 'q' \"d\"; touch PWNED"

FAKE_LAUNCHD = """#!/bin/bash
cmd_install_project() {{ :; }}
printf '%s\\n' "$@" > "{record}"
"""
FAKE_OPEN = """#!/bin/bash
printf '%s\\n' "$@" > "{record}"
exit "${{FAKE_OPEN_EXIT:-0}}"
"""


class Home:
    """A temp home with the job installed, and the recording stand-ins."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.state = self.home / ".claude" / "orchestra"
        self.launchd = self.state / "fork" / "scripts" / "maint" / "launchd.sh"
        self.launchd_record = tmp_path / "launchd-called"
        self.open_record = tmp_path / "open-called"
        self.ghostty = tmp_path / "Applications" / "Ghostty.app"
        self.tmpdir = tmp_path / "tmp"
        self.tmpdir.mkdir()
        self.approve_launchd(FAKE_LAUNCHD.format(record=self.launchd_record))
        launcher = self.state / "bin" / "orchestra-maint"
        launcher.parent.mkdir(parents=True)
        support.write_script(launcher, "#!/bin/bash\nexit 99\n")
        fake_open = tmp_path / "fake-open"
        support.write_script(fake_open, FAKE_OPEN.format(record=self.open_record))
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "ORCHE_INSTALL_TEST_SEAMS": "1",
            "ORCHE_INSTALL_OPEN": str(fake_open),
            "ORCHE_INSTALL_GHOSTTY_APP": str(self.ghostty),
        }

    def approve_launchd(self, text: str) -> None:
        """The job's clone holding ``text`` as launchd.sh, committed, checked
        out and approved: what the launcher leaves after a run."""
        clone = self.state / "fork"
        if not (clone / ".git").exists():
            clone.mkdir(parents=True)
            subprocess.run(
                [support.REAL_GIT, "init", "--quiet", str(clone)], check=True
            )
        self.launchd.parent.mkdir(parents=True, exist_ok=True)
        support.write_script(self.launchd, text)
        support.git(clone, "add", "-A")
        support.git(clone, "commit", "--quiet", "-m", "approved")
        state = self.state / "state"
        state.mkdir(exist_ok=True)
        head = support.git(clone, "rev-parse", "HEAD")
        (state / "approved-rev").write_text(f"{head}\n", encoding="utf-8")

    def helper(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(HELPER), *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**self.env, **extra},
        )

    def project(self, name: str = "fresh") -> Path:
        project = self.root / "projects" / name
        project.mkdir(parents=True)
        subprocess.run([support.REAL_GIT, "init", "--quiet", str(project)], check=True)
        (project / "README.md").write_text("# a project\n", encoding="utf-8")
        support.git(project, "add", "-A")
        support.git(project, "commit", "--quiet", "-m", "first")
        return project


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home(tmp_path.resolve())


def test_the_skill_can_only_be_started_by_the_owner() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---\n")[1]

    assert "name: orche-install\n" in frontmatter
    assert "disable-model-invocation: true\n" in frontmatter
    assert "You never type the confirmation" in text
    assert "You never run `install-project`" in text


def test_the_helper_never_answers_for_the_owner() -> None:
    text = HELPER.read_text(encoding="utf-8")

    assert "osascript" not in text
    assert "yes |" not in text and "<<< yes" not in text
    assert "--force" not in text


def test_the_window_seams_are_read_only_under_the_test_flag() -> None:
    text = HELPER.read_text(encoding="utf-8")
    flag = 'if [[ "${ORCHE_INSTALL_TEST_SEAMS:-}" == "1" ]]; then'
    before, guarded = text.split(flag, 1)
    guarded = guarded.split("\nfi\n", 1)[0]

    assert "ORCHE_INSTALL_OPEN" not in before
    assert "ORCHE_INSTALL_GHOSTTY_APP" not in before
    assert "ORCHE_INSTALL_OPEN" in guarded and "ORCHE_INSTALL_GHOSTTY_APP" in guarded
    assert "ORCHE_INSTALL_" not in text.split(flag, 1)[1].split("\nfi\n", 1)[1]
    assert "export PATH=/usr/bin:/bin:/usr/sbin:/sbin" in before


def test_check_runs_nothing_a_sessions_path_finds_first(home: Home) -> None:
    project = home.project()
    trap = home.root / "project-bin"
    trap.mkdir()
    for tool in ("git", "grep", "tr", "python3", "mktemp", "wc", "head", "chmod"):
        support.write_script(
            trap / tool, f"#!/bin/sh\ntouch '{home.root}/PWNED'\nexit 1\n"
        )

    done = home.helper(
        "check", str(project), PATH=f"{trap}{os.pathsep}{os.environ['PATH']}"
    )

    assert done.returncode == 0, done.stderr
    assert not (home.root / "PWNED").exists()


def test_check_accepts_a_clean_project_and_changes_nothing(home: Home) -> None:
    project = home.project()
    before = support.tree_digest(project)

    done = home.helper("check", str(project))

    assert done.returncode == 0, done.stderr
    assert f"ok: {project} can be installed" in done.stdout
    assert support.tree_digest(project) == before
    assert not home.open_record.exists()


def _dirty(home: Home, project: Path) -> str:
    (project / "notes.txt").write_text("x\n", encoding="utf-8")
    return str(project)


def _installed(home: Home, project: Path) -> str:
    stamp = project / ".claude" / "orchestra-version"
    stamp.parent.mkdir()
    stamp.write_text("1.0.0\n", encoding="utf-8")
    support.git(project, "add", "-A")
    support.git(project, "commit", "--quiet", "-m", "installed")
    return str(project)


def _adopted(home: Home, project: Path) -> str:
    record = home.state / "state" / "distribution.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"adopted": {str(project): {}}, "last": []}), encoding="utf-8"
    )
    return str(project)


def _unreadable(home: Home, project: Path) -> str:
    record = home.state / "state" / "distribution.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{ not json", encoding="utf-8")
    return str(project)


def _filter(home: Home, project: Path) -> str:
    support.git(project, "config", "filter.x.clean", "touch PWNED")
    return str(project)


def _subdirectory(home: Home, project: Path) -> str:
    (project / "docs").mkdir()
    return str(project / "docs")


def _link(home: Home, project: Path) -> str:
    link = home.root / "link"
    link.symlink_to(project)
    return str(link)


def _relative(home: Home, project: Path) -> str:
    return "projects/fresh"


def _no_launcher(home: Home, project: Path) -> str:
    (home.state / "bin" / "orchestra-maint").unlink()
    return str(project)


def _no_approval(home: Home, project: Path) -> str:
    (home.state / "state" / "approved-rev").unlink()
    return str(project)


def _foreign_approval(home: Home, project: Path) -> str:
    # A SHA the clone does not hold.
    (home.state / "state" / "approved-rev").write_text("1" * 40 + "\n")
    return str(project)


def _old_revision(home: Home, project: Path) -> str:
    home.approve_launchd("#!/bin/bash\nexit 1\n")
    return str(project)


CHECKS = {
    "dirty": (_dirty, "not clean"),
    "installed": (_installed, "already installed"),
    "adopted": (_adopted, "already adopted"),
    "unreadable-record": (_unreadable, "cannot be read"),
    "filter": (_filter, "a filter or an include"),
    "subdirectory": (_subdirectory, "does not hold its own git repository"),
    "link": (_link, "is a link or not its real spelling"),
    "relative": (_relative, "absolute path"),
    "job-not-installed": (_no_launcher, "not installed"),
    "no-approved-revision": (_no_approval, "cannot be read from"),
    "approved-revision-not-in-clone": (_foreign_approval, "cannot be read from"),
    "approved-revision-too-old": (_old_revision, "has no install-project"),
}


@pytest.mark.parametrize("case", sorted(CHECKS))
def test_check_and_open_refuse_and_open_no_window(home: Home, case: str) -> None:
    project = home.project()
    scenario, words = CHECKS[case]
    raw = scenario(home, project)

    for command in ("check", "open"):
        done = home.helper(command, raw)
        assert done.returncode == 2, (command, done.stdout, done.stderr)
        assert words in done.stderr
    assert not home.open_record.exists()
    assert not (project / "PWNED").exists()
    assert list(home.tmpdir.iterdir()) == []


def test_check_notes_a_project_listed_but_never_adopted(home: Home) -> None:
    project = home.project()
    (home.state / "targets.txt").write_text(f"{project}\n", encoding="utf-8")

    done = home.helper("check", str(project))

    assert done.returncode == 0, done.stderr
    assert "already listed" in done.stdout


@pytest.mark.parametrize("ghostty", [True, False], ids=["ghostty", "terminal-app"])
def test_open_starts_a_window_with_one_private_script_that_quotes_the_path(
    home: Home, ghostty: bool
) -> None:
    project = home.project(HOSTILE)
    if ghostty:
        home.ghostty.mkdir(parents=True)
    # The clone's work tree is not what runs: the approved commit's bytes are.
    support.write_script(home.launchd, "#!/bin/bash\ntouch PWNED\n")

    done = home.helper("open", str(project))

    assert done.returncode == 0, done.stderr
    opened = home.open_record.read_text(encoding="utf-8").splitlines()
    script = Path(opened[-1])
    if ghostty:
        assert opened == ["-na", str(home.ghostty), "--args", "-e", str(script)]
    else:
        assert opened == ["-a", "Terminal", str(script)]
    # The window is given one path that needs no quoting, private to the owner.
    assert script.parent.parent == home.tmpdir
    assert set(str(script)) <= set(
        "/abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    assert stat.S_IMODE(script.parent.stat().st_mode) == 0o700
    assert "yes" not in script.read_text(encoding="utf-8").split("install-project")[1]

    # What the window would run: exactly launchd.sh install-project <path>.
    ran = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        input="\n",
        cwd=str(home.root),
        env=home.env,
    )
    assert ran.returncode == 0, ran.stderr
    called = home.launchd_record.read_text(encoding="utf-8").splitlines()
    assert called == ["install-project", str(project)]
    assert not list(home.root.rglob("PWNED"))
    assert not script.exists() and not script.parent.exists()


def test_open_with_a_terminal_that_will_not_start_leaves_nothing_behind(
    home: Home,
) -> None:
    project = home.project()

    done = home.helper("open", str(project), FAKE_OPEN_EXIT="1")

    assert done.returncode == 2
    assert "no terminal window could be opened" in done.stderr
    assert list(home.tmpdir.iterdir()) == []


def test_open_refuses_a_temporary_directory_that_would_need_quoting(
    home: Home,
) -> None:
    project = home.project()
    odd = home.root / "t m p"
    odd.mkdir()

    done = home.helper("open", str(project), TMPDIR=str(odd))

    assert done.returncode == 2
    assert "needs quoting" in done.stderr
    assert not home.open_record.exists()
    assert list(odd.iterdir()) == []


def adopt_in_record(home: Home, project: Path, interrupted: str = "") -> None:
    root, repository = os.lstat(project), os.lstat(project / ".git")
    identity = [root.st_dev, root.st_ino, repository.st_dev, repository.st_ino]
    record = home.state / "state" / "distribution.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "identity": identity,
        "interrupted": interrupted,
        "revision": "b" * 40,
        "source_print": "c" * 64,
        "target_print": "d" * 64,
        "adopted_at": 1.0,
        "updated_at": 1.0,
    }
    record.write_text(
        json.dumps({"adopted": {str(project): entry}, "last": []}), encoding="utf-8"
    )


def test_verify_says_installed_listed_and_adopted(home: Home) -> None:
    project = home.project()
    _installed(home, project)
    (home.state / "targets.txt").write_text(f"{project}\n", encoding="utf-8")
    adopt_in_record(home, project)

    done = home.helper("verify", str(project))

    assert done.returncode == 0, done.stdout + done.stderr
    assert "installed: template 1.0.0" in done.stdout
    assert "listed:    yes" in done.stdout
    assert "adopted:   yes" in done.stdout


@pytest.mark.parametrize("what", ["stopped-half-way", "not-listed", "not-installed"])
def test_verify_names_what_is_missing(home: Home, what: str) -> None:
    project = home.project()
    if what != "not-installed":
        _installed(home, project)
    if what != "not-listed":
        (home.state / "targets.txt").write_text(f"{project}\n", encoding="utf-8")
    adopt_in_record(home, project, "a" * 40 if what == "stopped-half-way" else "")

    done = home.helper("verify", str(project))

    assert done.returncode == 2
    expected = {
        "stopped-half-way": "NOT WHOLE: a write stopped half-way",
        "not-listed": "listed:    NO",
        "not-installed": "installed: NO",
    }[what]
    assert expected in done.stdout


def test_verify_does_not_take_a_record_it_did_not_write_for_an_adoption(
    home: Home,
) -> None:
    project = home.project()
    _installed(home, project)
    (home.state / "targets.txt").write_text(f"{project}\n", encoding="utf-8")
    root, repository = os.lstat(project), os.lstat(project / ".git")
    identity = [root.st_dev, root.st_ino, repository.st_dev, repository.st_ino]
    record = home.state / "state" / "distribution.json"
    entry = {"identity": identity}
    record.write_text(
        json.dumps({"adopted": {str(project): entry}, "last": []}), encoding="utf-8"
    )

    done = home.helper("verify", str(project))

    assert done.returncode == 2
    assert "not one the job wrote" in done.stdout
