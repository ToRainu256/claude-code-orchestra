"""The distribution step: which projects it writes into, what, and when.

Every target here is a real git repository in a temp directory with the
template put there by the real installer. The fork carries the template this
checkout ships and the real ``scripts/update.sh``; the job's clone, state root
and home are scratch. Nothing reaches the network or the real home directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import maint_support as support
import pytest
from maint_support import (
    adopt,
    common,
    distribute,
    ledger,
    maint_run,
    report,
    staging,
    target_git,
    targets,
)

RULE_REL = ".claude/rules/testing.md"
RULE_TEXT = "# Testing rules, as the approved revision has them\n"
NEWER_TEXT = "# Testing rules from a main nobody approved\n"
OWNER_TEXT = "# Testing rules, as this project's owner rewrote them\n"
STAMP_REL = ".claude/orchestra-version"
MANIFEST_REL = ".claude/skills/_shared/models.json"
CONFIG_REL = ".codex/config.toml"
OVERRIDE_REL = ".claude/models.local.json"
RELEASE = {RULE_REL: RULE_TEXT, "VERSION": "9.9.9\n"}
PRINTABLE = {chr(code) for code in range(0x20, 0x7F)} | {"\n", "\t"}
ESCAPE = "\x1b"
HOSTILE_NAME = f"x{ESCAPE}[2J‮gnp.md"
HOOKS = (
    "post-index-change",
    "pre-commit",
    "post-checkout",
    "post-merge",
    "reference-transaction",
    "fsmonitor-watchman",
)
WRITING_COMMANDS = (
    "add",
    "commit",
    "stash",
    "reset",
    "checkout",
    "switch",
    "restore",
    "rm",
    "mv",
    "clean",
    "merge",
    "rebase",
    "update-index",
    "read-tree",
    "update-ref",
    "gc",
    "fetch",
)
LAUNCHD = support.MAINT_DIR / "launchd.sh"


@pytest.fixture
def fork(tmp_path: Path) -> support.Fork:
    return support.make_fork(tmp_path, real_template=True)


def list_in_registry(fork: support.Fork, line: str) -> None:
    registry = distribute.registry_path(fork.paths)
    with registry.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def make_project(fork: support.Fork, name: str = "alpha") -> Path:
    """A clean installed project, listed in the registry."""
    project = support.make_project(fork.root / "projects" / name)
    list_in_registry(fork, str(project))
    return project


def release(fork: support.Fork, files: dict[str, str | None], message: str) -> str:
    """Merge a change on the fork and approve it."""
    sha = fork.commit(files, message)
    fork.fetch()
    fork.approve(sha)
    return sha


def adopt_project(
    fork: support.Fork, project: Path | str, answer: str = "yes"
) -> tuple[int, str, list[str]]:
    """Run the owner's command the way a terminal would: (exit code, what was
    printed, the prompts that were asked)."""
    said: list[str] = []
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    code = adopt.run(fork.context(), str(project), ask, said.append)
    return code, "\n".join(said), prompts


def porcelain(project: Path) -> list[str]:
    """`git status --porcelain`, every line as git wrote it."""
    done = subprocess.run(
        [support.REAL_GIT, "-C", str(project), "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in done.stdout.split("\n") if line]


def commit_all(project: Path, message: str = "owner commits") -> str:
    support.git(project, "add", "-A")
    support.git(project, "commit", "--quiet", "-m", message)
    return support.git(project, "rev-parse", "HEAD")


def adopted_project(fork: support.Fork, name: str = "alpha") -> Path:
    """A project the owner adopted, clean again afterwards."""
    project = make_project(fork, name)
    code, said, _ = adopt_project(fork, project)
    assert code == common.EXIT_OK, said
    if porcelain(project):
        commit_all(project)
    return project


def record(fork: support.Fork) -> dict:
    path = fork.paths.state_dir / distribute.LEDGER_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def states(fork: support.Fork) -> dict[str, str]:
    """directory name -> state, from the record of the last run."""
    return {Path(row["target"]).name: row["state"] for row in record(fork)["last"]}


def detail_of(fork: support.Fork, name: str) -> str:
    rows = [row for row in record(fork)["last"] if Path(row["target"]).name == name]
    return rows[0]["detail"]


def git_bytes(project: Path) -> dict[str, bytes]:
    """The repository's own state: HEAD, the index and every ref."""
    refs = support.git(project, "for-each-ref", "--format=%(objectname) %(refname)")
    return {
        "HEAD": (project / ".git" / "HEAD").read_bytes(),
        "index": (project / ".git" / "index").read_bytes(),
        "refs": refs.encode(),
    }


def plant_tripwires(project: Path, marker: Path) -> None:
    """Hooks and a file-system monitor that leave a file behind when they
    run. Both are things a plain `git status` in the project would start."""
    for name in HOOKS:
        hook = project / ".git" / "hooks" / name
        hook.parent.mkdir(exist_ok=True)
        support.write_script(hook, f"#!/bin/sh\ntouch '{marker}.hook-{name}'\n")
    support.git(project, "config", "core.fsmonitor", f"touch '{marker}.monitor'; true")


def tripped(marker: Path) -> list[str]:
    return sorted(path.name for path in marker.parent.glob(f"{marker.name}*"))


def is_printable(text: str) -> bool:
    return set(text) <= PRINTABLE


# --------------------------------------------------------------------------
# The three guarantees: approved only, clean only, never a commit
# --------------------------------------------------------------------------


