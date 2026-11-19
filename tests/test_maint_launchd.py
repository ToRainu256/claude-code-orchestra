"""``scripts/maint/launchd.sh``: what it prints, installs and removes.

Nothing here loads a real LaunchAgent. ``HOME`` is a temp directory, ``git``
is the stand-in that sends the fork URL to a scratch repository, and
``launchctl`` is a recording fake that every test asserts it is about to use.
The subcommands that ask for an approval are run at a pseudo-terminal: they
refuse anything else.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import maint_support as support
import pytest
from maint_support import common

LAUNCHD = support.MAINT_DIR / "launchd.sh"
LAUNCHER = support.MAINT_DIR / "orchestra-maint"
TEMPLATE = support.MAINT_DIR / "orchestra-maint.plist.in"
LABEL = "io.github.torainu256.orchestra-maint"
BASH = "/bin/bash"
PRINTABLE = {chr(code) for code in range(0x20, 0x7F)} | {"\n"}

FAKE_LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$FAKE_LAUNCHCTL_LOG"
case "$1" in
    print) [ -f "$FAKE_LAUNCHCTL_LOADED" ] ;;
    bootstrap) : > "$FAKE_LAUNCHCTL_LOADED" ;;
    bootout) rm -f "$FAKE_LAUNCHCTL_LOADED" ;;
esac
"""
FAKE_GH = """#!/bin/sh
if [ "$1 $2 $3 $4" = "api --hostname github.com user" ]; then
    cat "$HOME/fake-login"
    exit 0
fi
exit 98
"""
OLD_PYTHON = """#!/bin/sh
# Stands in for an interpreter older than the job needs: the version probe,
# and only it, fails.
[ "$1" = "-c" ] && exit 1
exit 0
"""


def run_launchd(
    home: Path,
    *args: str,
    env: dict[str, str] | None = None,
    answers: str | None = None,
    script: Path = LAUNCHD,
) -> subprocess.CompletedProcess[str]:
    """Run ``launchd.sh``. With ``answers`` its stdin is a terminal they are
    typed into; without, stdin is closed."""
    full_env = {"PATH": os.environ["PATH"], **(env or {}), "HOME": str(home)}
    argv = [BASH, str(script), *args]
    if answers is not None:
        return support.run_with_terminal(argv, full_env, answers)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env=full_env,
    )


def listing(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if ".git" not in path.parts
    )


# --------------------------------------------------------------------------
# print
# --------------------------------------------------------------------------


def test_print_renders_a_valid_plist_for_this_home(tmp_path: Path) -> None:
    done = run_launchd(tmp_path, "print")

    assert done.returncode == 0, done.stderr
    plist = plistlib.loads(done.stdout.encode("utf-8"))
    base = f"{tmp_path}/.claude/orchestra"
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [BASH, f"{base}/bin/orchestra-maint", "run"]
    assert plist["StartCalendarInterval"] == [
        {"Hour": 8, "Minute": 40},
        {"Hour": 20, "Minute": 40},
    ]
    assert plist["RunAtLoad"] is False
    assert plist["Umask"] == 0o077
    assert plist["WorkingDirectory"] == base
    assert plist["StandardOutPath"] == f"{base}/logs/launchd.out.log"
    assert plist["StandardErrorPath"] == f"{base}/logs/launchd.err.log"
    assert "@" not in done.stdout  # every placeholder was filled in


def test_print_plist_carries_no_credentials(tmp_path: Path) -> None:
    plist = plistlib.loads(run_launchd(tmp_path, "print").stdout.encode("utf-8"))

    assert sorted(plist["EnvironmentVariables"]) == [
        "LANG",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
    ]
    assert plist["EnvironmentVariables"]["PATH"].startswith(f"{tmp_path}/.local/share/")
    template = TEMPLATE.read_text(encoding="utf-8").lower()
    for word in ("token", "secret", "password", "gh_", "api_key"):
        assert word not in template


