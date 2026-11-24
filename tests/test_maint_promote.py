"""Promotion: which merged revision the Mac may execute and distribute.

Also here, because promotion is where they matter most: the guards that keep
the job inside its own clone, and the renderer of the public status comment.
Scratch repositories and fakes only; see ``maint_support``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import maint_support as support
import pytest
from maint_support import (
    bump,
    common,
    maint_run,
    model_watch,
    never_merged_commit,
    plant_shadow_tag,
    promote,
    report,
)

DAY = 86400
NOW = 1_800_000_000.0
MAINT_RUN = support.MAINT_DIR / "run.py"


@pytest.fixture
def fork(tmp_path: Path) -> support.Fork:
    return support.make_fork(tmp_path)


def manifest_text(model: str, history: list[str], effort: str = "xhigh") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "claude": {"main": "fable"},
            "codex": {"model": model, "reasoning_effort": effort},
            "superseded": {"claude.main": [], "codex.model": history},
        }
    )


def config_text(model: str, extra: str = "") -> str:
    return (
        "# Default model configuration\n"
        f'model = "{model}"\n'
        'model_reasoning_effort = "xhigh"\n'
        f"{extra}"
        "\n[features]\nskills = true\n"
    )


def bump_problem(
    old: str = "gpt-7.1-astra",
    new: str = "gpt-7.2-astra",
    old_history: tuple[str, ...] = ("gpt-7-astra",),
    new_history: tuple[str, ...] = ("gpt-7-astra", "gpt-7.1-astra"),
    expected: str | None = None,
) -> str | None:
    return bump.model_bump_problem(
        manifest_text(old, list(old_history)),
        manifest_text(new, list(new_history)),
        config_text(old),
        config_text(new),
        expected,
    )


# --------------------------------------------------------------------------
# The shape of a pure model bump
# --------------------------------------------------------------------------


def test_model_bump_problem_with_one_bump_returns_none() -> None:
    assert bump_problem() is None


def test_model_bump_problem_with_two_bumps_in_a_row_returns_none() -> None:
    assert (
        bump_problem(
            new="gpt-7.10-astra",
            new_history=("gpt-7-astra", "gpt-7.1-astra", "gpt-7.9-astra"),
        )
        is None
    )


def test_model_bump_problem_with_expected_slug_pins_the_new_value() -> None:
    assert bump_problem(expected="gpt-7.2-astra") is None
    assert "not gpt-7.3-astra" in str(bump_problem(expected="gpt-7.3-astra"))


@pytest.mark.parametrize(
    ("new", "new_history", "fragment"),
    [
        ("gpt-7.0-astra", ("gpt-7-astra", "gpt-7.1-astra"), "does not move forward"),
        ("gpt-7.1-astra", ("gpt-7-astra",), "does not move forward"),
        ("gpt-7.2-sol", ("gpt-7-astra", "gpt-7.1-astra"), "changes family"),
        ("xyz-7.2-astra", ("gpt-7-astra", "gpt-7.1-astra"), "changes family"),
        ("latest", ("gpt-7-astra", "gpt-7.1-astra"), "is not a"),
        ("gpt-7.2-astra", ("gpt-7-astra",), "does not record"),
        ("gpt-7.2-astra", ("gpt-7.1-astra",), "does not record"),
        (
            "gpt-7.2-astra",
            ("gpt-7-astra", "gpt-7.1-astra", "gpt-9-sol"),
            "another family",
        ),
        (
            "gpt-7.2-astra",
            ("gpt-7-astra", "gpt-7.1-astra", "gpt-7.5-astra"),
            "outside the bump",
        ),
    ],
)
def test_model_bump_problem_with_wrong_manifest_change_names_it(
    new: str, new_history: tuple[str, ...], fragment: str
) -> None:
    assert fragment in str(bump_problem(new=new, new_history=new_history))


def test_model_bump_problem_with_another_manifest_key_changed_is_refused() -> None:
    problem = bump.model_bump_problem(
        manifest_text("gpt-7.1-astra", []),
        manifest_text("gpt-7.2-astra", ["gpt-7.1-astra"], effort="low"),
        config_text("gpt-7.1-astra"),
        config_text("gpt-7.2-astra"),
    )

    assert "more than the model" in str(problem)


def test_model_bump_problem_with_a_value_that_only_compares_equal_is_refused() -> None:
    # As Python objects `true == 1`; as a manifest they are different values.
    retyped = manifest_text("gpt-7.2-astra", ["gpt-7.1-astra"]).replace(
        '"schema_version": 1', '"schema_version": true'
    )

    problem = bump.model_bump_problem(
        manifest_text("gpt-7.1-astra", []),
        retyped,
        config_text("gpt-7.1-astra"),
        config_text("gpt-7.2-astra"),
    )

    assert "more than the model" in str(problem)


def test_model_bump_problem_with_duplicate_manifest_key_is_refused() -> None:
    doubled = manifest_text("gpt-7.2-astra", ["gpt-7.1-astra"]).replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
    )

    problem = bump.model_bump_problem(
        manifest_text("gpt-7.1-astra", []),
        doubled,
        config_text("gpt-7.1-astra"),
        config_text("gpt-7.2-astra"),
    )

    assert "unique keys" in str(problem)


@pytest.mark.parametrize(
    ("new_config", "fragment"),
    [
        (config_text("gpt-7.2-astra", 'approval_policy = "never"\n'), "gains or loses"),
        (
            config_text("gpt-7.2-astra").replace('effort = "xhigh"', 'effort = "low"'),
            "changes 2 lines",
        ),
        (config_text("gpt-7.3-astra"), "does not follow the manifest"),
        (
            config_text("gpt-7.1-astra").replace("# Default", "# Changed"),
            "not the model",
        ),
        (
            config_text("gpt-7.2-astra").replace(
                'model = "gpt-7.2-astra"', 'model  =  "gpt-7.2-astra" # x'
            ),
            "more than the model value",
        ),
    ],
)
def test_model_bump_problem_with_wrong_config_change_names_it(
    new_config: str, fragment: str
) -> None:
    problem = bump.model_bump_problem(
        manifest_text("gpt-7.1-astra", []),
        manifest_text("gpt-7.2-astra", ["gpt-7.1-astra"]),
        config_text("gpt-7.1-astra"),
        new_config,
    )

    assert fragment in str(problem)


def test_model_bump_problem_with_model_text_inside_a_string_is_refused() -> None:
    # The changed line only looks like the key: it is text in a multi-line
    # string, and the real model below it is still the old one.
    old = 'note = """\nmodel = "gpt-7.1-astra"\n"""\nmodel = "gpt-7.1-astra"\n'
    new = 'note = """\nmodel = "gpt-7.2-astra"\n"""\nmodel = "gpt-7.1-astra"\n'

    problem = bump.model_bump_problem(
        manifest_text("gpt-7.1-astra", []),
        manifest_text("gpt-7.2-astra", ["gpt-7.1-astra"]),
        old,
        new,
    )

    assert "multi-line string" in str(problem)


def test_model_bump_problem_with_model_line_inside_a_table_is_refused() -> None:
    old = '[profiles.x]\nmodel = "gpt-7.1-astra"\n'
    new = '[profiles.x]\nmodel = "gpt-7.2-astra"\n'

    problem = bump.model_bump_problem(
        manifest_text("gpt-7.1-astra", []),
        manifest_text("gpt-7.2-astra", ["gpt-7.1-astra"]),
        old,
        new,
    )

    assert "inside a table" in str(problem)


# --------------------------------------------------------------------------
# What the Mac executes or ships
# --------------------------------------------------------------------------


def test_shipped_paths_with_the_real_updater_is_understood() -> None:
    text = (support.REPO_ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")

    shipped = promote.shipped_paths(text)

    assert shipped is not None
    for expected in (
        ".claude/rules",
        ".claude/hooks",
        "AGENTS.md",
        "scripts",
        "VERSION",
    ):
        assert expected in shipped


def test_shipped_paths_includes_every_literal_template_source() -> None:
    in_place = support.UPDATER_TEMPLATE.format(seed=support.SEED_IN_PLACE)
    in_scaffold = support.UPDATER_TEMPLATE.format(seed=support.SEED_IN_SCAFFOLD)

    assert ".claude/STATE.md" in (promote.shipped_paths(in_place) or ())
    assert ".claude/STATE.md" not in (promote.shipped_paths(in_scaffold) or ())


@pytest.mark.parametrize(
    "broken",
    [
        support.UPDATER_TEMPLATE.format(seed="x").replace("SAFE_FILES=(", "OTHER=("),
        support.UPDATER_TEMPLATE.format(seed="x").replace('"AGENTS.md"', '"${EXTRA}"'),
        support.UPDATER_TEMPLATE.format(seed="x").replace(
            "TEMPLATE_DIR}/VERSION", "T}/VERSION"
        ),
        support.UPDATER_TEMPLATE.format(seed="x")
        .replace('    "AGENTS.md"\n', "")
        .replace('    "scripts/check.sh"\n', ""),
    ],
)
def test_shipped_paths_with_an_updater_it_cannot_read_returns_none(broken: str) -> None:
    assert promote.shipped_paths(broken) is None
    assert promote.shipped_paths(None) is None


def test_relevant_paths_separates_shipped_from_inert() -> None:
    shipped = promote.shipped_paths(
        support.UPDATER_TEMPLATE.format(seed="scripts/scaffold/S.md")
    )
    changed = [
        "README.md",
        "PROGRESS.md",
        "tests/test_x.py",
        ".github/workflows/ci.yml",
        ".claude/STATE.md",
        ".claude/docs/plans/a.md",
        ".claude/hooks/x.py",
        ".claude/skills/_shared/models.py",
        "scripts/maint/run.py",
        "AGENTS.md",
        "VERSION",
        ".codex/config.toml",
    ]

    relevant = promote.relevant_paths(changed, shipped)

    assert relevant == sorted(
        [
            ".claude/hooks/x.py",
            ".claude/skills/_shared/models.py",
            "scripts/maint/run.py",
            "AGENTS.md",
            "VERSION",
            ".codex/config.toml",
        ]
    )


@pytest.mark.parametrize(
    "path",
    [
        "Scripts/Maint/run.py",  # another case of a path the Mac runs
        ".CLAUDE/hooks/x.py",
        ".claude",  # a parent of shipped paths, replaced by a file or a link
        "scripts",
        "tests/café.py",  # not plain ASCII: never inert
        "tests/a\tb.py",
    ],
)
def test_relevant_paths_with_a_disguised_path_is_never_inert(path: str) -> None:
    shipped = promote.shipped_paths(support.UPDATER_TEMPLATE.format(seed="x"))

    assert promote.relevant_paths([path], shipped) == [path]


@pytest.mark.parametrize(
    "path",
    [
        ".gitattributes",
        "tests/.gitattributes",
        "docs/deep/.GitAttributes",
        ".gitmodules",
        ".lfsconfig",
    ],
)
def test_relevant_paths_with_a_file_git_interprets_is_never_inert(path: str) -> None:
    # An attribute can re-encode a script or route it through a filter at
    # checkout, without the script's own path appearing in the diff.
    shipped = promote.shipped_paths(support.UPDATER_TEMPLATE.format(seed="x"))

    assert promote.relevant_paths([path, "README.md"], shipped) == [path]


def test_relevant_paths_without_shipped_lists_treats_everything_as_relevant() -> None:
    assert promote.relevant_paths(["README.md", "b"], None) == ["README.md", "b"]


def test_parse_bash_array_reads_literals_and_skips_comments() -> None:
    text = 'A=(\n    "x/y"\n    # note\n\n    ".z"\n)\nB=(\n)\n'

    assert promote.parse_bash_array(text, "A") == ("x/y", ".z")
    assert promote.parse_bash_array(text, "B") == ()
    assert promote.parse_bash_array(text, "C") is None
    assert promote.parse_bash_array('A=(\n    "x"\n', "A") is None


# --------------------------------------------------------------------------
# The scheduled step, on scratch repositories
# --------------------------------------------------------------------------


def test_auto_promote_with_nothing_new_is_up_to_date(fork: support.Fork) -> None:
    before = fork.approved()

    result = promote.auto_promote(fork.context())

    assert (result.result, result.exit_code) == ("up-to-date", common.EXIT_OK)
    assert fork.approved() == before


def test_auto_promote_with_state_only_range_promotes(fork: support.Fork) -> None:
    fork.commit({".claude/STATE.md": "# Agent State\n\nworking\n"}, "state")
    head = fork.commit(
        {
            "PROGRESS.md": "progress\n",
            ".claude/docs/plans/plan.md": "plan\n",
            "tests/test_new.py": "def test_new():\n    assert 1 + 1 == 2\n",
            ".github/workflows/ci.yml": "name: CI\n",
            "README.md": "# changed\n",
        },
        "docs, tests and CI (#12)",
    )
    fork.fetch()

    result = promote.auto_promote(fork.context())

    assert (result.result, result.exit_code) == ("promoted", common.EXIT_OK)
    assert promote.REASON_INERT in result.detail
    assert fork.approved() == head


def test_auto_promote_with_state_seed_still_shipped_waits(tmp_path: Path) -> None:
    # Until the seeds move out of the canonical paths, the updater copies this
    # file into projects, so a change to it is a shipped change.
    fork = support.make_fork(tmp_path, updater_seed=support.SEED_IN_PLACE)
    before = fork.approved()
    fork.commit({".claude/STATE.md": "# Agent State\n\nworking\n"}, "state")
    fork.fetch()

    result = promote.auto_promote(fork.context())

    assert result.result == "awaiting-promotion"
    assert ".claude/STATE.md" in result.detail
    assert fork.approved() == before


def merge_own_bump(fork: support.Fork) -> str:
    """Let this Mac's model watch open the bump, then merge it the way GitHub
    does. Returns the new head of the default branch."""
    fork.set_catalog(support.entry(support.CURRENT), support.entry(support.NEWER))
    assert model_watch.run(fork.context()).result == "pr-opened"
    head = fork.squash_merge(model_watch.BRANCH_PREFIX + support.NEWER)
    fork.fetch()
    return head


def test_auto_promote_with_this_macs_own_bump_promotes(fork: support.Fork) -> None:
    head = merge_own_bump(fork)

    result = promote.auto_promote(fork.context())

    assert (result.result, result.exit_code) == ("promoted", common.EXIT_OK)
    assert promote.REASON_BUMP in result.detail
    assert fork.approved() == head


def test_auto_promote_with_bump_merged_by_other_means_waits(
    fork: support.Fork,
) -> None:
    # Exactly the shape of a bump, but no model watch on this Mac built it,
    # checked the catalog for it, or made a live call with it.
    before = fork.approved()
    fork.bump(support.NEWER)
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert (result.result, result.exit_code) == ("awaiting-promotion", common.EXIT_OK)
    assert "no model watch on this Mac tested this bump" in result.detail
    assert fork.approved() == before


def test_auto_promote_with_a_slug_nobody_verified_waits(fork: support.Fork) -> None:
    before = fork.approved()
    fork.bump(f"gpt-9999.1-{support.FAMILY}")
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert fork.approved() == before
    assert fork.codex_state()["calls"] == []


def test_auto_promote_with_own_bump_altered_before_the_merge_waits(
    fork: support.Fork,
) -> None:
    # The model watch tested one pair of files; what was merged is another
    # pair for the same slug (the history is padded), still bump-shaped.
    before = fork.approved()
    fork.set_catalog(support.entry(support.CURRENT), support.entry(support.NEWER))
    assert model_watch.run(fork.context()).result == "pr-opened"
    fork.bump(support.NEWER)
    manifest = json.loads(
        (fork.seed / support.MANIFEST_REL).read_text(encoding="utf-8")
    )
    manifest["superseded"]["codex.model"].append(f"gpt-7.1.5-{support.FAMILY}")
    fork.commit({support.MANIFEST_REL: json.dumps(manifest, indent=2) + "\n"}, "pad")
    fork.fetch()
    ctx = fork.context(now=time.time())

    result = promote.auto_promote(ctx)

    assert (
        bump.bump_problem_between(
            ctx.git, str(before), str(ctx.git.commit_of(common.MAIN_REF)), only=False
        )
        is None
    )  # the shape alone would have passed
    assert result.result == "awaiting-promotion"
    assert "the two files are not the ones the model watch tested" in result.detail
    assert fork.approved() == before


def test_auto_promote_with_own_bump_and_inert_changes_promotes(
    fork: support.Fork,
) -> None:
    merge_own_bump(fork)
    head = fork.commit({"README.md": "# after the bump\n"}, "readme")
    fork.fetch()

    result = promote.auto_promote(fork.context())

    assert result.result == "promoted"
    assert fork.approved() == head


def test_auto_promote_with_bump_mixed_with_a_hook_change_waits(
    fork: support.Fork,
) -> None:
    before = fork.approved()
    fork.bump(support.NEWER)
    fork.commit({support.HOOK_REL: "print('changed hook')\n"}, "hook")
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert (result.result, result.exit_code) == ("awaiting-promotion", common.EXIT_OK)
    assert result.counts == {"commits": 2}
    assert support.HOOK_REL in result.detail
    assert fork.approved() == before


def test_auto_promote_with_hook_change_reverted_in_range_promotes(
    fork: support.Fork,
) -> None:
    original = (fork.seed / support.HOOK_REL).read_text(encoding="utf-8")
    fork.commit({support.HOOK_REL: "print('changed hook')\n"}, "hook")
    head = fork.commit({support.HOOK_REL: original}, "revert hook")
    fork.fetch()

    result = promote.auto_promote(fork.context())

    # The Mac only ever runs the tree at the end of the range.
    assert result.result == "promoted"
    assert fork.approved() == head


@pytest.mark.parametrize(
    "files",
    [
        {"scripts/maint/run.py": "print('x')\n"},
        {"scripts/update.sh": support.UPDATER_TEMPLATE.format(seed="x") + "# edit\n"},
        {"VERSION": "9.9.9\n"},
        {".claude/settings.json": "{}\n"},
        {"AGENTS.md": "# contract\n"},
        {".claude/rules/new.md": "rule\n"},
        {"Scripts/tool.sh": "echo hi\n"},
    ],
)
def test_auto_promote_with_executed_or_shipped_change_waits(
    fork: support.Fork, files: dict[str, str]
) -> None:
    before = fork.approved()
    fork.commit(files, "change")
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert (result.result, result.exit_code) == ("awaiting-promotion", common.EXIT_OK)
    assert fork.approved() == before


def test_auto_promote_with_attributes_file_waits(fork: support.Fork) -> None:
    before = fork.approved()
    fork.commit(
        {".gitattributes": "scripts/maint/*.py working-tree-encoding=UTF-16\n"},
        "attributes",
    )
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert ".gitattributes" in result.detail
    assert fork.approved() == before


def test_auto_promote_with_family_switch_is_not_a_model_bump(
    fork: support.Fork,
) -> None:
    before = fork.approved()
    fork.bump(f"gpt-7.5-{support.OTHER_FAMILY}")
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert "not a model bump this Mac tested" in result.detail
    assert fork.approved() == before


def test_auto_promote_with_downgrade_is_not_a_model_bump(fork: support.Fork) -> None:
    before = fork.approved()
    fork.bump(f"gpt-7.0-{support.FAMILY}")
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert fork.approved() == before


def test_auto_promote_with_hand_edited_bump_is_not_a_model_bump(
    fork: support.Fork,
) -> None:
    before = fork.approved()
    fork.bump(support.NEWER)
    config = (fork.seed / support.CONFIG_REL).read_text(encoding="utf-8")
    fork.commit(
        {
            support.CONFIG_REL: config.replace(
                'approval_policy = "never"', 'approval_policy = "x"'
            )
        },
        "also the approval policy",
    )
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert fork.approved() == before


def test_auto_promote_waiting_longer_than_a_week_needs_the_owner(
    fork: support.Fork,
) -> None:
    fork.commit({support.HOOK_REL: "print('changed hook')\n"}, "hook")
    fork.fetch()

    fresh = promote.auto_promote(fork.context(now=time.time() + 6 * DAY))
    overdue = promote.auto_promote(fork.context(now=time.time() + 8 * DAY))

    assert fresh.exit_code == common.EXIT_OK
    assert (overdue.result, overdue.exit_code) == (
        "awaiting-promotion",
        common.EXIT_NEEDS_OWNER,
    )


def test_auto_promote_with_non_ancestor_needs_the_owner(fork: support.Fork) -> None:
    before = fork.approved()
    support.git(fork.seed, "commit", "--quiet", "--amend", "-m", "rewritten seed")
    support.git(fork.seed, "push", "--quiet", "--force", "origin", "HEAD:main")
    fork.fetch()

    result = promote.auto_promote(fork.context())

    assert (result.result, result.exit_code) == ("diverged", common.EXIT_NEEDS_OWNER)
    assert fork.approved() == before


def test_auto_promote_without_approved_revision_is_a_configuration_error(
    fork: support.Fork,
) -> None:
    fork.paths.approved_file.write_text("not-a-sha\n", encoding="utf-8")

    result = promote.auto_promote(fork.context())

    assert (result.result, result.exit_code) == (
        "no-approved-revision",
        common.EXIT_USAGE,
    )


def test_auto_promote_without_updater_at_approved_revision_waits(
    fork: support.Fork,
) -> None:
    fork.approve(fork.commit({"scripts/update.sh": None}, "drop the updater"))
    before = fork.approved()
    fork.commit({"README.md": "# harmless\n"}, "readme")
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    # Without the lists nothing can be called inert, not even a README.
    assert result.result == "awaiting-promotion"
    assert "cannot read the shipped lists" in result.detail
    assert fork.approved() == before


def test_write_approved_rejects_anything_but_a_full_sha(fork: support.Fork) -> None:
    before = fork.approved()

    for bad in ("main", "abc123", "A" * 40, "0" * 40 + "\n"):
        with pytest.raises(ValueError):
            promote.write_approved(fork.paths, bad)

    assert fork.approved() == before
    assert oct(fork.paths.approved_file.stat().st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------
# The owner's command
# --------------------------------------------------------------------------


def run_manual(fork: support.Fork, answer: str) -> tuple[int, str, list[str]]:
    lines: list[str] = []
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    code = promote.manual_promote(fork.context(), ask, lines.append)
    return code, "\n".join(lines), prompts


def test_manual_promote_shows_the_range_and_advances_on_yes(fork: support.Fork) -> None:
    fork.commit({support.HOOK_REL: "print('changed hook')\n"}, "change a hook (#34)")
    head = fork.fetch()

    code, shown, prompts = run_manual(fork, "yes")

    assert code == common.EXIT_OK
    assert "change a hook (#34)" in shown
    # The link is to the commit itself. A subject can name any pull request.
    assert f"{common.FORK_WEB_URL}/commit/{head}" in shown
    assert "/pull/" not in shown
    assert f"executed or shipped: {support.HOOK_REL}" in shown
    assert head in prompts[0]
    assert fork.approved() == head


@pytest.mark.parametrize("answer", ["", "y", "YES", "no", "yes please"])
def test_manual_promote_with_any_other_answer_changes_nothing(
    fork: support.Fork, answer: str
) -> None:
    before = fork.approved()
    fork.commit({support.HOOK_REL: "print('changed hook')\n"}, "hook")
    fork.fetch()

    code, shown, _ = run_manual(fork, answer)

    assert code == common.EXIT_OK
    assert "not confirmed" in shown
    assert fork.approved() == before


def test_manual_promote_with_nothing_waiting_does_not_ask(fork: support.Fork) -> None:
    code, shown, prompts = run_manual(fork, "yes")

    assert code == common.EXIT_OK
    assert prompts == []
    assert "already" in shown


def test_manual_promote_with_rewritten_history_shows_what_differs_before_asking(
    fork: support.Fork,
) -> None:
    # The default branch was rewritten, and the new history also changes a
    # hook and adds a file whose name tries to redraw the screen.
    before = str(fork.approved())
    support.git(fork.seed, "commit", "--quiet", "--amend", "-m", "rewritten seed")
    name = "zz\x1b[2J\x1b[HNothing changed."
    for rel, text in ((support.HOOK_REL, "print('rewritten hook')\n"), (name, "x\n")):
        (fork.seed / rel).write_text(text, encoding="utf-8")
    support.git(fork.seed, "add", "-A")
    support.git(fork.seed, "commit", "--quiet", "-m", "more, after the rewrite")
    support.git(fork.seed, "push", "--quiet", "--force", "origin", "HEAD:main")
    head = fork.fetch()
    lines: list[str] = []
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append("\n".join(lines))  # what was on screen when asked
        return "yes"

    code = promote.manual_promote(fork.context(), ask, lines.append)

    on_screen = prompts[0]
    assert code == common.EXIT_OK
    assert "WARNING" in on_screen
    # The diff stat of the two trees, which needs no shared history.
    assert re.search(r"example-hook\.py +\| +\d", on_screen)
    assert f"executed or shipped: {support.HOOK_REL}" in on_screen
    assert f"Inspect: git -C {fork.paths.clone} diff {before} {head}" in on_screen
    assert f"{common.FORK_WEB_URL}/commit/{head}" in on_screen
    assert set(on_screen) <= PRINTABLE | {"\n"}
    assert fork.approved() == head


def test_manual_promote_when_the_trees_cannot_be_compared_refuses(
    fork: support.Fork,
) -> None:
    # An approved revision this clone no longer has: nothing can be shown,
    # so nothing is asked.
    fork.approve("0123456789abcdef0123456789abcdef01234567")
    fork.commit({"README.md": "# new\n"}, "docs")
    fork.fetch()

    code, _, prompts = run_manual(fork, "yes")

    assert (code, prompts) == (common.EXIT_FAILED, [])
    assert fork.approved() == "0123456789abcdef0123456789abcdef01234567"


# --------------------------------------------------------------------------
# Staying inside the clone the job owns
# --------------------------------------------------------------------------


def test_git_refuses_a_directory_outside_the_state_root(
    fork: support.Fork, tmp_path: Path
) -> None:
    ctx = fork.context()

    for outside in (support.REPO_ROOT, tmp_path, fork.seed, fork.paths.state_root):
        with pytest.raises(common.Refused):
            ctx.git.run(["status"], cwd=outside)


def test_git_refuses_a_clone_that_is_a_link_to_another_checkout(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "fork").symlink_to(support.REPO_ROOT, target_is_directory=True)
    ctx = common.make_context(
        common.Paths(state_root), support.REPO_ROOT, common.Log(None)
    )

    with pytest.raises(common.Refused):
        ctx.git.run(["status"])
    with pytest.raises(common.Refused):
        common.require_owned_clone(ctx.git)


def test_git_refuses_a_scratch_path_that_links_out_of_the_run_directory(
    fork: support.Fork,
) -> None:
    fork.paths.run_dir.mkdir(parents=True, exist_ok=True)
    link = fork.paths.run_dir / "model-watch"
    link.symlink_to(fork.seed, target_is_directory=True)

    with pytest.raises(common.Refused):
        fork.context().git.run(["status"], cwd=link)


def test_require_owned_clone_accepts_the_scratch_clone(fork: support.Fork) -> None:
    common.require_owned_clone(fork.context().git)


def test_require_owned_clone_refuses_another_origin(fork: support.Fork) -> None:
    support.git(
        fork.paths.clone,
        "remote",
        "set-url",
        "origin",
        "https://github.com/DeL-TaiseiOzaki/claude-code-orchestra.git",
    )

    with pytest.raises(common.Refused, match="not a clone of"):
        common.require_owned_clone(fork.context().git)


def test_require_owned_clone_refuses_a_separate_push_url(fork: support.Fork) -> None:
    support.git(
        fork.paths.clone,
        "config",
        "remote.origin.pushurl",
        "https://github.com/DeL-TaiseiOzaki/claude-code-orchestra.git",
    )

    with pytest.raises(common.Refused, match="does not fetch from and push to"):
        common.require_owned_clone(fork.context().git)


def test_require_owned_clone_refuses_a_repository_kept_elsewhere(
    fork: support.Fork,
) -> None:
    elsewhere = fork.root / "moved.git"
    (fork.paths.clone / ".git").rename(elsewhere)
    (fork.paths.clone / ".git").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(common.Refused, match="is a link"):
        common.require_owned_clone(fork.context().git)


@pytest.mark.parametrize("name", ["run", "state", "logs", "bin"])
def test_managed_directory_that_is_a_link_is_refused(
    fork: support.Fork, name: str
) -> None:
    outside = fork.root / f"outside-{name}"
    managed = fork.paths.state_root / name
    if managed.exists():
        managed.rename(outside)
    else:
        outside.mkdir()
    managed.symlink_to(outside, target_is_directory=True)
    ctx = fork.context()

    with pytest.raises(common.Refused, match="is a link"):
        common.require_owned_clone(ctx.git)
    if name == "run":
        with pytest.raises(common.Refused):
            ctx.git.run(["status"], cwd=managed / "model-watch")
        with pytest.raises(common.Refused):
            common.require_scratch(ctx.paths, managed / "smoke-1")


def test_gh_environment_that_names_another_host_is_not_passed_on(
    fork: support.Fork,
) -> None:
    env = {**fork.env, "GH_HOST": "ghe.example", "GH_REPO": "other/repo"}

    ctx = common.make_context(fork.paths, support.REPO_ROOT, common.Log(None), env=env)

    assert "GH_HOST" not in ctx.env and "GH_REPO" not in ctx.env
    assert ctx.github.login() == common.FORK_OWNER
    assert fork.gh_state()["calls"] == [
        ["api", "--hostname", "github.com", "user", "--jq", ".login"]
    ]


def test_require_owned_clone_refuses_a_second_remote(fork: support.Fork) -> None:
    support.git(fork.paths.clone, "remote", "add", "upstream", str(fork.bare))

    with pytest.raises(common.Refused, match="exactly one remote"):
        common.require_owned_clone(fork.context().git)


def test_require_owned_clone_refuses_a_missing_clone(tmp_path: Path) -> None:
    ctx = common.make_context(
        common.Paths(tmp_path / "none"), tmp_path, common.Log(None)
    )

    with pytest.raises(common.Refused, match="does not exist"):
        common.require_owned_clone(ctx.git)


def run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MAINT_RUN), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("command", ["run", "promote", "status"])
def test_cli_from_a_developer_checkout_refuses_and_creates_nothing(
    tmp_path: Path, command: str
) -> None:
    state_root = tmp_path / "state"
    env = {**os.environ, "HOME": str(tmp_path / "home")}

    done = run_cli(["--state-root", str(state_root), command], env)

    assert done.returncode == common.EXIT_USAGE
    assert "refused" in done.stderr
    assert not state_root.exists()
    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("command", ["run", "status"])
def test_cli_with_a_valid_clone_still_refuses_code_from_elsewhere(
    fork: support.Fork, command: str
) -> None:
    before = sorted(
        str(p) for p in fork.paths.state_root.rglob("*") if ".git" not in p.parts
    )

    done = run_cli([command], fork.env)

    assert done.returncode == common.EXIT_USAGE
    assert "runs only from" in done.stderr
    assert before == sorted(
        str(p) for p in fork.paths.state_root.rglob("*") if ".git" not in p.parts
    )
    assert fork.gh_state()["calls"] == []


def test_state_root_comes_from_flag_then_environment_then_home(tmp_path: Path) -> None:
    flag = tmp_path / "flag"
    env = {common.STATE_ROOT_ENV: str(tmp_path / "env")}

    assert common.resolve_state_root(flag, env) == flag
    assert common.resolve_state_root(None, env) == tmp_path / "env"
    assert common.resolve_state_root(None, {}) == Path.home() / ".claude" / "orchestra"


# --------------------------------------------------------------------------
# Plumbing: deadline, lock, log, public comment
# --------------------------------------------------------------------------


def test_run_command_kills_a_command_that_exceeds_its_deadline(tmp_path: Path) -> None:
    # A shell that starts a long-running grandchild and waits for it. The
    # grandchild records its own PID so its fate can be checked afterwards.
    pid_file = tmp_path / "pid"
    script = (
        "import os, time, pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(120)"
    )
    started = time.monotonic()

    result = common.run_command(
        ["/bin/sh", "-c", f'"{sys.executable}" -c "{script}" & wait'], timeout=2
    )

    assert result.timed_out and not result.ok
    assert time.monotonic() - started < 60
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    # Killed with its group, not left holding the pipe: within a moment the
    # PID no longer names a process.
    gone = False
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    assert gone


def test_run_command_with_a_missing_program_reports_it() -> None:
    result = common.run_command(["no-such-program-anywhere"], timeout=5)

    assert result.missing and not result.ok
    assert "could not be started" in result.failure()


def test_acquire_lock_twice_gives_the_second_caller_nothing(fork: support.Fork) -> None:
    first = common.acquire_lock(fork.paths)

    assert first is not None
    assert common.acquire_lock(fork.paths) is None
    os.close(first)
    again = common.acquire_lock(fork.paths)
    assert again is not None
    os.close(again)


def test_open_run_log_keeps_only_the_newest_logs(fork: support.Fork) -> None:
    fork.paths.logs_dir.mkdir(parents=True)
    for index in range(common.LOGS_KEPT + 5):
        (fork.paths.logs_dir / f"20260101T0000{index:02d}Z.log").write_text("x")

    log = common.open_run_log(fork.paths, NOW)
    log.line("first\nsecond line")

    names = sorted(path.name for path in fork.paths.logs_dir.glob("*.log"))
    assert len(names) == common.LOGS_KEPT
    assert "20260101T000000Z.log" not in names
    assert log.path is not None
    assert log.path.read_text(encoding="utf-8").count("\n") == 1  # one event, one line
    assert oct(log.path.stat().st_mode & 0o777) == "0o600"


def test_render_status_comment_prints_outcomes_and_counts_only() -> None:
    steps = [
        common.StepResult(
            "promote",
            "awaiting-promotion",
            common.EXIT_NEEDS_OWNER,
            "/Users/someone/Workspace/secret-project/.claude/hooks/x.py changed",
            {"commits": 3},
        ),
        common.StepResult(
            "distribute", "ok", 0, "secret-project skipped", {"skipped": 1, "synced": 2}
        ),
        common.StepResult("model-watch", "see /Users/someone/x", 0, "detail"),
    ]

    body = report.render(steps, "a" * 40, "b" * 40, 3, NOW)

    assert body.startswith("<!-- job:local-maint -->\n")
    assert (
        "approved `aaaaaaaaaaaa` · main `bbbbbbbbbbbb` · awaiting promotion: 3" in body
    )
    assert "| promote | awaiting-promotion | 2 | commits 3 |" in body
    assert "| distribute | ok | 0 | skipped 1, synced 2 |" in body
    assert "| model-watch | unprintable | 0 |  |" in body
    assert "STALE" not in body  # no job is watched that does not exist
    assert "exit 2" in body
    for private in ("secret-project", "/Users", "hooks", "detail", "someone"):
        assert private not in body


def test_parse_slug_orders_versions_numerically() -> None:
    def version(text: str) -> tuple[int, ...]:
        slug = common.parse_slug(text)
        assert slug is not None
        return slug.version

    assert version("gpt-6-astra") < version("gpt-6.1-astra") < version("gpt-6.9-astra")
    assert version("gpt-6.9-astra") < version("gpt-6.10-astra") < version("gpt-7-astra")
    assert version("gpt-6.0-astra") == version("gpt-6-astra")


@pytest.mark.parametrize(
    "text",
    [
        "gpt-6.2-astra\n",
        "gpt-6.2-astra; rm -rf ~",
        "gpt-6.2-astra --repo other/repo",
        "-gpt-6.2-astra",
        "gpt-6.2",
        "gpt-reserve",
        "codex-auto-review",
        "GPT-6.2-ASTRA",
        "gpt-6.2-astra/../../x",
        "gpt-99999-astra",
        "",
        None,
        62,
    ],
)
def test_parse_slug_rejects_anything_but_a_plain_slug(text: object) -> None:
    assert common.parse_slug(text) is None


# --------------------------------------------------------------------------
# Review round: refs, modes, line endings, what is shown, ambient settings
# --------------------------------------------------------------------------

PRINTABLE = {chr(code) for code in range(0x20, 0x7F)}
PAYLOADS = {
    "escape": "\x1b[2J\x1b[H",
    "carriage-return": "\r",
    "bell": "\x07",
    "c1-control": "\x9b2J",
    "bidi-override": "\u202e",
}
THIN_UPDATER = """SAFE_DIRS=(
    ".claude/rules"
)
SAFE_FILES=(
    "AGENTS.md"
)
SETTINGS_FILES=(
    "unrelated/settings.toml"
)
cp -f "${TEMPLATE_DIR}/VERSION" "${PROJECT_ROOT}/.claude/orchestra-version"
"""


def test_fetch_never_brings_a_tag_that_shadows_the_branch(fork: support.Fork) -> None:
    side = never_merged_commit(fork, {"scripts/backdoor.sh": "echo owned\n"})
    plant_shadow_tag(fork, side, in_clone=False)
    real = fork.commit({"README.md": "# real\n"}, "docs")

    fetched = fork.fetch()

    assert fetched == real
    assert support.git(fork.paths.clone, "tag", "-l") == ""
    common.require_owned_clone(fork.context().git)


def test_evaluate_reads_the_branch_by_its_full_name_past_a_shadowing_tag(
    fork: support.Fork,
) -> None:
    # Even with the tag inside the clone, the decision is about the branch.
    side = never_merged_commit(fork, {"README.md": "# never merged\n"})
    plant_shadow_tag(fork, side, in_clone=True)
    real = fork.commit({support.HOOK_REL: "print('real change')\n"}, "hook")
    fork.fetch()
    ctx = fork.context(now=time.time())

    decision = promote.evaluate(ctx)

    assert decision.target == real != side
    assert decision.action == "await"  # the tag's commit alone would be inert
    assert promote.awaiting_summary(ctx)[1] == real


def test_require_owned_clone_refuses_a_clone_that_holds_a_tag(
    fork: support.Fork,
) -> None:
    side = never_merged_commit(fork, {"README.md": "# never merged\n"})
    plant_shadow_tag(fork, side, in_clone=True)
    before = fork.approved()

    with pytest.raises(common.Refused, match="refs the job never creates"):
        common.require_owned_clone(fork.context().git)
    assert fork.approved() == before


@pytest.mark.parametrize(
    "ref", ["refs/heads/origin/main", "refs/origin/main", "refs/heads/main"]
)
def test_require_owned_clone_refuses_any_ref_the_job_never_creates(
    fork: support.Fork, ref: str
) -> None:
    support.git(fork.paths.clone, "update-ref", ref, str(fork.approved()))

    with pytest.raises(common.Refused, match="refs the job never creates"):
        common.require_owned_clone(fork.context().git)


def test_require_owned_clone_refuses_a_clone_install_did_not_create(
    fork: support.Fork,
) -> None:
    support.git(fork.paths.clone, "config", "--unset", common.OWNERSHIP_KEY)

    with pytest.raises(common.Refused, match="was not created by"):
        common.require_owned_clone(fork.context().git)


def test_require_owned_clone_refuses_a_rewrite_rule_for_the_fork_url(
    fork: support.Fork,
) -> None:
    support.git(
        fork.paths.clone,
        "config",
        "url.https://github.com/DeL-TaiseiOzaki/.insteadOf",
        "https://github.com/ToRainu256/",
    )

    with pytest.raises(common.Refused, match="does not fetch from and push to"):
        common.require_owned_clone(fork.context().git)


def link_typed_bump(fork: support.Fork) -> str:
    """A bump whose configuration entry is a link whose target text is the
    bumped file: the content comparison alone would pass it."""
    for args in (["set", "codex.model", support.NEWER], ["apply"]):
        support.run_models(fork.seed, args)
    blob = (
        subprocess.run(
            [support.REAL_GIT, "-C", str(fork.seed), "hash-object", "-w", "--stdin"],
            input=(fork.seed / support.CONFIG_REL).read_bytes(),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    support.git(fork.seed, "add", support.MANIFEST_REL)
    support.git(
        fork.seed, "update-index", "--cacheinfo", f"120000,{blob},{support.CONFIG_REL}"
    )
    support.git(fork.seed, "commit", "--quiet", "-m", "chore(models): bump")
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    return support.git(fork.seed, "rev-parse", "HEAD")


def test_bump_with_a_link_typed_entry_is_not_a_bump(fork: support.Fork) -> None:
    before = str(fork.approved())
    target = link_typed_bump(fork)
    fork.fetch()
    ctx = fork.context(now=time.time())

    problem = bump.bump_problem_between(ctx.git, before, target)
    result = promote.auto_promote(ctx)

    assert "is not a regular file with mode 100644" in str(problem)
    assert result.result == "awaiting-promotion"
    assert "not a plain file: .codex/config.toml (mode 120000)" in result.detail
    assert fork.approved() == before


def test_bump_with_a_mode_change_is_not_a_bump(fork: support.Fork) -> None:
    before = str(fork.approved())
    for args in (["set", "codex.model", support.NEWER], ["apply"]):
        support.run_models(fork.seed, args)
    support.git(fork.seed, "add", "-A")
    support.git(fork.seed, "update-index", "--chmod=+x", support.MANIFEST_REL)
    support.git(fork.seed, "commit", "--quiet", "-m", "bump, and executable")
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    fork.fetch()
    ctx = fork.context()

    problem = bump.bump_problem_between(
        ctx.git, before, str(ctx.git.commit_of(common.MAIN_REF))
    )

    assert "is not a regular file with mode 100644" in str(problem)


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.replace("\n", "\r\n"),
        lambda text: text.replace("configuration\n", "configuration\r"),
        lambda text: text.replace('"xhigh"\n', '"xhigh"\r\n'),
    ],
    ids=["crlf-everywhere", "bare-cr-joins-two-lines", "one-line-gains-cr"],
)
def test_model_bump_problem_sees_a_change_of_line_endings(transform: object) -> None:
    assert callable(transform)
    new_config = transform(config_text("gpt-7.2-astra"))
    assert new_config != config_text("gpt-7.2-astra")

    problem = bump.model_bump_problem(
        manifest_text("gpt-7.1-astra", []),
        manifest_text("gpt-7.2-astra", ["gpt-7.1-astra"]),
        config_text("gpt-7.1-astra"),
        new_config,
    )

    assert problem is not None


def test_bump_problem_between_reads_carriage_returns_from_git_unchanged(
    fork: support.Fork,
) -> None:
    before = str(fork.approved())
    for args in (["set", "codex.model", support.NEWER], ["apply"]):
        support.run_models(fork.seed, args)
    config = fork.seed / support.CONFIG_REL
    config.write_bytes(config.read_bytes().replace(b"\n", b"\r\n"))
    support.git(fork.seed, "add", "-A")
    support.git(fork.seed, "commit", "--quiet", "-m", "bump")
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    fork.fetch()
    ctx = fork.context()

    problem = bump.bump_problem_between(
        ctx.git, before, str(ctx.git.commit_of(common.MAIN_REF))
    )

    assert problem is not None and support.CONFIG_REL in problem


@pytest.mark.parametrize("payload", sorted(PAYLOADS))
def test_manual_promote_never_sends_a_control_character_to_the_terminal(
    fork: support.Fork, payload: str
) -> None:
    # What the fork controls: a commit subject and a file name. The name is
    # not plain, so it is always listed, last, right above the prompt.
    code = PAYLOADS[payload]
    name = f"zz{code}Commits: docs only. Nothing executed or shipped changed."
    fork.commit(
        {"scripts/backdoor.sh": "echo owned\n", name: "x\n"}, f"docs: typo{code}hidden"
    )
    fork.fetch()
    lines: list[str] = []
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "no"

    promote.manual_promote(fork.context(), ask, lines.append)

    assert prompts and lines
    for line in [*lines, *prompts]:
        assert set(line) <= PRINTABLE, repr(line)
    shown = "\n".join(lines)
    assert "scripts/backdoor.sh" in shown
    assert "docs: typo\\" in shown  # the payload is visible as an escape


def test_safe_text_escapes_everything_but_printable_ascii() -> None:
    hostile = "ok\x1b[2J\r\n\t\x07\x7f\x9b\u202e\u2066\U0001f600 end"

    shown = common.safe_text(hostile)

    assert set(shown) <= PRINTABLE
    assert shown.startswith("ok\\x1b[2J\\x0d\\x0a\\x09\\x07\\x7f\\x9b\\u202e")
    assert len(common.safe_text("x" * 5000)) == common.DISPLAY_LIMIT
    assert common.one_line("a\x1b[8m\nb") == "a\\x1b[8m b"


def test_manual_promote_of_a_promotable_range_says_it_would_promote_itself(
    fork: support.Fork,
) -> None:
    fork.commit({"README.md": "# docs only\n"}, "docs")
    fork.fetch()

    code, shown, _ = run_manual(fork, "no")

    assert code == common.EXIT_OK
    assert "would promote this by itself (inert-paths)" in shown
    assert "Not promoted automatically" not in shown


def test_promotion_when_the_branch_cannot_be_read_is_a_failure(
    fork: support.Fork,
) -> None:
    before = fork.approved()
    support.git(fork.paths.clone, "update-ref", "-d", common.MAIN_REF)

    result = promote.auto_promote(fork.context())
    code, _, prompts = run_manual(fork, "yes")

    assert (result.result, result.exit_code) == ("git-failed", common.EXIT_FAILED)
    assert (code, prompts) == (common.EXIT_FAILED, [])
    assert fork.approved() == before


def test_manual_promote_without_approved_revision_is_a_configuration_error(
    fork: support.Fork,
) -> None:
    fork.paths.approved_file.unlink()

    code, shown, prompts = run_manual(fork, "yes")

    assert (code, prompts) == (common.EXIT_USAGE, [])
    assert "no approved revision" in shown


def test_auto_promote_counts_a_merge_as_one_change_landed_when_it_was_merged(
    fork: support.Fork,
) -> None:
    # An upstream sync: several commits dated long ago, merged today.
    old = {**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
    support.git(fork.seed, "checkout", "--quiet", "-b", "sync")
    for index in range(3):
        (fork.seed / support.HOOK_REL).write_text(f"print({index})\n", encoding="utf-8")
        subprocess.run(
            [support.REAL_GIT, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
             "-C", str(fork.seed), "commit", "--quiet", "-am", f"old {index}"],
            env=old, check=True,
        )  # fmt: skip
    support.git(fork.seed, "checkout", "--quiet", "main")
    support.git(fork.seed, "merge", "--quiet", "--no-ff", "sync", "-m", "sync upstream")
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    fork.fetch()
    ctx = fork.context(now=time.time())

    result = promote.auto_promote(ctx)

    assert (result.result, result.exit_code) == ("awaiting-promotion", common.EXIT_OK)
    assert result.counts == {"commits": 1}
    assert promote.awaiting_summary(ctx)[2] == 1


@pytest.mark.parametrize(
    "path",
    [
        ".claude/skills/_shared/models.py",
        ".claude/skills/_shared/codex_consult.py",
        ".claude/settings.json",
        ".codex/config.toml",
        ".claude/models.local.json",
        "scripts/maint/run.py",
    ],
)
def test_relevant_paths_keeps_what_the_job_runs_when_the_updater_omits_it(
    path: str,
) -> None:
    # An approved updater that stopped shipping these: the job still executes
    # its own tools and reads its own settings from them.
    shipped = promote.shipped_paths(THIN_UPDATER)

    assert shipped is not None
    assert promote.relevant_paths([path, "README.md"], shipped) == [path]


def test_child_env_drops_everything_that_re_aims_git_or_gh() -> None:
    hostile = {
        "PATH": "/usr/bin",
        "GIT_DIR": "/elsewhere/.git",
        "GIT_WORK_TREE": "/elsewhere",
        "GIT_INDEX_FILE": "/elsewhere/index",
        "GIT_CONFIG_GLOBAL": "/hostile/gitconfig",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/hostile/hooks",
        "GIT_SSH_COMMAND": "/hostile/ssh",
        "GH_HOST": "ghe.example",
        "GH_REPO": "other/repo",
    }

    clean = common.child_env(hostile)

    assert clean == {
        "PATH": "/usr/bin",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def test_checked_result_refuses_an_outcome_the_step_never_declared() -> None:
    declared = promote.OUTCOMES

    assert common.checked_result(declared, "promote", "diverged", 2).exit_code == 2
    for result, exit_code in (("diverged", 0), ("invented", 0), ("promoted", 3)):
        with pytest.raises(ValueError, match="undeclared outcome"):
            common.checked_result(declared, "promote", result, exit_code)


README = support.MAINT_DIR / "README.md"
OUTCOME_ROW_RE = re.compile(r"^\| `([a-z0-9-]+)` \| ([0-9]( / [0-9])*) \|")


def readme_outcomes() -> dict[str, dict[str, tuple[int, ...]]]:
    """The outcome tables of the README: section -> result -> exit codes."""
    text = README.read_text(encoding="utf-8")
    section = text.split("\n## Outcomes\n", 1)[1].split("\n## ", 1)[0]
    tables: dict[str, dict[str, tuple[int, ...]]] = {}
    current = ""
    for line in section.split("\n"):
        if line.startswith("### "):
            current = line[4:].strip()
            tables[current] = {}
        row = OUTCOME_ROW_RE.match(line)
        if row is not None:
            assert row.group(1) not in tables[current], row.group(1)
            codes = tuple(int(code) for code in row.group(2).split(" / "))
            tables[current][row.group(1)] = codes
    return tables


def test_readme_outcome_tables_match_what_the_code_can_report() -> None:
    declared = {
        "promote": promote.OUTCOMES,
        "distribute": support.distribute.OUTCOMES,
        "distribute, per project": support.distribute.TARGET_STATES,
        "model-watch": model_watch.OUTCOMES,
        "report": report.OUTCOMES,
        "any step, and the launcher": maint_run.OUTCOMES,
    }

    documented = readme_outcomes()

    assert documented == {
        section: {name: tuple(sorted(codes)) for name, codes in outcomes.items()}
        for section, outcomes in declared.items()
    }


# --------------------------------------------------------------------------
# Round 3: entries that are not plain files, override file, single-use records
# --------------------------------------------------------------------------


def add_entry(fork: support.Fork, mode: str, path: str, message: str) -> str:
    """Commit one tree entry of the given mode on the fork's default branch:
    a link with a target no file system can create, or another repository."""
    target = str(fork.approved())
    if mode == "120000":
        target = (
            subprocess.run(
                [
                    support.REAL_GIT,
                    "-C",
                    str(fork.seed),
                    "hash-object",
                    "-w",
                    "--stdin",
                ],
                input=b"x" * 3000,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
    support.git(
        fork.seed, "update-index", "--add", "--cacheinfo", f"{mode},{target},{path}"
    )
    support.git(fork.seed, "commit", "--quiet", "-m", message)
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    return support.git(fork.seed, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("mode", "path"),
    [("120000", "docs/notes.md"), ("160000", "docs/vendored")],
    ids=["link", "gitlink"],
)
def test_auto_promote_with_an_inert_entry_that_is_not_a_plain_file_waits(
    fork: support.Fork, mode: str, path: str
) -> None:
    # Nothing the Mac runs or ships, by its path. But a tree with it may not
    # check out at all, and then the Mac would keep running an older revision
    # than the one recorded as approved.
    before = fork.approved()
    add_entry(fork, mode, path, "docs: notes")
    fork.fetch()
    ctx = fork.context(now=time.time())

    decision = promote.evaluate(ctx)
    result = promote.auto_promote(ctx)

    assert decision.relevant == ()  # inert by its path alone
    assert decision.special == (f"{path} (mode {mode})",)
    assert (result.result, result.exit_code) == ("awaiting-promotion", common.EXIT_OK)
    assert f"{path} (mode {mode})" in result.detail
    assert fork.approved() == before


def test_auto_promote_with_an_executable_inert_file_still_promotes(
    fork: support.Fork,
) -> None:
    (fork.seed / "tests").mkdir()
    (fork.seed / "tests" / "run.sh").write_text("echo ok\n", encoding="utf-8")
    support.git(fork.seed, "add", "-A")
    support.git(fork.seed, "update-index", "--chmod=+x", "tests/run.sh")
    support.git(fork.seed, "commit", "--quiet", "-m", "tests: runner")
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    head = fork.fetch()

    result = promote.auto_promote(fork.context())

    assert result.result == "promoted"
    assert fork.approved() == head


def test_auto_promote_past_a_link_that_was_added_and_removed_again_promotes(
    fork: support.Fork,
) -> None:
    add_entry(fork, "120000", "docs/notes.md", "docs: notes")
    support.git(fork.seed, "rm", "--quiet", "--cached", "docs/notes.md")
    head = fork.commit({"README.md": "# without the link\n"}, "docs: drop notes")
    fork.fetch()

    result = promote.auto_promote(fork.context())

    assert result.result == "promoted"  # the tree at the end has no such entry
    assert fork.approved() == head


def test_manual_promote_names_entries_that_are_not_plain_files_before_asking(
    fork: support.Fork,
) -> None:
    add_entry(fork, "120000", "docs/notes.md", "docs: notes")
    fork.fetch()

    code, shown, prompts = run_manual(fork, "no")

    assert code == common.EXIT_OK and prompts
    assert "NOT A PLAIN FILE" in shown
    assert "docs/notes.md (mode 120000)" in shown
    assert "would promote this by itself" not in shown


def test_auto_promote_with_a_model_override_file_waits(fork: support.Fork) -> None:
    # The approved manifest tool reads this file when it is in the tree.
    before = fork.approved()
    override = {"schema_version": 1, "codex": {"model": f"gpt-1-{support.FAMILY}"}}
    fork.commit({promote.LOCAL_OVERRIDE_REL: json.dumps(override) + "\n"}, "override")
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert promote.LOCAL_OVERRIDE_REL in result.detail
    assert fork.approved() == before


def open_own_bump(fork: support.Fork) -> str:
    """This Mac's model watch opens the bump pull request. Returns its branch."""
    fork.set_catalog(support.entry(support.CURRENT), support.entry(support.NEWER))
    assert model_watch.run(fork.context()).result == "pr-opened"
    return model_watch.BRANCH_PREFIX + support.NEWER