def test_run_with_an_adopted_clean_target_leaves_the_approved_files_uncommitted(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    head, before = support.git(project, "rev-parse", "HEAD"), git_bytes(project)
    release(fork, RELEASE, "change a rule")

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("distributed", common.EXIT_OK)
    assert states(fork) == {"alpha": "updated"}
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert (project / STAMP_REL).read_text(encoding="utf-8") == "9.9.9\n"
    # Nothing was committed, staged, stashed or checked out: the update is
    # there as plain unstaged changes, and the index is the same bytes.
    assert support.git(project, "rev-parse", "HEAD") == head
    assert git_bytes(project) == before
    assert porcelain(project) == [f" M {STAMP_REL}", f" M {RULE_REL}"]


@pytest.mark.parametrize("kind", ["staged", "staged-new", "unstaged", "untracked"])
def test_run_with_a_dirty_target_skips_it_and_leaves_every_byte(
    fork: support.Fork, kind: str
) -> None:
    project = adopted_project(fork)
    if kind in ("untracked", "staged-new"):
        (project / "notes.txt").write_text("not committed\n", encoding="utf-8")
    else:
        (project / "README.md").write_text("# edited\n", encoding="utf-8")
    if kind.startswith("staged"):
        support.git(project, "add", "-A")
    approved = release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    first = distribute.run(fork.context(now=1000.0))
    second = distribute.run(fork.context(now=9000.0))

    # Waiting is not an error, and it says which revision waits since when.
    for result in (first, second):
        assert (result.result, result.exit_code) == ("waiting", common.EXIT_OK)
        assert result.counts == {"targets": 1, "waiting-dirty": 1}
    assert support.tree_digest(project) == before
    row = record(fork)["last"][0]
    assert (row["state"], row["pending"], row["since"]) == (
        "waiting-dirty",
        approved,
        1000.0,
    )
    assert ("1 untracked" in row["detail"]) == (kind == "untracked")
    assert ("1 modified or staged" in row["detail"]) == (kind != "untracked")


def test_run_updates_a_waiting_target_once_its_owner_commits(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    (project / "README.md").write_text("# work in progress\n", encoding="utf-8")
    release(fork, RELEASE, "change a rule")
    waiting = distribute.run(fork.context())
    commit_all(project, "finish the work")

    result = distribute.run(fork.context())

    assert waiting.result == "waiting"
    assert (result.result, result.exit_code) == ("distributed", common.EXIT_OK)
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT


def test_run_after_an_update_reports_it_uncommitted_and_holds_the_next_revision(
    fork: support.Fork,
) -> None:
    # The consequence of never committing: the project is dirty with the
    # job's own update until its owner commits, and the next revision waits.
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    distribute.run(fork.context())
    held = support.tree_digest(project)

    same_revision = distribute.run(fork.context())
    after_same = states(fork)
    following = release(fork, {RULE_REL: NEWER_TEXT}, "change it again")
    next_revision = distribute.run(fork.context(now=2000.0))

    assert same_revision.exit_code == common.EXIT_OK
    assert after_same == {"alpha": "uncommitted"}
    assert (next_revision.result, next_revision.exit_code) == (
        "waiting",
        common.EXIT_OK,
    )
    row = record(fork)["last"][0]
    assert (row["state"], row["pending"], row["since"]) == (
        "waiting-dirty",
        following,
        2000.0,
    )
    assert support.tree_digest(project) == held


def test_run_never_touches_a_target_that_was_not_adopted(
    fork: support.Fork, tmp_path: Path
) -> None:
    project = make_project(fork)
    marker = tmp_path / "tripwire"
    plant_tripwires(project, marker)
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    # Counted and listed with the hint, and not an alarm: the registry is
    # filled by other tooling, and adopting is the owner's standing choice.
    assert (result.result, result.exit_code) == ("none-adopted", common.EXIT_OK)
    assert result.counts == {"targets": 1, "not-adopted": 1}
    assert states(fork) == {"alpha": "not-adopted"}
    assert "launchd.sh adopt" in detail_of(fork, "alpha")
    assert support.tree_digest(project) == before
    assert tripped(marker) == []


def test_run_with_a_target_at_the_approved_revision_writes_nothing(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    distribute.run(fork.context())
    commit_all(project, "take the update")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("up-to-date", common.EXIT_OK)
    assert states(fork) == {"alpha": "up-to-date"}
    assert support.tree_digest(project) == before


def test_run_after_a_promotion_that_ships_nothing_new_writes_nothing(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    before = support.tree_digest(project)
    approved = release(fork, {"README.md": "# only the fork's readme\n"}, "inert")

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("up-to-date", common.EXIT_OK)
    assert support.tree_digest(project) == before
    assert record(fork)["adopted"][str(project)]["revision"] == approved


def test_run_with_a_newer_unapproved_main_distributes_the_approved_revision(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    newer = fork.commit({RULE_REL: NEWER_TEXT, "VERSION": "10.0.0\n"}, "not approved")
    fork.fetch()
    # Even the clone's own work tree sitting at the newer commit changes
    # nothing: the source is laid out from the approved SHA.
    support.git(fork.paths.clone, "checkout", "--quiet", "--force", "--detach", newer)

    result = distribute.run(fork.context())

    assert result.result == "distributed"
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert (project / STAMP_REL).read_text(encoding="utf-8") == "9.9.9\n"


def test_run_brings_a_model_bump_into_the_projects_own_settings(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    manifest = json.loads((fork.seed / MANIFEST_REL).read_text(encoding="utf-8"))
    current = common.parse_slug(manifest["codex"]["model"])
    assert current is not None
    bumped = f"{current.vendor}-{current.version[0] + 1}.0-{current.family}"
    sha = fork.bump(bumped)
    fork.fetch()
    fork.approve(sha)

    result = distribute.run(fork.context())

    assert result.result == "distributed"
    config = (project / CONFIG_REL).read_text(encoding="utf-8")
    assert f'model = "{bumped}"' in config
    assert f'model = "{current.text}"' not in config
    assert porcelain(project) == [f" M {MANIFEST_REL}", f" M {CONFIG_REL}"]


@pytest.mark.parametrize("command", WRITING_COMMANDS)
def test_target_git_refuses_every_command_that_is_not_a_reading_one(
    fork: support.Fork, command: str
) -> None:
    project = make_project(fork)
    before = support.tree_digest(project)
    git = target_git.TargetGit(fork.context(), str(project))

    with pytest.raises(common.Refused, match="not a reading command"):
        git.run([command, "--all"])

    assert support.tree_digest(project) == before


@pytest.mark.parametrize(
    "sneaked",
    [
        ["commit", "--quiet", "--allow-empty", "-m", "sneaked"],
        ["add", "-A"],
        ["branch", "sneaked"],
    ],
    ids=["a-commit", "the-index-alone", "a-ref-alone"],
)
def test_run_fails_the_target_when_its_git_state_moved_during_the_update(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch, sneaked: list[str]
) -> None:
    # Nothing the job runs can commit, stage or create a ref. Should that
    # ever stop being true, the comparison of HEAD, refs and index says so.
    adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    real_apply = targets.apply_plan

    def apply_and_sneak(plan: targets.Plan) -> None:
        real_apply(plan)
        support.git(Path(plan.target), *sneaked)

    monkeypatch.setattr(targets, "apply_plan", apply_and_sneak)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert "HEAD, refs or index changed" in detail_of(fork, "alpha")
    monkeypatch.undo()
    again = distribute.run(fork.context())
    assert (again.result, states(fork)) == ("needs-owner", {"alpha": "local-changes"})
    assert "stopped half-way" in detail_of(fork, "alpha")
    assert "discard or commit the half-written changes" in detail_of(fork, "alpha")


# --------------------------------------------------------------------------
# Only the job's own output is replaced
# --------------------------------------------------------------------------


def test_run_with_a_template_file_the_owner_rewrote_leaves_it_and_asks(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    (project / RULE_REL).write_text(OWNER_TEXT, encoding="utf-8")
    commit_all(project, "our own testing rules")
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == (
        "needs-owner",
        common.EXIT_NEEDS_OWNER,
    )
    assert states(fork) == {"alpha": "local-changes"}
    assert support.tree_digest(project) == before


def test_adopt_names_every_file_git_could_not_bring_back(fork: support.Fork) -> None:
    # A skill the owner added and keeps out of git. The directory swap would
    # delete it, and nothing could restore it: it gets its own line.
    project = adopted_project(fork)
    own = project / ".claude" / "skills" / "own-skill" / "SKILL.md"
    own.parent.mkdir()
    own.write_text("# ours\n", encoding="utf-8")
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".claude/skills/own-skill/\n", encoding="utf-8")
    release(fork, RELEASE, "change a rule")
    scheduled = distribute.run(fork.context())
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project, answer="no")

    assert states(fork) == {"alpha": "local-changes"}
    assert scheduled.exit_code == common.EXIT_NEEDS_OWNER
    assert code == common.EXIT_OK
    assert "Git cannot bring back the present content of 1 of the files" in said
    assert "  delete  .claude/skills/own-skill/SKILL.md  (untracked)" in said
    assert len(prompts) == 1 and "Type yes" in prompts[0]
    assert "not confirmed; nothing was changed" in said
    assert support.tree_digest(project) == before


def test_run_with_a_retired_path_that_came_back_leaves_it_to_an_attended_update(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    retired = project / ".gemini" / "settings.json"
    retired.parent.mkdir()
    retired.write_text("{}\n", encoding="utf-8")
    commit_all(project, "bring an old directory back")
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "local-changes"}
    assert "would also delete .gemini/settings.json" in detail_of(fork, "alpha")
    assert support.tree_digest(project) == before


@pytest.mark.parametrize("meanwhile", ["an-edit", "an-untracked-file", "a-commit"])
def test_run_asks_git_again_right_before_the_write_and_writes_nothing_if_it_moved(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch, meanwhile: str
) -> None:
    # The tree was clean when the run looked. Preparing an update takes a
    # while; what the owner does in that while must not be written over.
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")

    def owner_works_meanwhile(*_arguments: object) -> None:
        if meanwhile == "an-edit":
            (project / "README.md").write_text("# edited\n", encoding="utf-8")
        elif meanwhile == "an-untracked-file":
            (project / "notes.txt").write_text("new\n", encoding="utf-8")
        else:
            support.git(project, "commit", "--quiet", "--allow-empty", "-m", "work")

    monkeypatch.setattr(distribute, "unattended_problem", owner_works_meanwhile)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("waiting", common.EXIT_OK)
    assert states(fork) == {"alpha": "busy"}
    assert "nothing was written" in detail_of(fork, "alpha")
    assert (project / RULE_REL).read_text(encoding="utf-8") != RULE_TEXT
    assert record(fork)["adopted"][str(project)]["interrupted"] == ""


@pytest.mark.parametrize("replaced", ["both", "the-repository", "the-directory"])
def test_run_with_another_repository_at_an_adopted_path_does_not_touch_it(
    fork: support.Fork, replaced: str
) -> None:
    project = adopted_project(fork)
    away = project.with_name("moved-away")
    if replaced == "both":
        shutil.move(str(project), str(away))
        support.make_project(project)
    elif replaced == "the-repository":
        # The same directory, holding another clone's repository.
        other = support.make_project(project.with_name("another-clone"))
        shutil.move(str(project / ".git"), str(away))
        shutil.copytree(other / ".git", project / ".git", symlinks=True)
    else:
        # The same repository, moved into a directory made anew.
        shutil.move(str(project), str(away))
        project.mkdir()
        for child in sorted(away.iterdir()):
            shutil.move(str(child), str(project / child.name))
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "refused"}
    assert "another directory or repository" in detail_of(fork, "alpha")
    assert support.tree_digest(project) == before


# --------------------------------------------------------------------------
# A hostile target
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "no-repository", "gitfile", "link"])
def test_run_with_a_target_that_is_not_a_plain_repository_refuses_it(
    fork: support.Fork, kind: str
) -> None:
    project = adopted_project(fork)
    elsewhere = project.with_name("elsewhere")
    if kind in ("no-repository", "gitfile"):
        shutil.move(str(project / ".git"), str(elsewhere))
    else:
        shutil.move(str(project), str(elsewhere))
    if kind == "gitfile":
        # git would follow this into the repository it names.
        (project / ".git").write_text(f"gitdir: {elsewhere}\n", encoding="utf-8")
    if kind == "link":
        project.symlink_to(elsewhere)
    release(fork, RELEASE, "change a rule")
    survivor = project if kind in ("no-repository", "gitfile") else elsewhere
    before = support.tree_digest(survivor)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == (
        "needs-owner",
        common.EXIT_NEEDS_OWNER,
    )
    assert states(fork) == {"alpha": "refused"}
    assert support.tree_digest(survivor) == before


def test_run_with_hostile_hooks_and_a_monitor_runs_none_of_them(
    fork: support.Fork, tmp_path: Path
) -> None:
    project = make_project(fork)
    marker = tmp_path / "tripwire"
    plant_tripwires(project, marker)
    # The tripwires are live: git as a person would run it sets them off.
    os.utime(project / "README.md", (1, 1))
    subprocess.run([support.REAL_GIT, "-C", str(project), "status"], check=True)
    assert tripped(marker) != []
    for path in marker.parent.glob(f"{marker.name}*"):
        path.unlink()
    os.utime(project / "README.md", (2, 2))

    code, said, _ = adopt_project(fork, project)
    after_adopt = tripped(marker)
    if porcelain_without_hooks(project):
        commit_without_hooks(project)
    release(fork, RELEASE, "change a rule")
    os.utime(project / "README.md", (3, 3))
    result = distribute.run(fork.context())
    after_run = tripped(marker)
    # The hooks are real ones: the owner's own commit starts them.
    support.git(project, "commit", "--quiet", "--allow-empty", "-m", "the owner's")

    assert code == common.EXIT_OK, said
    assert result.result == "distributed"
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert (after_adopt, after_run) == ([], [])
    assert "tripwire.hook-pre-commit" in tripped(marker)


def porcelain_without_hooks(project: Path) -> str:
    return support.git(
        project, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "status", "--porcelain",
    )  # fmt: skip


def commit_without_hooks(project: Path) -> None:
    quiet = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
    support.git(project, *quiet, "add", "-A")
    support.git(project, *quiet, "commit", "--quiet", "-m", "owner commits")


def test_run_with_a_filter_in_the_targets_configuration_refuses_it(
    fork: support.Fork, tmp_path: Path
) -> None:
    project = adopted_project(fork)
    marker = tmp_path / "tripwire"
    (project / ".gitattributes").write_text("*.md filter=evil\n", encoding="utf-8")
    commit_all(project, "route files through a filter")
    support.git(project, "config", "filter.evil.clean", f"touch '{marker}'; cat")
    # Same size, another time: git has to read the file to compare it, and
    # reading it would go through the filter.
    os.utime(project / "README.md", (1, 1))
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "refused"}
    assert "filter.evil.clean" in detail_of(fork, "alpha")
    assert tripped(marker) == []
    assert support.tree_digest(project) == before


@pytest.mark.parametrize(
    "key",
    [
        "core.bare",
        "core.hookspath",
        "core.fsmonitor",
        "remote.origin.url",
        "remote.origin.fetch",
        "branch.main.merge",
        "branch.feature/x.y.remote",
        "user.email",
        "submodule.vendored.url",
        "lfs.repositoryformatversion",
        "extensions.worktreeconfig",
    ],
)
def test_config_key_is_inert_accepts_what_no_reading_command_acts_on(key: str) -> None:
    assert target_git.config_key_is_inert(key)


@pytest.mark.parametrize(
    "key",
    [
        "filter.lfs.clean",
        "filter.evil.process",
        "diff.external",
        "diff.pdf.textconv",
        "merge.ours.driver",
        "include.path",
        "includeif.gitdir:/x/.path",
        "core.worktree",
        "core.sshcommand",
        "core.pager",
        "core.attributesfile",
        "core.alternaterefscommand",
        "extensions.partialclone",
        "remote.origin.promisor",
        "remote.origin.vcs",
        "protocol.ext.allow",
        "url.ext::sh -c x.insteadof",
        "credential.helper",
        "alias.status",
        "http.proxy",
        "never.heard-of-it",
        "core",
    ],
)
def test_config_key_is_inert_refuses_everything_else(key: str) -> None:
    assert not target_git.config_key_is_inert(key)


def test_adopt_with_a_template_directory_that_is_a_link_refuses(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    outside = fork.root / "shared-skills"
    shutil.move(str(project / ".claude" / "skills"), str(outside))
    (project / ".claude" / "skills").symlink_to(outside)
    commit_all(project, "share the skills directory")
    before, shared = support.tree_digest(project), support.tree_digest(outside)

    code, said, prompts = adopt_project(fork, project)

    assert code == common.EXIT_NEEDS_OWNER
    assert "has a link at .claude/skills" in said
    assert prompts == []
    assert support.tree_digest(project) == before
    assert support.tree_digest(outside) == shared


def test_apply_changes_never_writes_through_a_link_on_the_way(tmp_path: Path) -> None:
    target, stage, outside = tmp_path / "target", tmp_path / "stage", tmp_path / "out"
    for directory in (target, stage / "docs", outside):
        directory.mkdir(parents=True)
    (stage / "docs" / "new.md").write_text("new\n", encoding="utf-8")
    (target / "docs").symlink_to(outside)
    before = {"docs": targets.Entry(targets.KIND_DIR)}
    after = targets.snapshot(str(stage))

    with pytest.raises(targets.Conflict):
        targets.apply_changes(str(target), str(stage), before, after)

    assert list(outside.iterdir()) == []


def test_apply_changes_with_a_path_that_changed_since_writes_nothing(
    tmp_path: Path,
) -> None:
    target, stage = tmp_path / "target", tmp_path / "stage"
    for directory, texts in ((target, ("one\n", "two\n")), (stage, ("ONE\n", "TWO\n"))):
        directory.mkdir()
        (directory / "a.md").write_text(texts[0], encoding="utf-8")
        (directory / "b.md").write_text(texts[1], encoding="utf-8")
    before, after = targets.snapshot(str(target)), targets.snapshot(str(stage))
    (target / "b.md").write_text("somebody else was here\n", encoding="utf-8")

    with pytest.raises(targets.Conflict, match="b.md"):
        targets.apply_changes(str(target), str(stage), before, after)

    assert (target / "a.md").read_text(encoding="utf-8") == "one\n"


def test_apply_changes_writes_normal_modes_whatever_the_umask(tmp_path: Path) -> None:
    # The launcher runs with umask 077; a project's files must not turn private.
    target, stage = tmp_path / "target", tmp_path / "stage"
    (stage / "new" / "deep").mkdir(parents=True)
    target.mkdir()
    for rel, text, mode in (
        ("new/deep/tool.sh", "#!/bin/sh\n", 0o700),
        ("new/note.md", "note\n", 0o600),
        ("kept.md", "new\n", 0o600),
    ):
        (stage / rel).write_text(text, encoding="utf-8")
        (stage / rel).chmod(mode)
    (target / "kept.md").write_text("old\n", encoding="utf-8")
    (target / "kept.md").chmod(0o640)
    before, after = targets.snapshot(str(target)), targets.snapshot(str(stage))
    previous = os.umask(0o077)

    try:
        targets.apply_changes(str(target), str(stage), before, after)
    finally:
        os.umask(previous)

    modes = {
        rel: stat.S_IMODE((target / rel).stat().st_mode)
        for rel in ("new", "new/deep", "new/deep/tool.sh", "new/note.md", "kept.md")
    }
    assert modes == {
        "new": 0o755,
        "new/deep": 0o755,
        "new/deep/tool.sh": 0o755,
        "new/note.md": 0o644,
        "kept.md": 0o640,
    }
    assert (target / "kept.md").read_text(encoding="utf-8") == "new\n"
    assert targets.applied_problem(str(target), before, after) is None
    assert not list(target.rglob(f"{targets.TEMP_PREFIX}*"))


def test_fingerprint_ignores_what_appears_by_itself_and_the_rendered_model_line(
    tmp_path: Path,
) -> None:
    dirs, files, stamp = (".claude/agents", ".claude/rules"), ("AGENTS.md",), "stamp"
    agent = tmp_path / ".claude" / "agents" / "deep.md"
    rule = tmp_path / ".claude" / "rules" / "rule.md"
    for path, text in (
        (agent, "---\nname: deep\nmodel: one\n---\nbody\n"),
        (rule, "a rule\n"),
        (tmp_path / "AGENTS.md", "contract\n"),
        (tmp_path / stamp, "1.0.0\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def digest(lists: tuple[tuple[str, ...], tuple[str, ...]] = (dirs, files)) -> str:
        return targets.fingerprint(str(tmp_path), lists[0], lists[1], stamp)

    start = digest()
    agent.write_text("---\nname: deep\nmodel: two\n---\nbody\n", encoding="utf-8")
    (rule.parent / "__pycache__").mkdir()
    (rule.parent / "__pycache__" / "rule.cpython-312.pyc").write_bytes(b"\0")
    (rule.parent / ".DS_Store").write_bytes(b"\0")
    same = digest()
    seen = {start}
    for change in (
        lambda: agent.write_text("---\nname: deep\nmodel: two\n---\nBODY\n"),
        lambda: (rule.parent / "added.md").write_text("added\n"),
        lambda: rule.chmod(0o755),
        lambda: (tmp_path / stamp).write_text("1.0.1\n"),
        lambda: (tmp_path / "AGENTS.md").unlink(),
    ):
        change()
        seen.add(digest())
    seen.add(digest(((*dirs, ".claude/hooks"), files)))

    assert same == start
    assert len(seen) == 7


def test_control_characters_in_a_path_or_a_file_name_never_reach_the_terminal(
    fork: support.Fork,
) -> None:
    project = make_project(fork, f"be{ESCAPE}[2Jta")
    (project / ".claude" / "rules" / HOSTILE_NAME).write_text("x\n", encoding="utf-8")
    commit_all(project, "a file name that redraws the screen")
    (project / f"dirty{ESCAPE}[1A").write_text("x\n", encoding="utf-8")

    refused_code, refused, _ = adopt_project(fork, project)
    (project / f"dirty{ESCAPE}[1A").unlink()
    code, said, prompts = adopt_project(fork, project)
    (project / "README.md").write_text("# dirty again\n", encoding="utf-8")
    (project / f"dirty{ESCAPE}[1A").write_text("x\n", encoding="utf-8")
    release(fork, RELEASE, "change a rule")
    result = distribute.run(fork.context())

    assert refused_code == common.EXIT_NEEDS_OWNER
    assert code == common.EXIT_OK, said
    # The names are there, as visible escapes.
    assert "\\x1b[2Jta" in said and "\\x1b[1A" in refused
    listing = next(fork.paths.logs_dir.glob("*.adopt.log")).read_text(encoding="utf-8")
    assert "x\\x1b[2J\\u202egnp.md" in listing
    printed = [
        refused,
        said,
        *prompts,
        listing,
        fork.log_file.read_text(encoding="utf-8"),
        common.one_line(f"{result.name}: {result.result} {result.detail}"),
        *[common.one_line(line) for line in ledger.status_lines(fork.paths)],
    ]
    assert result.result == "waiting"
    assert "\\x1b[2J" in record(fork)["last"][0]["detail"]
    for text in [*printed, record(fork)["last"][0]["detail"]]:
        assert is_printable(text), text


# --------------------------------------------------------------------------
# The owner's command
# --------------------------------------------------------------------------


def test_adopt_shows_what_would_change_and_updates_on_yes(fork: support.Fork) -> None:
    project = make_project(fork)
    approved = release(fork, RELEASE, "change a rule")
    before = git_bytes(project)

    code, said, prompts = adopt_project(fork, project)

    assert code == common.EXIT_OK, said
    assert f"Project:   {project}" in said
    assert f"Revision:  {approved} (template 9.9.9)" in said
    assert "Tree:      clean, at " in said
    assert "  change     1  .claude/rules" in said
    assert "  change     1  .claude/orchestra-version" in said
    assert "Nothing is committed, staged or stashed." in said
    assert prompts == [
        f"Type yes to update this project to {approved[:12]} and let scheduled "
        "runs keep it updated: "
    ]
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert git_bytes(project) == before
    assert porcelain(project) == [f" M {STAMP_REL}", f" M {RULE_REL}"]
    adoption = record(fork)["adopted"][str(project)]
    assert (adoption["revision"], adoption["interrupted"]) == (approved, "")


@pytest.mark.parametrize("answer", ["", "no", "y", "YES", "yes please"])
def test_adopt_with_any_other_answer_changes_and_records_nothing(
    fork: support.Fork, answer: str
) -> None:
    project = make_project(fork)
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project, answer=answer)
    scheduled = distribute.run(fork.context())

    assert code == common.EXIT_OK
    assert len(prompts) == 1
    assert "not confirmed; nothing was changed" in said
    assert support.tree_digest(project) == before
    assert states(fork) == {"alpha": "not-adopted"}
    assert scheduled.exit_code == common.EXIT_OK


def test_adopt_with_a_dirty_tree_lists_the_paths_and_does_not_ask(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    (project / "notes.txt").write_text("not committed\n", encoding="utf-8")
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project)

    assert code == common.EXIT_NEEDS_OWNER
    assert "has 1 uncommitted paths" in said and "?? notes.txt" in said
    assert prompts == []
    assert support.tree_digest(project) == before


def test_adopt_with_a_path_the_registry_does_not_list_refuses(
    fork: support.Fork,
) -> None:
    project = support.make_project(fork.root / "projects" / "unlisted")
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project)

    assert code == common.EXIT_USAGE
    assert "is not listed in the registry" in said
    assert prompts == []
    assert support.tree_digest(project) == before


def test_adopt_with_a_project_the_template_was_never_installed_in_refuses(
    fork: support.Fork,
) -> None:
    # The updater would install it wholesale, over whatever Claude and Codex
    # configuration the project has. That is the installer's question to ask.
    project = fork.root / "projects" / "plain"
    project.mkdir(parents=True)
    subprocess.run([support.REAL_GIT, "init", "--quiet", str(project)], check=True)
    (project / "README.md").write_text("# no template here\n", encoding="utf-8")
    commit_all(project, "a project")
    list_in_registry(fork, str(project))
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project)

    assert code == common.EXIT_NEEDS_OWNER
    assert "the template is not installed there" in said
    assert prompts == []
    assert support.tree_digest(project) == before


@pytest.mark.parametrize("where", ["inside", "around"])
def test_adopt_with_a_path_that_overlaps_the_jobs_own_directory_refuses(
    fork: support.Fork, where: str
) -> None:
    target = fork.paths.clone if where == "inside" else fork.root
    if where == "around":
        subprocess.run([support.REAL_GIT, "init", "--quiet", str(target)], check=True)
    list_in_registry(fork, str(target))

    code, said, prompts = adopt_project(fork, target)

    assert code == common.EXIT_NEEDS_OWNER
    assert "the job's own directory" in said
    assert prompts == []


def test_adopt_migrates_an_old_installation_without_staging_anything(
    fork: support.Fork,
) -> None:
    # What the three real targets look like: a runtime directory the template
    # retired, project notes under the old root, and a root CLAUDE.md.
    project = make_project(fork)
    for rel, text in {
        ".agents/workflows/old.md": "# retired workflow\n",
        ".agents/docs/notes.md": "# the project's own notes\n",
        "CLAUDE.md": "# old contract\n",
    }.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text, encoding="utf-8")
    commit_all(project, "as an old version installed it")
    before = git_bytes(project)

    code, said, _ = adopt_project(fork, project)

    assert code == common.EXIT_OK, said
    assert "  delete     1  .agents/workflows" in said
    assert "  delete     1  CLAUDE.md" in said
    assert "  create     1  CLAUDE.md.pre-agents-md.bak" in said
    assert not (project / ".agents" / "workflows").exists()
    assert not (project / "CLAUDE.md").exists()
    moved = project / "CLAUDE.md.pre-agents-md.bak"
    assert moved.read_text(encoding="utf-8") == "# old contract\n"
    notes = (project / ".claude" / "docs" / "notes.md").read_text(encoding="utf-8")
    assert notes == "# the project's own notes\n"
    # The updater would have staged the deletion with `git rm`. Here it is an
    # unstaged deletion, and the index is the same bytes as before.
    assert git_bytes(project) == before
    status = porcelain(project)
    assert " D .agents/workflows/old.md" in status and " D CLAUDE.md" in status
    assert not [line for line in status if line[0] not in " ?"], status