def test_print_lists_every_path_and_changes_nothing(tmp_path: Path) -> None:
    done = run_launchd(tmp_path, "print")

    base = f"{tmp_path}/.claude/orchestra"
    for expected in (
        "nothing was changed",
        f"{tmp_path}/Library/LaunchAgents/{LABEL}.plist",
        f"{base}/bin/orchestra-maint",
        f"{base}/fork/",
        f"{base}/state/approved-rev",
        f"{base}/logs/maint/",
        f"{base}/state/last-run.json",
        f"{base}/state/last-good-rev",
        f"{base}/state/bump-records.json",
        f"{base}/state/launcher-failure",
        f"{base}/run/",
        "08:40 and 20:40",
    ):
        assert expected in done.stdout
    assert list(tmp_path.iterdir()) == []


def test_print_output_passes_plutil_where_it_exists(tmp_path: Path) -> None:
    done = run_launchd(tmp_path, "print")
    plutil = shutil.which("plutil")

    # plutil is macOS only; the XML itself is parsed on every platform.
    assert plistlib.loads(done.stdout.encode("utf-8"))["Label"] == LABEL
    if plutil is not None:
        lint = subprocess.run(
            [plutil, "-lint", "-"],
            input=done.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
        assert lint.returncode == 0, lint.stdout + lint.stderr


@pytest.mark.parametrize(
    "home", ["/tmp/a b", "relative/home", "/tmp/a--b", "/tmp/a&b", ""]
)
def test_print_with_a_home_it_cannot_write_into_xml_refuses(home: str) -> None:
    done = subprocess.run(
        [BASH, str(LAUNCHD), "print"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], "HOME": home},
    )

    assert done.returncode == 1
    assert done.stdout == ""
    assert "HOME must be a plain absolute path" in done.stderr


@pytest.mark.parametrize("args", [[], ["bogus"], ["print", "extra"], ["--help"]])
def test_unknown_usage_exits_one_and_changes_nothing(
    tmp_path: Path, args: list[str]
) -> None:
    done = run_launchd(tmp_path, *args)

    assert done.returncode == 1
    assert "usage:" in done.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("script", [LAUNCHD, LAUNCHER])
def test_shell_files_parse_under_the_system_bash(script: Path) -> None:
    done = subprocess.run(
        [BASH, "-n", str(script)], capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr


def test_launcher_and_installer_name_the_same_fork_by_full_ref() -> None:
    for script in (LAUNCHD, LAUNCHER):
        text = script.read_text(encoding="utf-8")
        assert "DeL-TaiseiOzaki" not in text
        assert common.FORK_OWNER in text
        assert common.MAIN_REF in text
        # The short name is how a tag would stand in for the branch.
        assert "origin/main^" not in text.replace(f"{common.MAIN_REF}^", "")
        assert "--no-tags" in text
    assert common.FORK_GIT_URL in LAUNCHER.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# install, status, run-now, uninstall: against fakes
# --------------------------------------------------------------------------


class Mac:
    """A temp home with fake job tools, a fake launchctl and a scratch fork."""

    def __init__(self, tmp_path: Path, bare: Path | None = None) -> None:
        self.home = tmp_path / "home"
        self.base = self.home / ".claude" / "orchestra"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        self.shims = self.home / ".local" / "share" / "mise" / "shims"
        self.fakebin = tmp_path / "fakebin"
        self.log = tmp_path / "launchctl.log"
        self.loaded = tmp_path / "launchctl.loaded"
        shared = support._template_world(False, support.SEED_IN_SCAFFOLD) / "fork.git"
        self.bare = shared if bare is None else bare
        self.shims.mkdir(parents=True)
        self.fakebin.mkdir()
        support.write_script(self.fakebin / "launchctl", FAKE_LAUNCHCTL)
        support.write_fake_git(self.fakebin / "git", self.bare)
        if shutil.which("plutil") is None:
            support.write_script(self.fakebin / "plutil", "#!/bin/sh\nexit 0\n")
        support.write_script(self.shims / "gh", FAKE_GH)
        support.write_script(self.shims / "codex", "#!/bin/sh\nexit 0\n")
        support.write_script(
            self.shims / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
        )
        (self.home / "fake-login").write_text(
            f"{common.FORK_OWNER}\n", encoding="utf-8"
        )
        self.env = {
            "PATH": f"{self.fakebin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LAUNCHCTL_LOG": str(self.log),
            "FAKE_LAUNCHCTL_LOADED": str(self.loaded),
        }
        # The real launchctl and the network must be unreachable before
        # anything is run.
        for tool in ("launchctl", "git"):
            assert shutil.which(tool, path=self.env["PATH"]) == str(self.fakebin / tool)

    def run(
        self, *args: str, answers: str | None = None, script: Path = LAUNCHD
    ) -> subprocess.CompletedProcess[str]:
        return run_launchd(
            self.home, *args, env=self.env, answers=answers, script=script
        )

    def install(self, answers: str = "yes\nyes\n") -> subprocess.CompletedProcess[str]:
        return self.run("install", answers=answers)

    def launchctl_calls(self, verb: str) -> list[str]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if line.split()[0] == verb]

    def installed_paths(self) -> list[str]:
        return [path for path in listing(self.home) if not path.startswith(".local")]

    def approved(self) -> str:
        return (
            (self.base / "state" / "approved-rev").read_text(encoding="utf-8").strip()
        )


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    return Mac(tmp_path)