def recorded_slugs(fork: support.Fork) -> list[str]:
    try:
        records = json.loads(fork.paths.bump_records_file.read_text(encoding="utf-8"))
    except OSError:
        return []
    return [record["slug"] for record in records]


def change_pull(fork: support.Fork, **changes: object) -> None:
    pulls = fork.gh_state()["pulls"]
    pulls[-1].update(changes)
    fork.set_gh(pulls=pulls)


def test_bump_record_is_tied_to_its_pull_request(fork: support.Fork) -> None:
    branch = open_own_bump(fork)

    records = json.loads(fork.paths.bump_records_file.read_text(encoding="utf-8"))

    assert [(r["slug"], r["pull"]) for r in records] == [(support.NEWER, 10)]
    assert records[0]["commit"] == support.git(fork.bare, "rev-parse", branch)


def test_bump_record_is_used_once(fork: support.Fork) -> None:
    merge_own_bump(fork)
    assert recorded_slugs(fork) == [support.NEWER]

    first = promote.auto_promote(fork.context())

    assert first.result == "promoted"
    assert recorded_slugs(fork) == []  # used up by the promotion


def test_auto_promote_after_the_owner_closed_the_bump_pull_request_waits(
    fork: support.Fork,
) -> None:
    # The owner said no. Later the same two files reach the default branch
    # some other way: the record made for that pull request must not count.
    before = fork.approved()
    branch = open_own_bump(fork)
    change_pull(fork, state="CLOSED")
    fork.squash_merge(branch, by_pull_request=False)
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert (result.result, result.exit_code) == ("awaiting-promotion", common.EXIT_OK)
    assert "pull request 10 was not merged" in result.detail
    assert fork.approved() == before
    assert recorded_slugs(fork) == []  # and it is forgotten


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({}, "pull request 10 was not merged"),
        ({"state": "MERGED", "headRefOid": "a" * 40}, "was merged from another commit"),
        ({"state": "MERGED"}, "names no merge commit"),
        (
            {"state": "MERGED", "mergeCommit": {"oid": "APPROVED"}},
            "is not in this range",
        ),
        ({"state": "MERGED", "baseRefName": "release"}, "cannot ask GitHub"),
        ({"state": "MERGED", "author": {"login": "someone"}}, "cannot ask GitHub"),
    ],
    ids=[
        "still-open",
        "other-head",
        "no-merge-commit",
        "merge-outside",
        "base",
        "author",
    ],
)
def test_auto_promote_of_the_same_files_without_that_merge_waits(
    fork: support.Fork, changes: dict, reason: str
) -> None:
    # The two files on the default branch are exactly the recorded ones, but
    # they did not get there as the merge of the recorded pull request.
    before = str(fork.approved())
    branch = open_own_bump(fork)
    fork.squash_merge(branch, by_pull_request=False)
    fork.fetch()
    if changes.get("mergeCommit") == {"oid": "APPROVED"}:
        changes = {**changes, "mergeCommit": {"oid": before}}
    change_pull(fork, **changes)

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert reason in result.detail
    assert fork.approved() == before