def run_py(fork: support.Fork) -> Path:
    """The dispatcher inside the job's clone, checked out the way the
    launcher leaves it."""
    approved = str(fork.approved())
    support.git(
        fork.paths.clone, "checkout", "--quiet", "--force", "-B", "approved", approved
    )
    return fork.paths.clone / "scripts" / "maint" / "run.py"


def test_adopt_without_a_terminal_refuses_and_changes_nothing(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    release(fork, RELEASE, "change a rule")
    script = run_py(fork)
    before = support.tree_digest(project)

    done = subprocess.run(
        [sys.executable, str(script), "adopt", str(project)],
        capture_output=True,
        text=True,
        check=False,
        env=fork.env,
        input="yes\n",
    )

    assert done.returncode == common.EXIT_USAGE
    assert "needs a terminal" in done.stdout
    assert support.tree_digest(project) == before
    assert not (fork.paths.state_dir / distribute.LEDGER_FILE).exists()


def test_adopt_at_a_terminal_updates_the_project(fork: support.Fork) -> None:
    project = make_project(fork)
    release(fork, RELEASE, "change a rule")
    script = run_py(fork)

    done = support.run_with_terminal(
        [sys.executable, str(script), "adopt", str(project)], fork.env, "yes\n"
    )

    assert done.returncode == common.EXIT_OK, done.stdout + done.stderr
    assert "now holds" in done.stdout
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert str(project) in record(fork)["adopted"]


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


def test_launchd_adopt_goes_through_the_installed_launcher_at_a_terminal(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "some project"
    project.mkdir()
    record = tmp_path / "launcher-called"
    stub = home / ".claude" / "orchestra" / "bin" / "orchestra-maint"
    stub.parent.mkdir(parents=True)
    support.write_script(
        stub,
        f'#!/bin/bash\nprintf "%s|" "$@" > "{record}"\n'
        f'echo "tty=$([ -t 0 ] && echo yes)" >> "{record}"\nexit 7\n',
    )

    piped = launchd(home, ["adopt", str(project)], None)
    missing = launchd(home, ["adopt", str(tmp_path / "nowhere")], "yes\n")
    unnamed = launchd(home, ["adopt"], "yes\n")
    done = launchd(home, ["adopt", str(project)], "yes\n")

    assert piped.returncode == 1 and "needs a terminal" in piped.stderr
    assert missing.returncode == 1 and "not a directory" in missing.stderr
    assert unnamed.returncode == 1 and "usage:" in unnamed.stderr
    assert done.returncode == 7
    assert record.read_text(encoding="utf-8") == f"adopt|{project.resolve()}|tty=yes\n"


# --------------------------------------------------------------------------
# Several targets, the registry, and what is reported
# --------------------------------------------------------------------------


def test_one_target_failing_does_not_stop_the_others(fork: support.Fork) -> None:
    broken = adopted_project(fork, "beta")
    healthy = adopted_project(fork, "alpha")
    make_project(fork, "gamma")
    (broken / OVERRIDE_REL).write_text("{ not json\n", encoding="utf-8")
    commit_all(broken, "an override the model tool cannot read")
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(broken)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert states(fork) == {
        "beta": "failed",
        "alpha": "updated",
        "gamma": "not-adopted",
    }
    assert result.counts == {"targets": 3, "failed": 1, "updated": 1, "not-adopted": 1}
    assert (healthy / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    # The updater failed on the private copy, so the project was never written.
    assert support.tree_digest(broken) == before


def test_a_target_that_raises_is_its_own_failure(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted_project(fork, "beta")
    healthy = adopted_project(fork, "alpha")
    release(fork, RELEASE, "change a rule")
    real_stage = staging.stage_update

    def stage(ctx: object, source: object, target: str, name: str) -> object:
        if Path(target).name == "beta":
            raise RuntimeError("the disk said no")
        return real_stage(ctx, source, target, name)

    monkeypatch.setattr(staging, "stage_update", stage)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_FAILED
    assert states(fork) == {"beta": "failed", "alpha": "updated"}
    assert (healthy / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT


def test_read_registry_skips_comments_blanks_and_duplicates(tmp_path: Path) -> None:
    registry = tmp_path / "targets.txt"
    registry.write_text(
        "# projects the job may look at\n"
        "\n"
        "/work/alpha\n"
        "   /work/beta   \n"
        "/work/alpha\n"
        "/work/alpha/\n"
        "/work/./beta\r\n"
        "relative/gamma\n"
        "  # indented comment\n"
        "~/delta\n",
        encoding="utf-8",
    )

    assert targets.read_registry(registry) == [
        "/work/alpha",
        "/work/beta",
        "relative/gamma",
        "~/delta",
    ]
    assert targets.read_registry(tmp_path / "missing.txt") == []


def test_run_with_a_registry_full_of_noise_visits_each_project_once(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    list_in_registry(fork, "# a comment")
    list_in_registry(fork, "")
    list_in_registry(fork, f"{project}/")
    list_in_registry(fork, str(project))
    list_in_registry(fork, "projects/alpha")
    release(fork, RELEASE, "change a rule")

    result = distribute.run(fork.context())

    assert result.counts == {"targets": 2, "updated": 1, "refused": 1}
    rows = record(fork)["last"]
    assert [(row["target"], row["state"]) for row in rows] == [
        (str(project), "updated"),
        ("projects/alpha", "refused"),
    ]
    assert result.exit_code == common.EXIT_NEEDS_OWNER


@pytest.mark.parametrize(
    "marker", ["MERGE_HEAD", "rebase-merge", "sequencer", "index.lock"]
)
def test_run_while_git_is_in_the_middle_of_something_waits(
    fork: support.Fork, marker: str
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    (project / ".git" / marker).write_text("", encoding="utf-8")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("waiting", common.EXIT_OK)
    assert states(fork) == {"alpha": "busy"}
    assert support.tree_digest(project) == before


def test_run_while_a_session_is_writing_in_the_project_waits(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    slug = "".join(char if char.isalnum() else "-" for char in str(project))
    session = Path(fork.env["HOME"], ".claude", "projects", slug, "session.jsonl")
    session.parent.mkdir(parents=True)
    session.write_text("{}\n", encoding="utf-8")
    now = session.stat().st_mtime

    during = distribute.run(fork.context(now=now + 60))
    state_during = states(fork)
    later = distribute.run(fork.context(now=now + 2 * targets.SESSION_QUIET_SEC))

    assert (during.result, state_during) == ("waiting", {"alpha": "busy"})
    assert later.result == "distributed"


def test_run_without_a_registry_leaves_no_trace(fork: support.Fork) -> None:
    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("no-targets", common.EXIT_OK)
    assert not (fork.paths.state_dir / distribute.LEDGER_FILE).exists()
    assert not list(fork.paths.run_dir.glob("distribute-*"))


def test_run_without_an_approved_revision_is_a_configuration_error(
    fork: support.Fork,
) -> None:
    make_project(fork)
    fork.paths.approved_file.unlink()

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == (
        "no-approved-revision",
        common.EXIT_USAGE,
    )


def test_run_with_an_approved_revision_the_clone_does_not_have_fails_closed(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    fork.approve("0" * 40)
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("source-failed", common.EXIT_FAILED)
    assert support.tree_digest(project) == before
    assert not list(fork.paths.run_dir.glob("distribute-*"))
    # The table `status` prints is this run's, not a stale one.
    assert states(fork) == {"alpha": "failed"}
    assert "not visited" in detail_of(fork, "alpha")


def test_the_scratch_area_is_gone_after_a_run_and_the_clone_is_untouched(
    fork: support.Fork,
) -> None:
    adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    refs = support.git(fork.paths.clone, "for-each-ref")

    distribute.run(fork.context())

    assert not list(fork.paths.run_dir.glob("distribute-*"))
    assert support.git(fork.paths.clone, "worktree", "list").count("\n") == 0
    assert support.git(fork.paths.clone, "for-each-ref") == refs
    common.require_owned_clone(fork.context().git)


def test_the_status_comment_carries_counts_and_never_a_project(
    fork: support.Fork,
) -> None:
    adopted_project(fork, "secret-client-name")
    make_project(fork, "another-client")
    release(fork, RELEASE, "change a rule")

    result = distribute.run(fork.context())
    body = report.render([result], fork.approved(), fork.approved(), 0, 0.0)

    assert "| distribute | distributed | 0 |" in body
    assert "not-adopted 1, targets 2, updated 1" in body
    for private in ("secret-client-name", "another-client", str(fork.root)):
        assert private not in body


def test_status_lines_say_which_revision_waits_and_since_when(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    (project / "notes.txt").write_text("not committed\n", encoding="utf-8")
    approved = release(fork, RELEASE, "change a rule")
    distribute.run(fork.context(now=0.0))

    lines = ledger.status_lines(fork.paths)

    assert lines[0] == "  targets as of 1970-01-01 00:00 UTC"
    assert len(lines) == 2
    assert f"target waiting-dirty  {project}" in lines[1]
    assert f"waits for {approved[:12]} since 1970-01-01 00:00 UTC" in lines[1]


def test_the_step_runs_right_after_promotion_and_counts_in_public_words() -> None:
    names = [name for name, _ in maint_run.STEPS]
    words = [*distribute.TARGET_STATES, distribute.COUNT_TARGETS]

    assert names[0] == distribute.STEP
    assert [report.public_word(word) for word in words] == words


# --------------------------------------------------------------------------
# The whole run, through the launcher
# --------------------------------------------------------------------------


def launch(fork: support.Fork, *args: str) -> subprocess.CompletedProcess[str]:
    fork.paths.launcher.parent.mkdir(parents=True, exist_ok=True)
    fork.paths.launcher.write_bytes(
        (support.MAINT_DIR / "orchestra-maint").read_bytes()
    )
    return subprocess.run(
        ["/bin/bash", str(fork.paths.launcher), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**fork.env, "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0"},
        stdin=subprocess.DEVNULL,
        cwd=str(fork.root),
    )


def test_a_scheduled_run_distributes_after_promotion_and_status_shows_the_project(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    waiting = make_project(fork, "beta")
    release(fork, RELEASE, "change a rule")
    manifest = json.loads((fork.seed / MANIFEST_REL).read_text(encoding="utf-8"))
    codex = manifest["codex"]
    fork.set_catalog(
        support.entry(codex["model"], efforts=(codex["reasoning_effort"],))
    )

    done = launch(fork, "run")
    status = launch(fork, "status")

    summary = json.loads(fork.paths.last_run_file.read_text(encoding="utf-8"))
    steps = [(step["name"], step["result"]) for step in summary["steps"]]
    assert steps == [
        ("promote", "up-to-date"),
        ("distribute", "distributed"),
        ("model-watch", "up-to-date"),
        ("report", "no-status-issue"),
    ]
    assert done.returncode == common.EXIT_OK, done.stdout + done.stderr
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert "targets as of " in status.stdout
    assert f"target updated {project}" in status.stdout
    assert f"target not-adopted {waiting}" in status.stdout


# --------------------------------------------------------------------------
# The private copy: nothing in a project can lead a write somewhere else
# --------------------------------------------------------------------------

CANARY = "the owner's own file, outside every project\n"
ESCAPES = ("seed-directory", "legacy-links", "temporary-names")


def outside_world(fork: support.Fork) -> Path:
    """A directory outside every project, with a file nothing may change."""
    outside = fork.root / "outside"
    (outside / "a-directory").mkdir(parents=True)
    (outside / "victim.txt").write_text(CANARY, encoding="utf-8")
    return outside


def plant_escape(project: Path, shape: str, outside: Path) -> None:
    """Commit content that led the updater to write outside the project:
    the three shapes of the security review."""
    victim = outside / "victim.txt"
    legacy = project / ".agents" / "docs"
    if shape == "seed-directory":
        # A seed path that is a directory holding a link.
        seed = project / ".claude" / "STATE.md"
        seed.unlink()
        seed.mkdir()
        (seed / "STATE.md").symlink_to(victim)
    elif shape == "legacy-links":
        # Legacy notes moved into an absent directory, carrying their links.
        shutil.rmtree(project / ".claude" / "docs")
        legacy.mkdir(parents=True)
        (legacy / "note.md").write_text("x\n", encoding="utf-8")
        (legacy / "DESIGN.md").symlink_to(outside / "created-by-the-job.txt")
        (legacy / "libraries").symlink_to(outside / "a-directory")
    else:
        # Links named like the updater's temporary files.
        legacy.mkdir(parents=True)
        (legacy / "note.md").write_text("x\n", encoding="utf-8")
        for number in (1, 77, 4242, os.getpid()):
            name = f"INDEX.md.tmp.{number}"
            (project / ".claude" / "docs" / name).symlink_to(victim)
    commit_all(project, "content somebody committed")


@pytest.mark.parametrize("shape", ESCAPES)
def test_adopt_with_content_that_leads_outside_refuses_before_anything_runs(
    fork: support.Fork, shape: str
) -> None:
    # The write outside used to happen during the rehearsal, before the
    # prompt: the owner answered no and the file was already gone.
    outside = outside_world(fork)
    project = make_project(fork)
    plant_escape(project, shape, outside)
    before, world = support.tree_digest(project), support.tree_digest(outside)

    code, said, prompts = adopt_project(fork, project, answer="no")

    assert code == common.EXIT_NEEDS_OWNER, said
    assert "has a link" in said
    assert prompts == []
    assert support.tree_digest(outside) == world
    assert support.tree_digest(project) == before
    assert not (fork.paths.state_dir / distribute.LEDGER_FILE).exists()


@pytest.mark.parametrize(
    ("shape", "state"),
    [
        ("seed-directory", "refused"),
        ("legacy-links", "local-changes"),
        ("temporary-names", "refused"),
    ],
)
def test_run_with_content_that_leads_outside_writes_nothing_anywhere(
    fork: support.Fork, shape: str, state: str
) -> None:
    # Committed content is enough: the tree is clean when the run looks.
    outside = outside_world(fork)
    project = adopted_project(fork)
    plant_escape(project, shape, outside)
    release(fork, RELEASE, "change a rule")
    before, world = support.tree_digest(project), support.tree_digest(outside)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": state}
    assert support.tree_digest(outside) == world
    assert support.tree_digest(project) == before


@pytest.mark.parametrize(
    ("rel", "why"),
    [
        (".claude/STATE.md", "has a directory where the file .claude/STATE.md belongs"),
        (".codex/config.toml", "has a directory where the file .codex/config.toml"),
        (".claude/docs/INDEX.md.tmp.123", "keeps for its temporary files"),
        (".claude/rules/x.orchestra-staging.9", "keeps for its temporary files"),
        (".claude/skills/.orchestra-tmp.Ab12Cd", "keeps for its temporary files"),
    ],
)
def test_adopt_with_a_copy_the_updater_would_misread_refuses(
    fork: support.Fork, rel: str, why: str
) -> None:
    # No link involved: a directory where the updater expects a file, or a
    # file under one of the names it keeps for itself.
    project = make_project(fork)
    (project / ".agents" / "docs").mkdir(parents=True)
    (project / ".agents" / "docs" / "note.md").write_text("x\n", encoding="utf-8")
    path = project / rel
    if path.is_file():
        path.unlink()
        path.mkdir()
        (path / "inside.md").write_text("x\n", encoding="utf-8")
    else:
        path.write_text("x\n", encoding="utf-8")
    commit_all(project, "an odd layout")
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project)

    assert code == common.EXIT_NEEDS_OWNER, said
    assert why in said
    assert prompts == []
    assert support.tree_digest(project) == before


def test_copy_scope_admits_regular_files_and_directories_only(tmp_path: Path) -> None:
    target, outside = tmp_path / "target", tmp_path / "outside"
    (target / "docs" / "deep").mkdir(parents=True)
    outside.mkdir()
    (target / "docs" / "deep" / "a.md").write_text("a\n", encoding="utf-8")
    (target / "docs" / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (target / "docs" / "tool.sh").chmod(0o755)
    refused: dict[str, str] = {}

    def attempt(name: str) -> None:
        stage = tmp_path / f"stage-{name}"
        stage.mkdir()
        try:
            staging.copy_scope(str(target), str(stage), ("docs", "docs/deep/a.md"))
        except staging.Unstageable as exc:
            refused[name] = str(exc)

    attempt("plain")
    plain = targets.snapshot(str(tmp_path / "stage-plain"), skip_top=())
    (target / "docs" / "deep" / "link").symlink_to(outside)
    attempt("link")
    (target / "docs" / "deep" / "link").unlink()
    os.mkfifo(target / "docs" / "deep" / "pipe")
    attempt("fifo")
    os.unlink(target / "docs" / "deep" / "pipe")
    shutil.move(str(target / "docs"), str(outside / "docs"))
    (target / "docs").symlink_to(outside / "docs")
    attempt("parent")

    assert sorted(plain) == ["docs", "docs/deep", "docs/deep/a.md", "docs/tool.sh"]
    assert plain["docs/tool.sh"].executable and not plain["docs/deep/a.md"].executable
    assert refused == {
        "link": "has a link at docs/deep/link",
        "fifo": "has something that is not a file or a directory at docs/deep/pipe",
        "parent": "has a link at docs",
    }
    assert list(outside.iterdir()) == [outside / "docs"]


def test_copy_scope_never_writes_onto_a_name_that_is_already_there(
    tmp_path: Path,
) -> None:
    # What two names differing only in case become on a volume that folds
    # case: the second meets the first. The copy stops instead of following.
    target, stage, outside = tmp_path / "target", tmp_path / "stage", tmp_path / "out"
    for directory in (target, stage, outside):
        directory.mkdir()
    (target / "notes.md").write_text("the project's bytes\n", encoding="utf-8")
    victim = outside / "victim.txt"
    victim.write_text(CANARY, encoding="utf-8")
    # The name is taken already, by a link: the file must not be written
    # onto it, and so not through it.
    (stage / "notes.md").symlink_to(victim)

    with pytest.raises(staging.Unstageable, match="two names that become one"):
        staging.copy_scope(str(target), str(stage), ("notes.md",))

    assert victim.read_text(encoding="utf-8") == CANARY
    assert (stage / "notes.md").is_symlink()


def fenced() -> bool:
    """Whether the kernel sandbox can be used from this process."""
    if not os.path.isfile(staging.SANDBOX):
        return False
    probe = [staging.SANDBOX, "-p", "(version 1)(allow default)", "/usr/bin/true"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


def release_updater(fork: support.Fork, extra: str) -> str:
    """Approve a revision whose updater does one more thing first."""
    text = support.REAL_UPDATER.read_text(encoding="utf-8")
    marker = "set -euo pipefail\n"
    assert text.count(marker) == 1
    changed = text.replace(marker, marker + extra)
    return release(fork, {"scripts/update.sh": changed, **RELEASE}, "another updater")


def test_the_updater_runs_with_its_home_and_temporary_directory_in_the_scratch_area(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    scratch = f"{fork.paths.run_dir}/distribute-"
    release_updater(
        fork,
        f'for place in "$HOME" "$TMPDIR" "$PWD"; do\n'
        f'    case "$place" in "{scratch}"*) ;; *) echo "outside: $place" >&2; exit 9 ;; esac\n'
        "done\n"
        'case "$PWD" in */empty) ;; *) echo "started in: $PWD" >&2; exit 9 ;; esac\n'
        '[ -z "$(ls -A "$PWD")" ] || exit 9\n',
    )

    result = distribute.run(fork.context())

    assert states(fork) == {"alpha": "updated"}, fork.log_file.read_text()
    assert result.exit_code == common.EXIT_OK
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT


@pytest.mark.skipif(not fenced(), reason="no usable sandbox-exec here")
def test_an_updater_that_writes_outside_its_scratch_area_is_stopped_by_the_kernel(
    fork: support.Fork,
) -> None:
    # A path nobody thought of: with the sandbox it fails instead of escaping.
    project = adopted_project(fork)
    canary = fork.root / "written-by-the-updater.txt"
    release_updater(fork, f'echo escaped > "{canary}"\n')
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert not canary.exists()
    assert support.tree_digest(project) == before
    assert "no sandbox here" not in fork.log_file.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# A write is planned whole, and a write that began is never "not written"
# --------------------------------------------------------------------------


def directory_becomes_file(tmp_path: Path) -> tuple[Path, Path, dict, dict]:
    """A target and a prepared copy in which `pkg/x` turns from a directory
    into a file, one file changes and one is new."""
    target, stage = tmp_path / "target", tmp_path / "stage"
    for root in (target, stage):
        (root / "pkg" / "x").mkdir(parents=True)
        (root / "pkg" / "x" / "child.md").write_text("old\n", encoding="utf-8")
        (root / "pkg" / "keep.md").write_text("keep\n", encoding="utf-8")
    before = targets.snapshot(str(stage), skip_top=())
    shutil.rmtree(stage / "pkg" / "x")
    (stage / "pkg" / "x").write_text("now a file\n", encoding="utf-8")
    (stage / "pkg" / "keep.md").write_text("changed too\n", encoding="utf-8")
    (stage / "pkg" / "zz-new.md").write_text("new\n", encoding="utf-8")
    return target, stage, before, targets.snapshot(str(stage), skip_top=())


def test_apply_changes_with_a_directory_that_becomes_a_file_writes_all_of_it(
    tmp_path: Path,
) -> None:
    target, stage, before, after = directory_becomes_file(tmp_path)

    targets.apply_changes(str(target), str(stage), before, after)

    assert targets.snapshot(str(target), skip_top=()) == after
    assert targets.applied_problem(str(target), before, after) is None


def test_plan_changes_with_something_in_the_way_of_a_file_writes_nothing(
    tmp_path: Path,
) -> None:
    # The directory that has to become a file holds a file of the owner's.
    target, stage, before, after = directory_becomes_file(tmp_path)
    (target / "pkg" / "x" / "mine.md").write_text("the owner's\n", encoding="utf-8")
    untouched = support.tree_digest(target)

    with pytest.raises(targets.Conflict, match="pkg/x"):
        targets.apply_changes(str(target), str(stage), before, after)

    assert support.tree_digest(target) == untouched


def test_a_directory_that_holds_something_of_the_owners_is_left_standing(
    tmp_path: Path,
) -> None:
    target, stage = tmp_path / "target", tmp_path / "stage"
    for root in (target, stage):
        (root / "old" / "deep").mkdir(parents=True)
        (root / "old" / "deep" / "gone.md").write_text("x\n", encoding="utf-8")
    before = targets.snapshot(str(stage), skip_top=())
    shutil.rmtree(stage / "old")
    (target / "old" / "deep" / "mine.md").write_text("the owner's\n", encoding="utf-8")

    targets.apply_changes(str(target), str(stage), before, {})

    assert sorted(support.tree_digest(target)) == [
        "old",
        "old/deep",
        "old/deep/mine.md",
    ]


def test_a_write_that_fails_part_way_is_interrupted_and_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, stage, before, after = directory_becomes_file(tmp_path)
    real_replace, calls = os.replace, []

    def replace_once(source: str, destination: str) -> None:
        calls.append(destination)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace_once)

    with pytest.raises(targets.Interrupted, match="No space left"):
        targets.apply_changes(str(target), str(stage), before, after)

    monkeypatch.undo()
    assert not list(target.rglob(f"{targets.TEMP_PREFIX}*"))
    assert targets.snapshot(str(target), skip_top=()) != after


def test_run_with_a_write_that_stops_part_way_says_so_and_how_to_recover(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    real_replace, calls = os.replace, []

    def replace_once(source: str, destination: str) -> None:
        inside = destination.startswith(str(project))
        calls.extend([destination] if inside else [])
        if inside and len(calls) == 2:
            raise OSError(28, "No space left on device")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace_once)

    failed = distribute.run(fork.context())
    detail = detail_of(fork, "alpha")
    monkeypatch.undo()
    # A write that was killed outright also leaves one of these behind.
    leftover = project / ".claude" / "rules" / f"{targets.TEMP_PREFIX}{'0' * 16}.tmp"
    leftover.write_text("half a file\n", encoding="utf-8")
    again = distribute.run(fork.context())

    assert (failed.result, failed.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert "written in part" in detail and "not written" not in detail
    assert record(fork)["adopted"][str(project)]["interrupted"] != ""
    assert (again.result, states(fork)) == ("needs-owner", {"alpha": "local-changes"})
    for step in ("discard or commit", "git status", "launchd.sh adopt"):
        assert step in detail_of(fork, "alpha")
    assert not leftover.exists()
    # The steps work: discard, adopt, and the mark is gone.
    support.git(project, "checkout", "--", ".")
    code, said, _ = adopt_project(fork, project)
    assert code == common.EXIT_OK, said
    assert record(fork)["adopted"][str(project)]["interrupted"] == ""


# --------------------------------------------------------------------------
# Settings: the model keys and nothing else
# --------------------------------------------------------------------------


def load_models_tool() -> object:
    spec = importlib.util.spec_from_file_location(
        "models_tool", support.REPO_ROOT / support.SHARED_REL / "models.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["models_tool"] = module
    spec.loader.exec_module(module)
    return module


def test_the_model_keys_are_the_manifest_tools_own() -> None:
    tool = load_models_tool()

    assert distribute.TOML_MODEL_KEYS == tool.TOML_MANAGED_NAMES
    assert set(distribute.JSON_MODEL_ENV_KEYS) == {tool.SUBAGENT_ENV, tool.CODEX_ENV}
    assert distribute.JSON_MODEL_KEYS == ("model",)


@pytest.mark.parametrize(
    ("rel", "old", "new", "allowed"),
    [
        ("s.json", '{"model": "a", "x": 1}', '{"x": 1, "model": "b"}', True),
        (
            "s.json",
            '{"env": {"CODEX_MODEL": "a", "CLAUDE_CODE_SUBAGENT_MODEL": "s"}}',
            '{"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "t"}}',
            True,
        ),
        ("s.json", '{"env": {"CODEX_MODEL": "a"}}', "{}", True),
        ("s.json", '{"model": "a", "x": 1}', '{"model": "b", "x": 2}', False),
        ("s.json", '{"env": {"MINE": "a"}}', '{"env": {"MINE": "b"}}', False),
        ("s.json", '{"hooks": {"a": 1}}', '{"hooks": {"a": 1, "b": 2}}', False),
        ("s.json", '{"model": "a"', '{"model": "b"}', False),
        ("s.json", '["model"]', '["model"]', False),
        ("c.toml", 'model = "a"\nx = 1\n', 'model = "b"\nx = 1\n', True),
        (
            "c.toml",
            'model_reasoning_effort = "high"\n[t]\nmodel = "k"\n',
            'model_reasoning_effort = "low"\n[t]\nmodel = "k"\n',
            True,
        ),
        ("c.toml", 'x = 1\n[t]\nmodel = "a"\n', 'x = 1\n[t]\nmodel = "b"\n', False),
        ("c.toml", 'model = "a"\nx = 1\n', 'model = "a"\nx = 2\n', False),
        ("c.yaml", "model: a\n", "model: b\n", False),
    ],
)
def test_only_model_keys_differ(rel: str, old: str, new: str, allowed: bool) -> None:
    assert distribute.only_model_keys_differ(rel, old, new) is allowed


def test_run_never_rewrites_the_owners_own_settings_values(fork: support.Fork) -> None:
    # The updater rewrites every `.agents/logs/` and `.agents/hooks/` it finds
    # in the settings file, the owner's own values included.
    project = adopted_project(fork)
    settings = project / ".claude" / "settings.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    document["env"] = {
        **document.get("env", {}),
        "MY_TOOL_LOG": "/Users/me/work/.agents/logs/tool.log",
        "MY_HOOK": "~/other/.agents/hooks/run.sh",
    }
    settings.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    commit_all(project, "the owner's own settings")
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "local-changes"}
    assert "more than the model keys of .claude/settings.json" in detail_of(
        fork, "alpha"
    )
    assert support.tree_digest(project) == before


# --------------------------------------------------------------------------
# What git was told not to look at, and what holds no file
# --------------------------------------------------------------------------


def hide_an_edit(project: Path, how: str) -> str:
    """Edit a tracked template file and tell git not to look at it."""
    rule = sorted((project / ".claude" / "rules").glob("*.md"))[0]
    rel = str(rule.relative_to(project))
    support.git(project, "update-index", how, rel)
    rule.write_text(rule.read_text(encoding="utf-8") + "\n## mine\n", encoding="utf-8")
    return rel


@pytest.mark.parametrize("how", ["--assume-unchanged", "--skip-worktree"])
def test_adopt_names_an_edit_git_was_told_not_to_look_at(
    fork: support.Fork, how: str
) -> None:
    project = make_project(fork)
    rel = hide_an_edit(project, how)
    assert porcelain(project) == []

    code, said, prompts = adopt_project(fork, project, answer="no")

    assert code == common.EXIT_OK and len(prompts) == 1
    assert "Git cannot bring back the present content of 1 of the files" in said
    assert f"  change  {rel}  (hidden)" in said
    assert "1 paths here are hidden from git status" in said
    listing = sorted(fork.paths.logs_dir.glob("*.adopt.log"))[-1]
    assert f"change\thidden\t{rel}\n" in listing.read_text(encoding="utf-8")


def test_run_with_an_edit_git_was_told_not_to_look_at_waits(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    hide_an_edit(project, "--assume-unchanged")
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("waiting", common.EXIT_OK)
    assert states(fork) == {"alpha": "waiting-dirty"}
    assert "1 hidden from git status" in detail_of(fork, "alpha")
    assert support.tree_digest(project) == before


def test_adopt_shows_a_directory_that_goes_although_it_holds_no_file(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    (project / ".gemini" / "mine" / "deeper").mkdir(parents=True)

    code, said, _ = adopt_project(fork, project, answer="no")

    assert code == common.EXIT_OK
    assert "  delete     1  .gemini/" in said
    assert "a path ending in / is a directory that holds no file" in said
    assert (project / ".gemini" / "mine" / "deeper").is_dir()


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage",
    ["truncated", "not-an-object", "bad-adoption", "short-identity", "wrong-type"],
)
def test_run_with_a_record_it_cannot_read_sets_it_aside_and_touches_nothing(
    fork: support.Fork, damage: str
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    path = fork.paths.state_dir / distribute.LEDGER_FILE
    good = path.read_text(encoding="utf-8")
    if damage == "truncated":
        bad = good[: len(good) // 2]
    elif damage == "not-an-object":
        bad = "[]\n"
    else:
        document = json.loads(good)
        adoption = document["adopted"][str(project)]
        if damage == "bad-adoption":
            adoption["identity"] = "none"
        elif damage == "short-identity":
            adoption["identity"] = adoption["identity"][:3]
        else:
            adoption["target_print"] = 7
        bad = json.dumps(document)
    path.write_text(bad, encoding="utf-8")
    before = support.tree_digest(project)

    first = distribute.run(fork.context(now=1_800_000_000.0))
    aside = sorted(fork.paths.state_dir.glob("distribution.json.unreadable-*"))
    second = distribute.run(fork.context())
    aside[0].unlink()
    third = distribute.run(fork.context())

    # Not read as "nothing adopted", kept as evidence, and said out loud for
    # as long as the file that was set aside is there.
    for result in (first, second):
        assert (result.result, result.exit_code) == (
            "needs-owner",
            common.EXIT_NEEDS_OWNER,
        )
        assert "set aside" in result.detail
    assert [item.name for item in aside] == [
        "distribution.json.unreadable-20270115T080000Z"
    ]
    assert support.tree_digest(project) == before
    assert "unreadable record" in fork.log_file.read_text(encoding="utf-8")
    assert (third.result, third.exit_code) == ("none-adopted", common.EXIT_OK)


def test_adopt_with_a_record_it_cannot_read_sets_it_aside_and_does_not_ask(
    fork: support.Fork,
) -> None:
    adopted_project(fork, "beta")
    project = make_project(fork)
    path = fork.paths.state_dir / distribute.LEDGER_FILE
    path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project)

    assert code == common.EXIT_NEEDS_OWNER
    assert "could not be read" in said and "set aside" in said
    assert prompts == []
    assert support.tree_digest(project) == before
    assert len(list(fork.paths.state_dir.glob("distribution.json.unreadable-*"))) == 1


# --------------------------------------------------------------------------
# No alarm when nothing is pending; an alarm when a wait has lasted
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("after", "state"),
    [
        ("committed", "up-to-date"),
        ("uncommitted", "uncommitted"),
        ("update-discarded", "up-to-date"),
    ],
)
def test_run_with_a_local_difference_and_nothing_new_to_write_raises_no_alarm(
    fork: support.Fork, after: str, state: str
) -> None:
    # The job wrote this very template here. What the owner did with it since
    # is the owner's business until there is something new to write.
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    distribute.run(fork.context())
    if after == "update-discarded":
        support.git(project, "checkout", "--", ".")
    else:
        commit_all(project, "take the update")
        (project / RULE_REL).write_text(OWNER_TEXT, encoding="utf-8")
    if after == "committed":
        commit_all(project, "our own testing rules")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("up-to-date", common.EXIT_OK)
    assert states(fork) == {"alpha": state}
    row = record(fork)["last"][0]
    assert (row["pending"], row["since"]) == ("", 0.0)
    assert "differ from what the job left" in row["detail"]
    assert support.tree_digest(project) == before
    # Once there is something new, the difference is the owner's to decide.
    release(fork, {RULE_REL: NEWER_TEXT}, "change it again")
    later = distribute.run(fork.context())
    expected = "waiting-dirty" if after == "uncommitted" else "local-changes"
    assert states(fork) == {"alpha": expected}
    assert later.exit_code == distribute.TARGET_STATES[expected][0]


def test_a_wait_is_counted_from_the_first_revision_and_becomes_the_owners_in_time(
    fork: support.Fork,
) -> None:
    day = common.SECONDS_PER_DAY
    limit = distribute.WAITING_OVERDUE_DAYS
    project = adopted_project(fork)
    (project / "notes.txt").write_text("never committed\n", encoding="utf-8")
    (project / "README.md").write_text("# edited\n", encoding="utf-8")
    release(fork, RELEASE, "change a rule")
    first = distribute.run(fork.context(now=1000.0))
    newest = release(fork, {RULE_REL: NEWER_TEXT}, "change it again")

    second = distribute.run(fork.context(now=1000.0 + day))
    row = record(fork)["last"][0]
    overdue = distribute.run(fork.context(now=1000.0 + (limit + 1) * day))

    assert (first.exit_code, second.exit_code) == (common.EXIT_OK, common.EXIT_OK)
    # The newest revision waits, since the day the first one had to.
    assert (row["pending"], row["since"]) == (newest, 1000.0)
    assert (overdue.result, overdue.exit_code) == (
        "needs-owner",
        common.EXIT_NEEDS_OWNER,
    )
    assert overdue.counts == {"targets": 1, "waiting-dirty": 1, "overdue": 1}
    detail = detail_of(fork, "alpha")
    assert f"waiting {limit + 1}.0 days" in detail
    assert "1 modified or staged, 1 untracked, 0 hidden" in detail
    # What leaves the machine carries the counts and no name.
    body = report.render([overdue], fork.approved(), fork.approved(), 0, 0.0)
    assert "overdue 1" in body and "notes.txt" not in body
    # The wait ends when the project is updated, and so does the clock.
    (project / "notes.txt").unlink()
    commit_all(project, "finish")
    done = distribute.run(fork.context(now=1000.0 + (limit + 2) * day))
    adoption = record(fork)["adopted"][str(project)]
    assert done.result == "distributed"
    assert (adoption["waiting_revision"], adoption["waiting_since"]) == ("", 0.0)


# --------------------------------------------------------------------------
# The approved updater, and nothing but what it produced
# --------------------------------------------------------------------------


def plant_own_updater(project: Path, marker: Path) -> None:
    """Give the project an updater of its own that leaves a file behind."""
    script = project / "scripts" / "update.sh"
    script.write_text(f"#!/bin/bash\n: > '{marker}'\nexit 0\n", encoding="utf-8")
    commit_all(project, "an updater of the project's own")


def test_adopt_runs_the_approved_updater_never_the_projects_own(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    marker = fork.root / "the-projects-own-updater-ran"
    plant_own_updater(project, marker)
    release(fork, RELEASE, "change a rule")

    code, said, _ = adopt_project(fork, project)

    assert code == common.EXIT_OK, said
    assert not marker.exists()
    assert not list(fork.root.rglob(marker.name))
    assert (project / "scripts" / "update.sh").read_bytes() == (
        support.REAL_UPDATER.read_bytes()
    )
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT


def test_run_with_an_updater_of_the_projects_own_runs_nothing_of_it(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    marker = fork.root / "the-projects-own-updater-ran"
    plant_own_updater(project, marker)
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "local-changes"}
    assert not marker.exists()
    assert support.tree_digest(project) == before


def test_run_fails_when_the_updater_did_not_produce_the_approved_files(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    real_run = common.run_command

    def run_then_tamper(argv: list[str], *rest: object, **named: object) -> object:
        done = real_run(argv, *rest, **named)
        for argument in argv:
            if argument.endswith("/scripts/update.sh"):
                rule = Path(argument).parents[1] / RULE_REL
                with rule.open("a", encoding="utf-8") as handle:
                    handle.write("# not in the approved revision\n")
        return done

    monkeypatch.setattr(common, "run_command", run_then_tamper)
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert "did not produce the approved revision's files" in detail_of(fork, "alpha")
    assert support.tree_digest(project) == before


def test_run_with_model_settings_the_tool_will_not_edit_refuses_and_says_why(
    fork: support.Fork,
) -> None:
    # The updater only warns about this and exits 0. The step asks the
    # manifest tool itself afterwards and does not write a half-applied update.
    project = adopted_project(fork)
    config = project / CONFIG_REL
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    model = next(line for line in lines if line.startswith("model = "))
    config.write_text("".join([model, *lines]), encoding="utf-8")
    commit_all(project, "the model key twice")
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "refused"}
    assert "has model settings to fix first" in detail_of(fork, "alpha")
    assert CONFIG_REL in detail_of(fork, "alpha")
    assert support.tree_digest(project) == before


def test_run_with_a_write_that_did_not_take_says_so(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    real_place = targets._place

    def place_then_spoil(source: str, destination: str, executable: bool) -> None:
        real_place(source, destination, executable)
        if destination.endswith(RULE_REL):
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write("# written by something else\n")

    monkeypatch.setattr(targets, "_place", place_then_spoil)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert f"written in part: {RULE_REL} is not what the update produced" in (
        detail_of(fork, "alpha")
    )
    assert record(fork)["adopted"][str(project)]["interrupted"] != ""


def test_applied_problem_names_what_is_wrong_and_what_should_be_gone(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "kept.md").write_text("kept\n", encoding="utf-8")
    (target / "gone.md").write_text("still here\n", encoding="utf-8")
    kept = targets.entry_at(str(target / "kept.md"))
    gone = targets.entry_at(str(target / "gone.md"))
    assert kept is not None and gone is not None
    before = {"kept.md": kept, "gone.md": gone}

    done = targets.applied_problem(str(target), before, {"kept.md": kept})
    (target / "gone.md").unlink()
    clean = targets.applied_problem(str(target), before, {"kept.md": kept})
    (target / "kept.md").write_text("spoiled\n", encoding="utf-8")
    spoiled = targets.applied_problem(str(target), before, {"kept.md": kept})

    assert done == "gone.md should be gone"
    assert clean is None
    assert spoiled == "kept.md is not what the update produced"


def test_plan_changes_refuses_an_update_that_is_not_files_and_directories(
    tmp_path: Path,
) -> None:
    target, stage = tmp_path / "target", tmp_path / "stage"
    target.mkdir()
    stage.mkdir()
    after = {"pipe": targets.Entry(targets.KIND_OTHER)}

    with pytest.raises(targets.Conflict, match="not a file"):
        targets.plan_changes(str(target), str(stage), {}, after)

    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    ("array", "entry"),
    [
        ("SAFE_DIRS", "../outside"),
        ("SAFE_DIRS", ".claude/../../outside"),
        ("SAFE_FILES", "/etc/passwd"),
        ("DEPRECATED_PATHS", "./"),
        ("LEGACY_PROJECT_DIRS", "docs//logs"),
    ],
)
def test_read_shipped_refuses_a_list_entry_that_is_not_a_plain_relative_path(
    array: str, entry: str
) -> None:
    updater = support.REAL_UPDATER.read_text(encoding="utf-8")
    opening = f"{array}=(\n"
    assert updater.count(opening) == 1
    tampered = updater.replace(opening, f'{opening}    "{entry}"\n')

    assert staging.read_shipped(updater) is not None
    assert staging.read_shipped(tampered) is None


def test_run_with_a_template_that_lacks_a_directory_it_replaces_touches_nothing(
    fork: support.Fork,
) -> None:
    # The updater skips a directory the template does not have, and would
    # then run the manifest tool from the project's copy of it.
    project = adopted_project(fork)
    support.git(fork.seed, "rm", "-r", "--quiet", ".claude/hooks")
    release(fork, RELEASE, "a template without its hooks")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("source-failed", common.EXIT_FAILED)
    assert "lacks a directory it replaces" in result.detail
    assert support.tree_digest(project) == before


def test_run_checks_that_the_scratch_checkout_is_the_approved_revision(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = adopted_project(fork)
    approved = release(fork, RELEASE, "change a rule")
    newer = fork.commit({RULE_REL: NEWER_TEXT}, "not approved")
    fork.fetch()
    real_run = common.Git.run

    def lay_out_another(self: object, args: list[str], *rest: object) -> object:
        swapped = [newer if item == approved else item for item in args]
        return real_run(
            self, swapped if args[:2] == ["worktree", "add"] else args, *rest
        )

    monkeypatch.setattr(common.Git, "run", lay_out_another)
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("source-failed", common.EXIT_FAILED)
    assert "not the approved revision" in result.detail
    assert support.tree_digest(project) == before


# --------------------------------------------------------------------------
# What the scheduled run refuses, and what it never looks at
# --------------------------------------------------------------------------


@pytest.mark.parametrize("hazard", ["a-link", "a-nested-repository"])
def test_run_with_a_hazard_on_a_template_path_refuses_the_project(
    fork: support.Fork, hazard: str
) -> None:
    project = adopted_project(fork)
    outside = fork.root / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    if hazard == "a-link":
        (project / OVERRIDE_REL).symlink_to(outside)
        commit_all(project, "an override kept somewhere else")
        why = f"has a link at {OVERRIDE_REL}"
    else:
        # git never lists an entry named .git, so the tree stays clean.
        (project / ".claude" / "rules" / ".git").mkdir()
        why = "has a repository of its own at .claude/rules"
    assert porcelain(project) == []
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())
    code, said, prompts = adopt_project(fork, project)

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "refused"}
    assert why in detail_of(fork, "alpha")
    assert (code, prompts) == (common.EXIT_NEEDS_OWNER, [])
    assert why in said
    assert support.tree_digest(project) == before
    assert outside.read_text(encoding="utf-8") == "{}\n"


def test_adopt_with_a_git_file_instead_of_a_repository_refuses(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    elsewhere = project.with_name("the-real-repository")
    shutil.move(str(project / ".git"), str(elsewhere))
    (project / ".git").write_text(f"gitdir: {elsewhere}\n", encoding="utf-8")
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project)

    assert (code, prompts) == (common.EXIT_NEEDS_OWNER, [])
    assert "does not hold its own git repository" in said
    assert support.tree_digest(project) == before


def test_run_starts_no_git_in_a_project_that_was_not_adopted(
    fork: support.Fork,
) -> None:
    adopted = adopted_project(fork)
    stranger = make_project(fork, "beta")
    release(fork, RELEASE, "change a rule")
    calls = fork.root / "git-calls.log"
    fork.env["FAKE_GIT_LOG"] = str(calls)

    distribute.run(fork.context())

    logged = calls.read_text(encoding="utf-8")
    assert f"-C {adopted} status" in logged
    assert str(stranger) not in logged
    assert states(fork) == {"alpha": "updated", "beta": "not-adopted"}


def test_target_git_passes_every_hardening_option_and_variable(
    fork: support.Fork,
) -> None:
    # No reading command starts a hook, so nothing behavioural can show that
    # the hook path is neutralised: the options themselves are held here.
    project = make_project(fork)
    git = target_git.TargetGit(fork.context(), str(project))

    listed = git.run(["config", "--list", "--show-scope", "-z"]).stdout.split("\0")
    given = {
        tuple(item.split("\n", 1))
        for scope, item in zip(listed[0::2], listed[1::2], strict=False)
        if scope == "command"
    }

    assert {
        ("core.hookspath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("core.attributesfile", "/dev/null"),
        ("status.submodulesummary", "false"),
        ("diff.autorefreshindex", "false"),
    } <= given
    assert {
        name: git.env.get(name)
        for name in (
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_TERMINAL_PROMPT",
            "GIT_OPTIONAL_LOCKS",
            "GIT_NO_LAZY_FETCH",
            "GIT_ALLOW_PROTOCOL",
        )
    } == {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "none",
    }


def test_run_never_starts_git_inside_a_submodule(
    fork: support.Fork, tmp_path: Path
) -> None:
    project = adopted_project(fork)
    source = fork.root / "submodule-source"
    subprocess.run([support.REAL_GIT, "init", "--quiet", str(source)], check=True)
    (source / "a.txt").write_text("a\n", encoding="utf-8")
    commit_all(source, "a submodule")
    support.git(
        project, "-c", "protocol.file.allow=always",
        "submodule", "--quiet", "add", str(source), "vendor/sub",
    )  # fmt: skip
    commit_all(project, "add a submodule")
    marker = tmp_path / "tripwire"
    inner = project / ".git" / "modules" / "vendor" / "sub"
    for key, command in (
        ("core.fsmonitor", f"touch '{marker}.monitor'; true"),
        ("filter.evil.clean", f"touch '{marker}.filter'; cat"),
    ):
        subprocess.run(
            [support.REAL_GIT, "--git-dir", str(inner), "config", key, command],
            check=True,
        )
    (project / "vendor" / "sub" / ".gitattributes").write_text(
        "* filter=evil\n", encoding="utf-8"
    )
    (project / "vendor" / "sub" / "a.txt").write_text(
        "dirty inside\n", encoding="utf-8"
    )
    release(fork, RELEASE, "change a rule")

    result = distribute.run(fork.context())

    # Work inside the submodule is not looked at, and nothing of it ran.
    assert states(fork) == {"alpha": "updated"}, detail_of(fork, "alpha")
    assert result.result == "distributed"
    assert tripped(marker) == []


def fabricated_update(
    tmp_path: Path, before: dict[str, str], after: dict[str, str]
) -> tuple[staging.Source, staging.Staged, str]:
    """A source with small path lists, and a prepared update between two
    sets of files (path -> text), without running an updater."""
    target, stage = tmp_path / "target", tmp_path / "stage"
    for root, files in ((target, before), (stage, after)):
        root.mkdir()
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
    shipped = staging.Shipped(
        dirs=(".claude/rules",),
        files=("AGENTS.md",),
        settings=(".claude/settings.json", ".codex/config.toml"),
        deprecated=(".gemini",),
        legacy=("docs",),
    )
    source = staging.Source(tmp_path, tmp_path, "", shipped, (), "", "", ())
    snapshots = (
        targets.snapshot(str(target), skip_top=()),
        targets.snapshot(str(stage), skip_top=()),
    )
    return source, staging.Staged(str(stage), *snapshots), str(target)


@pytest.mark.parametrize(
    ("before", "after", "problem"),
    [
        ({"AGENTS.md": "a\n"}, {"AGENTS.md": "b\n", ".claude/rules/r.md": "r\n"}, None),
        ({}, {".claude/STATE.md": "seed\n", ".claude/orchestra-version": "2\n"}, None),
        (
            {".claude/settings.json": '{"model": "a", "x": 1}'},
            {".claude/settings.json": '{"model": "b", "x": 1}'},
            None,
        ),
        (
            {".claude/settings.json": '{"model": "a", "x": 1}'},
            {".claude/settings.json": '{"model": "a", "x": 2}'},
            "the update would change more than the model keys of .claude/settings.json",
        ),
        (
            {},
            {".claude/settings.json": '{"model": "a"}'},
            "the update would also create .claude/settings.json",
        ),
        (
            {".codex/config.toml": 'model = "a"\n'},
            {},
            "the update would also delete .codex/config.toml",
        ),
        (
            {".claude/STATE.md": "the project's\n"},
            {".claude/STATE.md": "the project's\nand more\n"},
            "the update would also change .claude/STATE.md",
        ),
        ({".gemini/x": "x\n"}, {}, "the update would also delete .gemini/x"),
    ],
)
def test_unattended_problem_allows_the_templates_own_and_the_model_keys(
    tmp_path: Path, before: dict[str, str], after: dict[str, str], problem: str | None
) -> None:
    source, staged, target = fabricated_update(tmp_path, before, after)

    assert distribute.unattended_problem(source, staged, target) == problem


def test_a_record_that_is_a_link_is_refused(fork: support.Fork) -> None:
    adopted_project(fork)
    path = fork.paths.state_dir / distribute.LEDGER_FILE
    elsewhere = fork.root / "a-record-somewhere-else.json"
    shutil.move(str(path), str(elsewhere))
    path.symlink_to(elsewhere)

    with pytest.raises(common.Refused, match="is a link"):
        distribute.run(fork.context())


# --------------------------------------------------------------------------
# The owner's command, between the screen and the answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("meanwhile", "code", "words"),
    [
        ("a-new-file", common.EXIT_NEEDS_OWNER, "changed while the update was"),
        ("an-edit", common.EXIT_NEEDS_OWNER, "changed while the update was"),
        ("a-commit", common.EXIT_NEEDS_OWNER, "changed while the update was"),
        ("another-approval", common.EXIT_FAILED, "approved revision moved"),
    ],
)
def test_adopt_looks_again_after_the_answer_and_writes_nothing_if_anything_moved(
    fork: support.Fork, meanwhile: str, code: int, words: str
) -> None:
    project = make_project(fork)
    release(fork, RELEASE, "change a rule")
    said: list[str] = []

    def answer_after_something_happened(_prompt: str) -> str:
        if meanwhile == "a-new-file":
            (project / "notes.txt").write_text("new\n", encoding="utf-8")
        elif meanwhile == "an-edit":
            (project / "README.md").write_text("# edited\n", encoding="utf-8")
        elif meanwhile == "a-commit":
            support.git(project, "commit", "--quiet", "--allow-empty", "-m", "work")
        else:
            fork.approve("1" * 40)
        return "yes"

    result = adopt.run(
        fork.context(), str(project), answer_after_something_happened, said.append
    )

    assert result == code, said
    assert words in "\n".join(said)
    assert (project / RULE_REL).read_text(encoding="utf-8") != RULE_TEXT
    assert not (fork.paths.state_dir / distribute.LEDGER_FILE).exists()


def test_adopt_while_git_is_in_the_middle_of_something_refuses(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    (project / ".git" / "MERGE_HEAD").write_text("", encoding="utf-8")
    before = support.tree_digest(project)

    code, said, prompts = adopt_project(fork, project)

    assert (code, prompts) == (common.EXIT_NEEDS_OWNER, [])
    assert "git is in the middle of something there (MERGE_HEAD)" in said
    assert support.tree_digest(project) == before


def test_adopt_names_an_untracked_file_that_would_be_overwritten(
    fork: support.Fork,
) -> None:
    # Not only deletions lose content: a file git does not track and the
    # update rewrites is gone just the same.
    project = make_project(fork)
    support.git(project, "rm", "--cached", "--quiet", RULE_REL)
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(f"{RULE_REL}\n", encoding="utf-8")
    commit_all(project, "keep our testing rules out of git")
    release(fork, RELEASE, "change a rule")

    code, said, _ = adopt_project(fork, project, answer="no")

    assert code == common.EXIT_OK
    assert "Git cannot bring back the present content of 1 of the files" in said
    assert f"  change  {RULE_REL}  (untracked)" in said


def test_adopt_on_a_revision_the_launcher_fell_back_to_is_refused(
    fork: support.Fork,
) -> None:
    project = make_project(fork)
    release(fork, RELEASE, "change a rule")
    script = run_py(fork)
    before = support.tree_digest(project)
    env = {**fork.env, common.CHECKOUT_FAILED_ENV: "2" * 40}

    done = support.run_with_terminal(
        [sys.executable, str(script), "adopt", str(project)], env, "yes\n"
    )

    assert done.returncode == common.EXIT_FAILED, done.stdout + done.stderr
    assert "could not be checked out; nothing done" in done.stdout
    assert support.tree_digest(project) == before
    assert not (fork.paths.state_dir / distribute.LEDGER_FILE).exists()