def test_install_creates_exactly_what_print_lists(mac: Mac) -> None:
    done = mac.install()

    assert done.returncode == 0, done.stdout + done.stderr
    head = support.git(mac.bare, "rev-parse", "main")
    assert mac.approved() == head
    assert mac.plist.read_text(encoding="utf-8") in mac.run("print").stdout
    launcher = mac.base / "bin" / "orchestra-maint"
    assert launcher.read_bytes() == LAUNCHER.read_bytes()
    assert oct(launcher.stat().st_mode & 0o777) == "0o700"
    clone = mac.base / "fork"
    assert (
        support.git(clone, "config", "--get", "remote.origin.url")
        == common.FORK_GIT_URL
    )
    assert support.git(clone, "remote") == "origin"
    assert support.git(clone, "config", "--get", common.OWNERSHIP_KEY) == "true"
    assert support.git(clone, "config", "--get", "remote.origin.tagOpt") == "--no-tags"
    # Only the default branch, by its full name; nothing checked out.
    assert support.git(clone, "for-each-ref", "--format=%(refname)") == common.MAIN_REF
    assert [path.name for path in clone.iterdir()] == [".git"]
    assert mac.installed_paths() == [
        ".claude",
        ".claude/orchestra",
        ".claude/orchestra/bin",
        ".claude/orchestra/bin/orchestra-maint",
        ".claude/orchestra/fork",
        ".claude/orchestra/state",
        ".claude/orchestra/state/approved-rev",
        "Library",
        "Library/LaunchAgents",
        f"Library/LaunchAgents/{LABEL}.plist",
        "fake-login",
    ]
    assert len(mac.launchctl_calls("bootstrap")) == 1
    assert mac.launchctl_calls("bootstrap")[0].endswith(str(mac.plist))


def test_install_shows_the_revision_for_review_before_asking(mac: Mac) -> None:
    done = mac.install()

    head = support.git(mac.bare, "rev-parse", "main")
    shown, asked = done.stdout.split(f"to let this Mac execute {head}", 1)
    assert f"({common.MAIN_REF}) is at:" in shown
    assert f"  {head}\n" in shown  # the full SHA
    assert "  committed 20" in shown  # the commit date
    assert "seed" in shown  # its subject
    assert f"Inspect: git -C {mac.base}/fork " in shown
    assert "(recorded)" in asked


