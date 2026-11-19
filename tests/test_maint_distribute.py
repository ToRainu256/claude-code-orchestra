"""The distribution step: which projects it writes into, what, and when.

Every target here is a real git repository in a temp directory with the
template put there by the real installer. The fork carries the template this
checkout ships and the real ``scripts/update.sh``; the job's clone, state root
and home are scratch. Nothing reaches the network or the real home directory.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import maint_support as support
import pytest
from maint_support import adopt, common, distribute, maint_run, report, targets

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


def ledger(fork: support.Fork) -> dict:
    path = fork.paths.state_dir / distribute.LEDGER_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def states(fork: support.Fork) -> dict[str, str]:
    """directory name -> state, from the record of the last run."""
    return {Path(row["target"]).name: row["state"] for row in ledger(fork)["last"]}


def detail_of(fork: support.Fork, name: str) -> str:
    rows = [row for row in ledger(fork)["last"] if Path(row["target"]).name == name]
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


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_run_with_a_dirty_target_skips_it_and_leaves_every_byte(
    fork: support.Fork, kind: str
) -> None:
    project = adopted_project(fork)
    if kind == "untracked":
        (project / "notes.txt").write_text("not committed\n", encoding="utf-8")
    else:
        (project / "README.md").write_text("# edited\n", encoding="utf-8")
    if kind == "staged":
        support.git(project, "add", "README.md")
    approved = release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    first = distribute.run(fork.context(now=1000.0))
    second = distribute.run(fork.context(now=9000.0))

    # Waiting is not an error, and it says which revision waits since when.
    for result in (first, second):
        assert (result.result, result.exit_code) == ("waiting", common.EXIT_OK)
        assert result.counts == {"targets": 1, "waiting-dirty": 1}
    assert support.tree_digest(project) == before
    row = ledger(fork)["last"][0]
    assert (row["state"], row["pending"], row["since"]) == (
        "waiting-dirty",
        approved,
        1000.0,
    )
    assert "1 uncommitted paths" in row["detail"]


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
    row = ledger(fork)["last"][0]
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

    assert (result.result, result.exit_code) == (
        "needs-owner",
        common.EXIT_NEEDS_OWNER,
    )
    assert states(fork) == {"alpha": "not-adopted"}
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
    assert ledger(fork)["adopted"][str(project)]["revision"] == approved


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
    git = targets.TargetGit(fork.context(), str(project))

    with pytest.raises(common.Refused, match="not a reading command"):
        git.run([command, "--all"])

    assert support.tree_digest(project) == before


def test_run_fails_the_target_when_its_git_state_moved_during_the_update(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing the job runs can commit. Should that ever stop being true, the
    # comparison of HEAD, refs and index is what says so.
    adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    real_apply = targets.apply_changes

    def apply_and_commit(target: str, *rest: object) -> None:
        real_apply(target, *rest)
        support.git(Path(target), "commit", "--quiet", "--allow-empty", "-m", "sneaked")

    monkeypatch.setattr(targets, "apply_changes", apply_and_commit)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert "HEAD, refs or index changed" in detail_of(fork, "alpha")
    monkeypatch.undo()
    again = distribute.run(fork.context())
    assert (again.result, states(fork)) == ("needs-owner", {"alpha": "local-changes"})
    assert "stopped half-way" in detail_of(fork, "alpha")


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
    assert "Git does not track 1 of the files" in said
    assert "  delete  .claude/skills/own-skill/SKILL.md" in said
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


def test_run_does_not_write_when_the_project_changed_after_the_rehearsal(
    fork: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = adopted_project(fork)
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    def edit_meanwhile(_source: object, _staged: object) -> None:
        (project / RULE_REL).write_text(OWNER_TEXT, encoding="utf-8")

    monkeypatch.setattr(distribute, "unattended_problem", edit_meanwhile)

    result = distribute.run(fork.context())

    assert (result.result, result.exit_code) == ("target-failed", common.EXIT_FAILED)
    assert "not written" in detail_of(fork, "alpha")
    after = support.tree_digest(project)
    assert {rel for rel in after if after[rel] != before.get(rel)} == {RULE_REL}
    assert (project / RULE_REL).read_text(encoding="utf-8") == OWNER_TEXT
    assert ledger(fork)["adopted"][str(project)]["interrupted"] == ""


def test_run_with_another_repository_at_an_adopted_path_does_not_touch_it(
    fork: support.Fork,
) -> None:
    project = adopted_project(fork)
    shutil.move(str(project), str(project.with_name("moved-away")))
    support.make_project(project)
    release(fork, RELEASE, "change a rule")
    before = support.tree_digest(project)

    result = distribute.run(fork.context())

    assert result.exit_code == common.EXIT_NEEDS_OWNER
    assert states(fork) == {"alpha": "not-adopted"}
    assert support.tree_digest(project) == before


# --------------------------------------------------------------------------
# A hostile target
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "no-repository", "link"])
def test_run_with_a_target_that_is_not_a_plain_repository_refuses_it(
    fork: support.Fork, kind: str
) -> None:
    project = adopted_project(fork)
    elsewhere = project.with_name("elsewhere")
    if kind == "no-repository":
        shutil.move(str(project / ".git"), str(elsewhere))
    else:
        shutil.move(str(project), str(elsewhere))
    if kind == "link":
        project.symlink_to(elsewhere)
    release(fork, RELEASE, "change a rule")
    survivor = project if kind == "no-repository" else elsewhere
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
    if porcelain_without_hooks(project):
        commit_without_hooks(project)
    for path in marker.parent.glob(f"{marker.name}*"):
        path.unlink()
    release(fork, RELEASE, "change a rule")
    os.utime(project / "README.md", (3, 3))
    result = distribute.run(fork.context())

    assert code == common.EXIT_OK, said
    assert result.result == "distributed"
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert tripped(marker) == []


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
    assert targets.config_key_is_inert(key)


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
    assert not targets.config_key_is_inert(key)


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
        *[common.one_line(line) for line in distribute.status_lines(fork.paths)],
    ]
    assert result.result == "waiting"
    for text in printed:
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
    record = ledger(fork)["adopted"][str(project)]
    assert (record["revision"], record["interrupted"]) == (approved, "")


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
    assert scheduled.exit_code == common.EXIT_NEEDS_OWNER


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
    assert str(project) in ledger(fork)["adopted"]


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
    real_stage = distribute.stage_update

    def stage(ctx: object, source: object, target: str, name: str) -> object:
        if Path(target).name == "beta":
            raise RuntimeError("the disk said no")
        return real_stage(ctx, source, target, name)

    monkeypatch.setattr(distribute, "stage_update", stage)

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
    rows = ledger(fork)["last"]
    assert [(row["target"], row["state"]) for row in rows] == [
        (str(project), "updated"),
        ("projects/alpha", "refused"),
    ]
    assert result.exit_code == common.EXIT_NEEDS_OWNER


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "rebase-merge", "index.lock"])
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

    assert "| distribute | needs-owner | 2 |" in body
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

    lines = distribute.status_lines(fork.paths)

    assert len(lines) == 1
    assert f"target waiting-dirty  {project}" in lines[0]
    assert f"waits for {approved[:12]} since 1970-01-01 00:00 UTC" in lines[0]


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
        ("distribute", "needs-owner"),
        ("model-watch", "up-to-date"),
        ("report", "no-status-issue"),
    ]
    assert done.returncode == common.EXIT_NEEDS_OWNER, done.stdout + done.stderr
    assert (project / RULE_REL).read_text(encoding="utf-8") == RULE_TEXT
    assert f"target updated {project}" in status.stdout
    assert f"target not-adopted {waiting}" in status.stdout