def test_auto_promote_when_github_cannot_be_asked_about_the_bump_waits(
    fork: support.Fork,
) -> None:
    before = fork.approved()
    merge_own_bump(fork)
    fork.set_gh(fail=["pr view"])

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert "cannot ask GitHub about pull request 10" in result.detail
    assert fork.approved() == before
    assert recorded_slugs(fork) == [support.NEWER]  # kept for the next run


def test_manual_promotion_uses_up_the_bump_record_too(fork: support.Fork) -> None:
    merge_own_bump(fork)
    fork.commit({support.HOOK_REL: "print('and a hook')\n"}, "hook")
    fork.fetch()

    code, _, _ = run_manual(fork, "yes")

    assert code == common.EXIT_OK
    assert recorded_slugs(fork) == []


def test_special_entries_lists_only_added_or_changed_entries(
    fork: support.Fork,
) -> None:
    before = str(fork.approved())
    first = add_entry(fork, "120000", "docs/notes.md", "docs: notes")
    support.git(fork.seed, "rm", "--quiet", "--cached", "docs/notes.md")
    second = fork.commit({"README.md": "# drop\n"}, "drop")
    fork.fetch()
    git = fork.context().git

    assert bump.special_entries(git, before, first) == ["docs/notes.md (mode 120000)"]
    assert bump.special_entries(git, first, second) == []  # a deletion is not listed
    assert bump.special_entries(git, before, second) == []
    assert bump.special_entries(git, before, "f" * 40) is None