def test_install_twice_is_idempotent(mac: Mac) -> None:
    mac.install()
    before = mac.installed_paths()
    approved = mac.approved()

    again = mac.install(answers="yes\n")

    assert again.returncode == 0, again.stdout + again.stderr
    assert "already loaded with this plist" in again.stdout
    assert "(kept)" in again.stdout
    assert mac.installed_paths() == before
    assert mac.approved() == approved
    assert len(mac.launchctl_calls("bootstrap")) == 1
    assert mac.launchctl_calls("bootout") == []


@pytest.mark.parametrize("answers", ["", "no\n", "y\n", "YES\n"])
def test_install_not_confirmed_changes_nothing(mac: Mac, answers: str) -> None:
    done = mac.install(answers=answers)

    assert done.returncode == 1
    assert "nothing was changed" in done.stderr
    assert "launchd.sh install will:" in done.stdout  # the plan was shown first
    assert mac.installed_paths() == ["fake-login"]
    assert mac.launchctl_calls("bootstrap") == []


@pytest.mark.parametrize("command", ["install", "promote"])
def test_an_approval_that_is_piped_is_refused(mac: Mac, command: str) -> None:
    # Not a terminal: a prepared answer on stdin, or none at all.
    piped = subprocess.run(
        [BASH, str(LAUNCHD), command],
        capture_output=True,
        text=True,
        check=False,
        input="yes\nyes\n",
        env={**mac.env, "HOME": str(mac.home)},
    )
    closed = mac.run(command)

    for done in (piped, closed):
        assert done.returncode == 1
        assert "needs a terminal" in done.stderr
    assert mac.installed_paths() == ["fake-login"]
    assert mac.launchctl_calls("bootstrap") == []


def test_install_with_revision_not_confirmed_loads_nothing(mac: Mac) -> None:
    done = mac.install(answers="yes\nno\n")

    assert done.returncode == 1
    assert "no approved revision was recorded" in done.stderr
    assert not (mac.base / "state" / "approved-rev").exists()
    assert not mac.plist.exists()
    assert not (mac.base / "bin").exists()
    assert mac.launchctl_calls("bootstrap") == []


def test_install_with_an_interpreter_too_old_for_the_job_changes_nothing(
    mac: Mac,
) -> None:
    support.write_script(mac.shims / "python3", OLD_PYTHON)

    done = mac.install()

    assert done.returncode == 1
    assert "python3: older than" in done.stdout
    assert "nothing was changed" in done.stderr
    assert mac.installed_paths() == ["fake-login"]
    assert mac.launchctl_calls("bootstrap") == []


def test_install_with_a_missing_tool_changes_nothing(mac: Mac) -> None:
    (mac.shims / "codex").unlink()
    job_path = f"{mac.shims}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    elsewhere = shutil.which("codex", path=job_path)

    done = mac.install()

    # Whether the probe finds the tool depends on the machine; what must hold
    # either way is that its verdict decides.
    if elsewhere is None:
        assert done.returncode == 1
        assert "codex: NOT FOUND" in done.stdout
        assert mac.installed_paths() == ["fake-login"]
        assert mac.launchctl_calls("bootstrap") == []
    else:
        assert f"codex: {elsewhere}" in done.stdout
        assert done.returncode == 0, done.stdout + done.stderr


def test_install_with_another_account_logged_in_changes_nothing(mac: Mac) -> None:
    (mac.home / "fake-login").write_text("someone-else\n", encoding="utf-8")

    done = mac.install()

    assert done.returncode == 1
    assert f"is not {common.FORK_OWNER}" in done.stdout
    assert mac.installed_paths() == ["fake-login"]
    assert mac.launchctl_calls("bootstrap") == []


@pytest.mark.parametrize("marked", [False, True], ids=["unmarked", "other-origin"])
def test_install_refuses_an_existing_repository_that_is_not_the_jobs(
    mac: Mac, marked: bool
) -> None:
    clone = mac.base / "fork"
    clone.mkdir(parents=True)
    subprocess.run([support.REAL_GIT, "init", "--quiet", str(clone)], check=True)
    support.git(clone, "remote", "add", "origin", "https://example.invalid/other.git")
    if marked:
        support.git(clone, "config", common.OWNERSHIP_KEY, "true")

    done = mac.install()

    assert done.returncode == 1
    expected = (
        "is not a clone of" if marked else "was not created by launchd.sh install"
    )
    assert expected in done.stderr
    assert not mac.plist.exists()
    assert mac.launchctl_calls("bootstrap") == []


def scratch_fork(tmp_path: Path) -> support.Fork:
    """A fork of this test's own, carrying the maintenance code, so commits
    can be pushed to it and `launchd.sh` can be run from a checkout of it."""
    return support.make_fork(tmp_path / "fork", with_code=True)


def test_install_shows_what_the_fork_has_that_this_checkout_has_not(
    tmp_path: Path,
) -> None:
    # The owner runs launchd.sh from a checkout they reviewed. The fork has
    # moved on since; one of the new commits tries to redraw the screen.
    fork = scratch_fork(tmp_path)
    checkout = tmp_path / "reviewed-checkout"
    subprocess.run(
        [support.REAL_GIT, "clone", "--quiet", str(fork.bare), str(checkout)],
        check=True,
    )
    reviewed = support.git(checkout, "rev-parse", "HEAD")
    fork.commit({"README.md": "# one\n"}, "docs: one (#11)")
    head = fork.commit(
        {"scripts/extra.sh": "echo extra\n"}, "feat: extra\x1b[2J\x1b[HNothing changed."
    )
    mac = Mac(tmp_path, bare=fork.bare)

    done = mac.run(
        "install",
        answers="yes\nyes\n",
        script=checkout / "scripts" / "maint" / "launchd.sh",
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert mac.approved() == head
    shown = done.stdout.split(f"to let this Mac execute {head}", 1)[0]
    assert f"  {head}\n" in shown
    assert (
        f"2 commits are on the fork that this checkout ({reviewed}) does not have:"
        in shown
    )
    assert "docs: one (#11)" in shown
    assert "feat: extra?[2J?[HNothing changed." in shown
    assert "scripts/extra.sh" in shown  # the changed paths
    assert f"Inspect: git -C {mac.base}/fork diff {reviewed} {head}" in shown
    assert set(done.stdout) <= PRINTABLE


def test_install_approves_the_branch_when_a_tag_on_the_fork_shadows_its_name(
    tmp_path: Path,
) -> None:
    # A tag named `origin/main` on a commit that was never merged and replaces
    # the dispatcher. `origin/main` as a short name would resolve to it.
    fork = scratch_fork(tmp_path)
    side = support.never_merged_commit(
        fork, {"scripts/maint/run.py": "print('attacker code, never merged')\n"}
    )
    support.plant_shadow_tag(fork, side, in_clone=False)
    real = support.git(fork.bare, "rev-parse", "main")
    mac = Mac(tmp_path, bare=fork.bare)

    done = mac.install()

    assert done.returncode == 0, done.stdout + done.stderr
    assert mac.approved() == real != side
    assert side not in done.stdout
    clone = mac.base / "fork"
    assert support.git(clone, "tag", "-l") == ""
    assert support.git(clone, "for-each-ref", "--format=%(refname)") == common.MAIN_REF


def test_install_refuses_a_clone_that_already_holds_a_shadowing_tag(
    tmp_path: Path,
) -> None:
    fork = scratch_fork(tmp_path)
    side = support.never_merged_commit(fork, {"README.md": "# never merged\n"})
    mac = Mac(tmp_path, bare=fork.bare)
    assert (
        mac.install(answers="yes\nno\n").returncode == 1
    )  # clone made, nothing approved
    support.plant_shadow_tag(fork, side, in_clone=False)
    refspec = "refs/tags/origin/main:refs/tags/origin/main"
    support.git(mac.base / "fork", "fetch", "--quiet", str(fork.bare), refspec)

    done = mac.install()

    assert done.returncode == 1
    assert "holds a ref the job never creates" in done.stderr
    assert not (mac.base / "state" / "approved-rev").exists()
    assert mac.launchctl_calls("bootstrap") == []


def test_status_after_install_reports_without_running_unapproved_code(mac: Mac) -> None:
    mac.install()

    done = mac.run("status")

    assert done.returncode == 0, done.stderr
    assert f"agent: {LABEL} loaded" in done.stdout
    assert "plist: matches this checkout" in done.stdout
    assert "launcher: matches this checkout" in done.stdout
    assert f"gh login: {common.FORK_OWNER}" in done.stdout
    assert "not at the approved revision yet" in done.stdout
    assert "LAUNCHER FAILURE" not in done.stdout


def test_status_shows_a_launcher_failure_first_and_safely(mac: Mac) -> None:
    mac.install()
    record = mac.base / "state" / "launcher-failure"
    record.write_text(
        "20261119T084000Z orchestra-maint: cannot fetch the fork\x1b[2J. Next: retry\n",
        encoding="utf-8",
    )

    done = mac.run("status")

    assert done.returncode == 0
    assert done.stdout.startswith("LAUNCHER FAILURE")
    assert "cannot fetch the fork?[2J. Next: retry" in done.stdout
    assert "\x1b" not in done.stdout


def test_status_reports_a_launcher_that_drifted(mac: Mac) -> None:
    mac.install()
    launcher = mac.base / "bin" / "orchestra-maint"
    launcher.write_text(
        launcher.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8"
    )

    done = mac.run("status")

    assert "launcher: DIFFERS" in done.stdout


def test_status_before_install_says_so_and_creates_nothing(mac: Mac) -> None:
    done = mac.run("status")

    assert done.returncode == 0
    assert "NOT loaded" in done.stdout
    assert "plist: not installed" in done.stdout
    assert "launcher: not installed" in done.stdout
    assert mac.installed_paths() == ["fake-login"]


def test_run_now_starts_the_loaded_agent_only(mac: Mac) -> None:
    refused = mac.run("run-now")
    mac.install()

    started = mac.run("run-now")

    assert refused.returncode == 1
    assert "is not loaded" in refused.stderr
    assert started.returncode == 0
    assert len(mac.launchctl_calls("kickstart")) == 1
    assert mac.launchctl_calls("kickstart")[0].endswith(f"/{LABEL}")


def test_promote_before_install_is_refused(mac: Mac) -> None:
    done = mac.run("promote", answers="yes\n")

    assert done.returncode == 1
    assert "not installed" in done.stderr


def test_promote_goes_through_the_installed_launcher(mac: Mac, tmp_path: Path) -> None:
    # The launcher is what fetches and checks out the approved revision; the
    # clone's code is never started directly.
    record = tmp_path / "launcher-called"
    stub = mac.base / "bin" / "orchestra-maint"
    stub.parent.mkdir(parents=True)
    support.write_script(
        stub,
        f'#!/bin/bash\necho "$* home=$ORCHESTRA_MAINT_HOME tty=$([ -t 0 ] && echo yes)"'
        f' > "{record}"\nexit 7\n',
    )
    clone_code = mac.base / "fork" / "scripts" / "maint" / "run.py"
    clone_code.parent.mkdir(parents=True)
    clone_code.write_text("raise SystemExit('started directly')\n", encoding="utf-8")

    done = mac.run("promote", answers="yes\n")

    assert done.returncode == 7
    assert record.read_text(encoding="utf-8") == f"promote home={mac.base} tty=yes\n"


def test_uninstall_removes_the_agent_and_keeps_the_state(mac: Mac) -> None:
    mac.install()
    approved = mac.approved()

    done = mac.run("uninstall")

    assert done.returncode == 0, done.stderr
    assert len(mac.launchctl_calls("bootout")) == 1
    assert not mac.plist.exists()
    assert not (mac.base / "bin").exists()
    assert (mac.base / "fork" / ".git").is_dir()
    assert mac.approved() == approved
    assert f"{mac.base}/fork" in done.stdout  # what was kept is listed

    again = mac.run("uninstall")
    assert again.returncode == 0
    assert "was not loaded" in again.stdout


def test_install_under_a_hostile_git_configuration_runs_none_of_it(
    mac: Mac, tmp_path: Path
) -> None:
    # The owner's own git setup: a hook directory, a rewrite rule for the
    # fork's URL, injected settings. None of it may run or redirect.
    marker = tmp_path / "PWNED"
    evil = tmp_path / "evil.git"
    subprocess.run(
        [support.REAL_GIT, "init", "--quiet", "--bare", str(evil)], check=True
    )
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "post-checkout", "post-merge"):
        support.write_script(hooks / name, f'#!/bin/sh\necho "{name}" >> "{marker}"\n')
    config = mac.home / ".gitconfig"
    config.write_text(
        f"[core]\n\thooksPath = {hooks}\n"
        f'[url "{evil}"]\n\tinsteadOf = {common.FORK_GIT_URL}\n'
        f"[init]\n\ttemplateDir = {hooks}\n",
        encoding="utf-8",
    )
    mac.env.update(
        {
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks),
            "GIT_TEMPLATE_DIR": str(hooks),
        }
    )

    done = mac.install()

    assert done.returncode == 0, done.stdout + done.stderr
    assert mac.approved() == support.git(mac.bare, "rev-parse", "main")
    assert not marker.exists()
    assert support.git(evil, "for-each-ref") == ""
    installed_hooks = mac.base / "fork" / ".git" / "hooks"
    assert not (installed_hooks / "reference-transaction").exists()