# --------------------------------------------------------------------------
# Residuals (issue #10)
# --------------------------------------------------------------------------

LONG_NAME = "docs/" + "a" * (bump.NAME_MAX_BYTES + 45)
LONG_PATH = "docs/" + "/".join(["b" * 200] * 6)


def path_of(total: int) -> str:
    """A path of exactly ``total`` bytes, every name well under NAME_MAX."""
    head = "docs/" + ("c" * 199 + "/") * 4
    return head + "c" * (total - len(head))


# PATH_MAX is 1024 with the terminating NUL: 1023 bytes is the longest that
# checks out, 1024 is already too long.
LONGEST_PATH = path_of(1023)
FIRST_TOO_LONG_PATH = path_of(1024)


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("docs/" + "a" * bump.NAME_MAX_BYTES, None),
        ("docs/" + "a" * (bump.NAME_MAX_BYTES + 1), "a name of 256 bytes"),
        # Counted in bytes, not characters: 128 two-byte characters.
        ("docs/" + "é" * 128, "a name of 256 bytes"),
        ("d/" * 511 + "a", None),  # exactly 1023 bytes
        ("d/" * 511 + "ab", "1024 bytes"),  # PATH_MAX counts the NUL
        ("d/" * 511 + "abc", "1025 bytes"),
    ],
    ids=[
        "name-at-limit",
        "name-over",
        "name-over-in-bytes",
        "path-at-limit",
        "path-at-path-max",
        "path-over",
    ],
)
def test_path_length_problem_counts_utf8_bytes_against_both_limits(
    path: str, problem: str | None
) -> None:
    found = bump.path_length_problem(path)

    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found


@pytest.mark.parametrize(
    "path",
    [LONG_NAME, LONG_PATH, FIRST_TOO_LONG_PATH],
    ids=["name", "path", "path-of-1024-bytes"],
)
def test_auto_promote_with_a_path_the_mac_cannot_check_out_waits(
    fork: support.Fork, path: str
) -> None:
    # Inert by its path, and still never promoted by itself: the tree could
    # not be checked out, and the launcher would stop running anything.
    before = fork.approved()
    fork.commit_entry(path, "notes\n", "docs: a very long name")
    fork.fetch()
    ctx = fork.context(now=time.time())

    decision = promote.evaluate(ctx)
    result = promote.auto_promote(ctx)

    assert decision.relevant == ()
    assert (result.result, result.exit_code) == ("awaiting-promotion", common.EXIT_OK)
    assert "too long to check out" in result.detail
    assert path[: bump.LONG_PATH_SHOWN - 3] in result.detail  # named, and cut
    assert path not in result.detail
    assert fork.approved() == before


def test_auto_promote_with_a_path_of_the_longest_length_that_works_promotes(
    fork: support.Fork,
) -> None:
    fork.commit_entry(LONGEST_PATH, "notes\n", "docs: a long, but possible, name")
    head = fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "promoted"  # inert, and the Mac can hold it
    assert fork.approved() == head