def test_install_says_how_much_of_a_long_change_it_does_not_show(
    tmp_path: Path,
) -> None:
    # More commits and more changed paths than the screen lists, and one
    # subject longer than a line: each cut must be announced, with the
    # command that shows everything.
    fork = scratch_fork(tmp_path)
    checkout = tmp_path / "reviewed-checkout"
    subprocess.run(
        [support.REAL_GIT, "clone", "--quiet", str(fork.bare), str(checkout)],
        check=True,
    )
    reviewed = support.git(checkout, "rev-parse", "HEAD")
    for index in range(34):
        (fork.seed / "README.md").write_text(f"# step {index}\n", encoding="utf-8")
        support.git(fork.seed, "commit", "--quiet", "-am", f"docs: step {index}")
    for index in range(45):
        (fork.seed / "docs").mkdir(exist_ok=True)
        (fork.seed / "docs" / f"page-{index:02d}.md").write_text(
            "x\n", encoding="utf-8"
        )
    support.git(fork.seed, "add", "-A")
    support.git(fork.seed, "commit", "--quiet", "-m", "docs: " + "many pages " * 40)
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    head = support.git(fork.seed, "rev-parse", "HEAD")
    mac = Mac(tmp_path, bare=fork.bare)
    clone = mac.base / "fork"

    done = mac.run(
        "install",
        answers="yes\nyes\n",
        script=checkout / "scripts" / "maint" / "launchd.sh",
    )

    assert done.returncode == 0, done.stdout + done.stderr
    shown = done.stdout
    assert "35 commits are on the fork that this checkout" in shown
    assert (
        f"(5 more lines are not shown; all of them: git -C {clone} log {reviewed}..{head})"
        in shown
    )
    # 45 pages and the README, plus the summary line: 47 lines, 40 shown.
    assert (
        "(7 more lines are not shown; all of them: "
        f"git -C {clone} diff --stat {reviewed} {head})" in shown
    )
    assert " [line cut]" in shown  # the long subject, cut and marked


def test_install_fails_closed_when_the_refs_cannot_be_listed(mac: Mac) -> None:
    # A list that cannot be read must not pass for a list with nothing wrong.
    (mac.fakebin / "git.fail").write_text("for-each-ref", encoding="utf-8")

    done = mac.install()

    assert done.returncode == 1
    assert "cannot list the refs" in done.stderr
    assert not (mac.base / "state" / "approved-rev").exists()
    assert not mac.plist.exists()
    assert mac.launchctl_calls("bootstrap") == []