def test_auto_promote_names_a_long_path_only_in_a_safe_form(
    fork: support.Fork,
) -> None:
    path = "docs/\x1b[2J" + "c" * 300
    fork.commit_entry(path, "notes\n", "docs: hostile name")
    fork.fetch()

    decision = promote.evaluate(fork.context(now=time.time()))

    assert decision.action == "await"
    assert "too long to check out: docs/\\x1b[2Jccc" in decision.reason
    assert "\x1b" not in decision.reason
    assert all("\x1b" not in entry for entry in decision.overlong)


def test_manual_promote_lists_paths_too_long_to_check_out_before_asking(
    fork: support.Fork,
) -> None:
    fork.commit_entry(LONG_NAME, "notes\n", "docs: a very long name")
    fork.fetch()

    code, shown, prompts = run_manual(fork, "no")

    assert code == common.EXIT_OK and prompts
    assert "PATH TOO LONG TO CHECK OUT" in shown
    cut = LONG_NAME[: bump.LONG_PATH_SHOWN - 3]
    assert f"{cut}... (a name of {len(LONG_NAME) - len('docs/')} bytes" in shown
    assert "would promote this by itself" not in shown


def test_auto_promote_with_a_differently_cased_override_file_waits(
    fork: support.Fork,
) -> None:
    # macOS checks this out as the very file the approved manifest tool reads.
    before = fork.approved()
    override = {"schema_version": 1, "codex": {"model": f"gpt-1-{support.FAMILY}"}}
    fork.commit_entry(
        ".CLAUDE/Models.Local.JSON", json.dumps(override) + "\n", "override"
    )
    fork.fetch()

    result = promote.auto_promote(fork.context(now=time.time()))

    assert result.result == "awaiting-promotion"
    assert ".CLAUDE/Models.Local.JSON" in result.detail
    assert fork.approved() == before
