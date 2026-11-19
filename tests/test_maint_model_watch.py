"""Model watch: candidate selection, the resumable bump, and the whole run.

Scratch repositories, a fake ``gh`` and a fake ``codex`` only; see
``maint_support``. The manifest tool and the Codex wrapper are the
repository's real ones, pointed at scratch trees.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import maint_support as support
import pytest
from maint_support import (
    CURRENT,
    EFFORT,
    FAMILY,
    NEWER,
    OTHER_FAMILY,
    bump,
    common,
    entry,
    gh,
    maint_run,
    model_watch,
    promote,
    report,
)

DAY = 86400
BRANCH = model_watch.BRANCH_PREFIX + NEWER
EXPECTED_BUMP = sorted([support.MANIFEST_REL, support.CONFIG_REL])


@pytest.fixture
def fork(tmp_path: Path) -> support.Fork:
    return support.make_fork(tmp_path)


@pytest.fixture
def bumpable(fork: support.Fork) -> support.Fork:
    """A fork whose catalog lists a newer slug of the current family."""
    fork.set_catalog(entry(CURRENT), entry(NEWER))
    return fork


def pick(
    *entries: dict, current: str = CURRENT, effort: str = EFFORT
) -> model_watch.Selection:
    return model_watch.pick_candidate(support.catalog(*entries), current, effort)


def pull(state: str, age_days: float = 0.0, **extra: object) -> dict:
    created = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_days * DAY)
    )
    return {
        "number": 7,
        "state": state,
        "url": f"{common.FORK_WEB_URL}/pull/7",
        "createdAt": created,
        "headRefOid": "a" * support.SHA_LENGTH,
        "headRefName": BRANCH,
        "baseRefName": "main",
        "author": {"login": common.FORK_OWNER},
        "isCrossRepository": False,
        **extra,
    }


def watch(fork: support.Fork, now: float | None = None) -> common.StepResult:
    return model_watch.run(fork.context(now=now))


def assert_nothing_published(fork: support.Fork) -> None:
    assert fork.remote_branches() == ["main"]
    assert fork.gh_calls("pr", "create") == []
    assert fork.gh_calls("pr", "merge") == []


def assert_clone_left_clean(fork: support.Fork) -> None:
    assert not (fork.paths.run_dir / model_watch.WORKTREE_NAME).exists()
    assert support.git(fork.paths.clone, "status", "--porcelain") == ""
    assert support.git(fork.paths.clone, "branch", "--list", "bot/*") == ""
    assert len(support.git(fork.paths.clone, "worktree", "list").splitlines()) == 1
    leftovers = [path.name for path in fork.paths.run_dir.iterdir()]
    assert leftovers == [] or leftovers == [common.LOCK_FILE]


# --------------------------------------------------------------------------
# pick_candidate: a pure function over untrusted input
# --------------------------------------------------------------------------


def test_pick_candidate_with_newer_same_family_slug_returns_it() -> None:
    selection = pick(entry(CURRENT), entry(NEWER))

    assert (selection.result, selection.slug) == ("candidate", NEWER)
    assert selection.family_behind is None


def test_pick_candidate_with_only_the_current_slug_is_up_to_date() -> None:
    selection = pick(entry(CURRENT))

    assert (selection.result, selection.slug) == ("up-to-date", None)


def test_pick_candidate_with_lower_versions_only_never_downgrades() -> None:
    selection = pick(
        entry(f"gpt-6.9-{FAMILY}"), entry(f"gpt-7-{FAMILY}"), entry(CURRENT)
    )

    assert (selection.result, selection.slug) == ("up-to-date", None)


def test_pick_candidate_with_current_not_listed_has_no_candidate() -> None:
    selection = pick(entry(NEWER), entry(f"gpt-9-{FAMILY}"))

    assert (selection.result, selection.slug) == ("current-not-listed", None)


def test_pick_candidate_with_current_hidden_counts_as_not_listed() -> None:
    selection = pick(entry(CURRENT, visibility="hide"), entry(NEWER))

    assert selection.result == "current-not-listed"


def test_pick_candidate_with_effort_unsupported_names_the_newest_slug() -> None:
    selection = pick(entry(CURRENT), entry(NEWER, efforts=("low", "high")))

    assert (selection.result, selection.slug) == ("effort-unsupported", NEWER)


def test_pick_candidate_with_effort_unsupported_never_takes_an_older_one() -> None:
    middle = f"gpt-7.2-{FAMILY}"
    newest = f"gpt-7.3-{FAMILY}"

    selection = pick(entry(CURRENT), entry(middle), entry(newest, efforts=("low",)))

    assert (selection.result, selection.slug) == ("effort-unsupported", newest)


def test_pick_candidate_with_hidden_newer_slug_ignores_it() -> None:
    selection = pick(entry(CURRENT), entry(NEWER, visibility="hide"))

    assert selection.result == "up-to-date"


def test_pick_candidate_with_non_matching_slugs_ignores_them() -> None:
    selection = pick(
        entry(CURRENT),
        entry("gpt-reserve"),
        entry("codex-auto-review"),
        entry("gpt-9"),
        entry(f"gpt-9.9-{FAMILY}; rm -rf ~"),
        entry(f"gpt-9.9-{FAMILY}\n"),
        entry(f"--repo=other/gpt-9.9-{FAMILY}"),
        {"slug": 99, "visibility": "list"},
        {"visibility": "list"},
        "not an entry",
        None,
    )

    assert (selection.result, selection.slug) == ("up-to-date", None)


def test_pick_candidate_orders_versions_numerically() -> None:
    current = f"gpt-6.9-{FAMILY}"
    tenth = f"gpt-6.10-{FAMILY}"

    selection = pick(
        entry(f"gpt-6.2-{FAMILY}"), entry(current), entry(tenth), current=current
    )

    assert (selection.result, selection.slug) == ("candidate", tenth)
    assert pick(entry(current), entry(tenth), current=tenth).result == "up-to-date"


def test_pick_candidate_takes_the_highest_of_several_newer_slugs() -> None:
    newest = f"gpt-8-{FAMILY}"

    selection = pick(
        entry(f"gpt-7.9-{FAMILY}"), entry(newest), entry(NEWER), entry(CURRENT)
    )

    assert selection.slug == newest


def test_pick_candidate_with_newer_other_family_reports_family_behind() -> None:
    other = f"gpt-7.3-{OTHER_FAMILY}"

    selection = pick(entry(CURRENT), entry(other), entry(f"gpt-7.0-{OTHER_FAMILY}"))

    assert (selection.result, selection.slug) == ("up-to-date", None)
    assert selection.family_behind is not None
    assert selection.family_behind.text == other


def test_pick_candidate_never_switches_family_even_with_a_candidate() -> None:
    selection = pick(entry(CURRENT), entry(NEWER), entry(f"gpt-9-{OTHER_FAMILY}"))

    assert selection.slug == NEWER
    assert selection.family_behind is not None
    assert selection.family_behind.family == OTHER_FAMILY


def test_pick_candidate_with_other_family_not_ahead_reports_nothing() -> None:
    selection = pick(entry(CURRENT), entry(NEWER), entry(f"gpt-7.2-{OTHER_FAMILY}"))

    assert selection.family_behind is None


def test_pick_candidate_ignores_another_vendor_entirely() -> None:
    selection = pick(
        entry(CURRENT), entry(f"xyz-9-{FAMILY}"), entry(f"xyz-9-{OTHER_FAMILY}")
    )

    assert (selection.result, selection.family_behind) == ("up-to-date", None)


def test_pick_candidate_with_unparseable_current_slug_says_so() -> None:
    assert pick(entry("gpt-7.5"), current="gpt-7.5").result == "current-unparseable"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "text",
        {},
        {"models": "x"},
        {"models": None},
        {"models": []},
        {"models": [{"slug": "gpt-reserve", "visibility": "list"}]},
        {"models": [support.entry(CURRENT, visibility="hide")]},
    ],
)
def test_pick_candidate_with_malformed_catalog_raises(bad: object) -> None:
    with pytest.raises(model_watch.CatalogError):
        model_watch.pick_candidate(bad, CURRENT, EFFORT)


# --------------------------------------------------------------------------
# The step: nothing to do, and the notices
# --------------------------------------------------------------------------


def test_watch_with_candidate_equal_to_current_does_nothing(fork: support.Fork) -> None:
    result = watch(fork)

    assert (result.result, result.exit_code) == ("up-to-date", common.EXIT_OK)
    assert fork.gh_state()["calls"] == [
        ["api", "--hostname", "github.com", "user", "--jq", ".login"]
    ]
    assert fork.codex_calls("exec") == []
    assert_nothing_published(fork)
    assert_clone_left_clean(fork)


def test_watch_with_wrong_account_fails_closed(bumpable: support.Fork) -> None:
    bumpable.set_gh(login="someone-else")

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("auth-or-tool", common.EXIT_FAILED)
    assert len(bumpable.gh_state()["calls"]) == 1
    assert bumpable.codex_state()["calls"] == []
    assert_nothing_published(bumpable)


def test_watch_with_broken_codex_cli_fails_closed(bumpable: support.Fork) -> None:
    bumpable.set_codex(version_exit=1)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("auth-or-tool", common.EXIT_FAILED)
    assert_nothing_published(bumpable)


def test_watch_with_catalog_past_its_deadline_fails(
    bumpable: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_watch, "CATALOG_TIMEOUT_SEC", 1)
    bumpable.set_codex(catalog_sleep=30)
    started = time.monotonic()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "catalog-fetch-failed",
        common.EXIT_FAILED,
    )
    assert "deadline" in result.detail
    assert time.monotonic() - started < 20
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


@pytest.mark.parametrize(
    "broken",
    [
        {"catalog_exit": 1},
        {"catalog_text": "not json"},
        {"catalog_text": ""},
        {"catalog_text": json.dumps({"models": "none"})},
        {"catalog_text": json.dumps([])},
    ],
)
def test_watch_with_unusable_catalog_fails(
    bumpable: support.Fork, broken: dict
) -> None:
    bumpable.set_codex(**broken)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "catalog-fetch-failed",
        common.EXIT_FAILED,
    )
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


@pytest.mark.parametrize(
    "broken",
    [
        "{ not json",
        json.dumps({"codex": {"model": "has a space", "reasoning_effort": EFFORT}}),
        json.dumps(
            {"codex": {"model": f"{CURRENT}\n# title", "reasoning_effort": EFFORT}}
        ),
        json.dumps({"codex": {"model": CURRENT, "reasoning_effort": "x; rm -rf ~"}}),
        json.dumps({"codex": {"reasoning_effort": EFFORT}}),
        json.dumps(["not", "an", "object"]),
    ],
    ids=[
        "not-json",
        "model-with-space",
        "model-with-newline",
        "effort",
        "no-model",
        "list",
    ],
)
def test_watch_with_a_manifest_it_cannot_read_waits(
    bumpable: support.Fork, broken: str
) -> None:
    # The values come from a file on the fork and end up in titles and logs:
    # anything but the plain shape is not read at all.
    bumpable.commit({support.MANIFEST_REL: broken}, "a manifest of another shape")
    bumpable.fetch()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "manifest-unreadable",
        common.EXIT_NEEDS_OWNER,
    )
    assert bumpable.gh_state()["issues"] == []
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


def test_watch_with_newer_other_family_opens_one_notice_and_never_switches(
    fork: support.Fork,
) -> None:
    other = f"gpt-7.3-{OTHER_FAMILY}"
    fork.set_catalog(entry(CURRENT), entry(other))

    first = watch(fork)
    second = watch(fork)

    assert (first.result, first.exit_code) == ("up-to-date", common.EXIT_OK)
    assert (second.result, second.exit_code) == ("up-to-date", common.EXIT_OK)
    issues = fork.gh_state()["issues"]
    assert len(issues) == 1
    assert issues[0]["labels"] == [common.LABEL_NEEDS_OWNER]
    assert f"<!-- family-decision:{OTHER_FAMILY}:7.3 -->" in issues[0]["body"]
    assert common.LABEL_NEEDS_OWNER in fork.gh_state()["labels"]
    assert_nothing_published(fork)
    support.assert_fork_only(fork)


def test_watch_with_family_notice_closed_by_the_owner_does_not_ask_again(
    fork: support.Fork,
) -> None:
    fork.set_catalog(entry(CURRENT), entry(f"gpt-7.3-{OTHER_FAMILY}"))
    watch(fork)
    answered = [{**issue, "state": "CLOSED"} for issue in fork.gh_state()["issues"]]
    fork.set_gh(issues=answered)

    result = watch(fork)

    assert result.exit_code == common.EXIT_OK
    assert len(fork.gh_state()["issues"]) == 1


def test_watch_with_current_not_listed_asks_once_then_respects_the_answer(
    fork: support.Fork,
) -> None:
    fork.set_catalog(entry(NEWER))

    first = watch(fork)
    second = watch(fork)

    assert (first.result, first.exit_code) == (
        "current-not-listed",
        common.EXIT_NEEDS_OWNER,
    )
    assert second.exit_code == common.EXIT_NEEDS_OWNER
    issues = fork.gh_state()["issues"]
    assert len(issues) == 1
    assert f"<!-- model-not-listed:{CURRENT} -->" in issues[0]["body"]
    assert_nothing_published(fork)

    fork.set_gh(issues=[{**issues[0], "state": "CLOSED"}])
    third = watch(fork)

    assert (third.result, third.exit_code) == ("current-not-listed", common.EXIT_OK)
    assert len(fork.gh_state()["issues"]) == 1


def test_watch_with_effort_unsupported_asks_once_and_does_not_bump(
    fork: support.Fork,
) -> None:
    fork.set_catalog(entry(CURRENT), entry(NEWER, efforts=("low",)))

    first = watch(fork)
    watch(fork)

    assert (first.result, first.exit_code) == (
        "effort-unsupported",
        common.EXIT_NEEDS_OWNER,
    )
    issues = fork.gh_state()["issues"]
    assert len(issues) == 1
    assert f"<!-- effort-unsupported:{NEWER}:{EFFORT} -->" in issues[0]["body"]
    assert_nothing_published(fork)


def test_watch_when_the_notice_cannot_be_filed_fails(fork: support.Fork) -> None:
    fork.set_catalog(entry(NEWER))
    fork.set_gh(fail=["issue create"])

    result = watch(fork)

    assert (result.result, result.exit_code) == ("notice-failed", common.EXIT_FAILED)


# --------------------------------------------------------------------------
# The step: the bump, and every row of the resume table
# --------------------------------------------------------------------------


def test_watch_with_newer_slug_opens_one_auto_merging_pull_request(
    bumpable: support.Fork,
) -> None:
    main_before = support.git(bumpable.bare, "rev-parse", "main")

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("pr-opened", common.EXIT_OK)
    # The branch on the fork: one commit, exactly the two rendered files.
    assert bumpable.remote_branches() == [BRANCH, "main"]
    assert bumpable.changed_on(BRANCH) == EXPECTED_BUMP
    assert support.git(bumpable.bare, "rev-parse", "main") == main_before
    head = support.git(bumpable.bare, "rev-parse", BRANCH)
    assert support.git(bumpable.bare, "rev-parse", f"{BRANCH}~1") == main_before
    manifest = json.loads(
        support.git(bumpable.bare, "show", f"{BRANCH}:{support.MANIFEST_REL}")
    )
    assert manifest["codex"]["model"] == NEWER
    assert manifest["superseded"]["codex.model"][-1] == CURRENT
    assert f'model = "{NEWER}"' in support.git(
        bumpable.bare, "show", f"{BRANCH}:{support.CONFIG_REL}"
    )
    # The pull request and its auto-merge, both on the fork.
    creates = bumpable.gh_calls("pr", "create")
    assert len(creates) == 1
    for flag, value in (
        ("--repo", common.FORK_REPO),
        ("--base", "main"),
        ("--head", BRANCH),
    ):
        assert creates[0][creates[0].index(flag) + 1] == value
    merges = bumpable.gh_state()["merges"]
    assert merges == [
        ["pr", "merge", "--repo", common.FORK_REPO, "10", "--auto", "--squash",
         "--match-head-commit", head]
    ]  # fmt: skip
    # The live call named the slug and the effort, read-only.
    smoke = bumpable.codex_calls("exec")
    assert len(smoke) == 1
    assert smoke[0][smoke[0].index("--model") + 1] == NEWER
    assert smoke[0][smoke[0].index("--sandbox") + 1] == "read-only"
    assert f"model_reasoning_effort={EFFORT}" in smoke[0]
    assert_clone_left_clean(bumpable)
    support.assert_fork_only(bumpable)


def test_watch_rerun_after_opening_does_not_open_a_second_pull_request(
    bumpable: support.Fork,
) -> None:
    watch(bumpable)
    head = support.git(bumpable.bare, "rev-parse", BRANCH)

    again = watch(bumpable)

    assert (again.result, again.exit_code) == ("pending", common.EXIT_OK)
    assert len(bumpable.gh_calls("pr", "create")) == 1
    assert len(bumpable.gh_state()["pulls"]) == 1
    assert len(bumpable.codex_calls("exec")) == 1
    assert support.git(bumpable.bare, "rev-parse", BRANCH) == head
    assert len(bumpable.gh_state()["merges"]) == 2  # auto-merge re-asserted
    assert_clone_left_clean(bumpable)


def test_watch_with_merged_pull_request_is_done(bumpable: support.Fork) -> None:
    bumpable.set_gh(pulls=[pull("CLOSED"), pull("MERGED")])

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("already-merged", common.EXIT_OK)
    assert bumpable.codex_calls("exec") == []
    assert_nothing_published(bumpable)


def open_bump(fork: support.Fork, age_days: float = 0.0) -> str:
    """Let one run open the pull request; return the branch head. The pull
    request can be made to look older than it is."""
    assert watch(fork).result == "pr-opened"
    created = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_days * DAY)
    )
    fork.set_gh(pulls=[{**fork.gh_state()["pulls"][0], "createdAt": created}])
    return support.git(fork.bare, "rev-parse", BRANCH)


def test_watch_with_open_pull_request_keeps_auto_merge_on(
    bumpable: support.Fork,
) -> None:
    head = open_bump(bumpable, age_days=1)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("pending", common.EXIT_OK)
    assert len(bumpable.gh_calls("pr", "create")) == 1
    merges = bumpable.gh_state()["merges"]
    assert len(merges) == 2
    assert merges[1][:5] == ["pr", "merge", "--repo", common.FORK_REPO, "10"]
    assert merges[1][-1] == head
    assert len(bumpable.codex_calls("exec")) == 1


def test_watch_with_open_pull_request_older_than_two_days_needs_the_owner(
    bumpable: support.Fork,
) -> None:
    open_bump(bumpable, age_days=3)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("pending", common.EXIT_NEEDS_OWNER)
    assert len(bumpable.gh_calls("pr", "create")) == 1


def test_watch_with_open_pull_request_whose_branch_was_pushed_to_leaves_it_alone(
    bumpable: support.Fork,
) -> None:
    # Someone pushes more into the open pull request's branch. GitHub reports
    # the new head; auto-merge must not be asked for on it.
    open_bump(bumpable)
    support.git(bumpable.seed, "fetch", "--quiet", "origin", BRANCH)
    support.git(bumpable.seed, "checkout", "--quiet", "-B", "hijack", "FETCH_HEAD")
    (bumpable.seed / "scripts" / "extra.sh").write_text(
        "echo extra\n", encoding="utf-8"
    )
    support.git(bumpable.seed, "add", "-A")
    support.git(bumpable.seed, "commit", "--quiet", "-m", "something else")
    support.git(bumpable.seed, "push", "--quiet", "origin", f"hijack:{BRANCH}")
    moved = support.git(bumpable.bare, "rev-parse", BRANCH)
    bumpable.set_gh(pulls=[{**bumpable.gh_state()["pulls"][0], "headRefOid": moved}])

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "unknown-branch",
        common.EXIT_NEEDS_OWNER,
    )
    assert "open pull request" in result.detail
    assert "auto-merge withdrawn" in result.detail
    assert len(bumpable.gh_state()["merges"]) == 1  # only the original request
    assert [call[4:] for call in bumpable.gh_state()["disabled"]] == [
        ["10", "--disable-auto"]
    ]
    assert support.git(bumpable.bare, "rev-parse", BRANCH) == moved  # never rewritten
    assert_clone_left_clean(bumpable)


def test_watch_with_open_pull_request_and_no_branch_withdraws_auto_merge(
    bumpable: support.Fork,
) -> None:
    bumpable.set_gh(pulls=[pull("OPEN", age_days=1)])

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "unknown-branch",
        common.EXIT_NEEDS_OWNER,
    )
    assert "auto-merge withdrawn" in result.detail
    assert bumpable.gh_calls("pr", "create") == []
    assert bumpable.gh_state()["merges"] == []
    assert [call[4:] for call in bumpable.gh_state()["disabled"]] == [
        ["7", "--disable-auto"]
    ]
    assert bumpable.remote_branches() == ["main"]


def test_watch_with_pull_request_closed_unmerged_is_never_recreated(
    bumpable: support.Fork,
) -> None:
    bumpable.set_gh(pulls=[pull("CLOSED")])

    first = watch(bumpable)
    second = watch(bumpable)

    assert (first.result, first.exit_code) == ("rejected-by-owner", common.EXIT_OK)
    assert second.result == "rejected-by-owner"
    assert bumpable.codex_calls("exec") == []
    assert_nothing_published(bumpable)
    # `gh pr list` shows open pull requests only unless told otherwise; the
    # closed one is found because every state, this base and this author are
    # asked for by name.
    listing = bumpable.gh_calls("pr", "list")[0]
    for flag, value in (
        ("--head", BRANCH),
        ("--base", "main"),
        ("--author", common.FORK_OWNER),
        ("--state", "all"),
    ):
        assert listing[listing.index(flag) + 1] == value


@pytest.mark.parametrize(
    "other",
    [{"baseRefName": "release"}, {"author": {"login": "someone-else"}}],
    ids=["another-base", "another-author"],
)
def test_watch_ignores_a_pull_request_that_is_not_the_owners_into_main(
    bumpable: support.Fork, other: dict
) -> None:
    # Neither may stand in for "the bump was merged" or "the owner rejected it".
    bumpable.set_gh(pulls=[pull("MERGED", **other), pull("CLOSED", **other)])

    result = watch(bumpable)

    assert result.result == "pr-opened"


def test_watch_ignores_a_pull_request_from_another_repository(
    bumpable: support.Fork,
) -> None:
    bumpable.set_gh(pulls=[pull("CLOSED", isCrossRepository=True)])

    result = watch(bumpable)

    assert result.result == "pr-opened"


def test_watch_with_pushed_branch_and_no_pull_request_resumes_at_the_pull_request(
    bumpable: support.Fork,
) -> None:
    # A run that stops between the push and the pull request.
    bumpable.set_gh(fail=["pr create"])
    crashed = watch(bumpable)
    head = support.git(bumpable.bare, "rev-parse", BRANCH)
    bumpable.set_gh(fail=[])

    resumed = watch(bumpable)

    assert (crashed.result, crashed.exit_code) == (
        "pr-create-failed",
        common.EXIT_FAILED,
    )
    assert (resumed.result, resumed.exit_code) == ("pr-opened", common.EXIT_OK)
    # The push is not repeated. The live call is: nothing proves the branch
    # found on the fork came from a run that made it.
    assert support.git(bumpable.bare, "rev-parse", BRANCH) == head
    assert len(bumpable.codex_calls("exec")) == 2
    assert len(bumpable.gh_state()["pulls"]) == 1
    assert bumpable.gh_state()["merges"][-1][-1] == head
    assert_clone_left_clean(bumpable)


def test_watch_with_pushed_branch_and_failing_live_call_opens_no_pull_request(
    bumpable: support.Fork,
) -> None:
    # A branch of exactly the right shape that no run of this job vouched for.
    support.git(bumpable.seed, "checkout", "--quiet", "-b", BRANCH)
    for args in (["set", "codex.model", NEWER], ["apply"]):
        support.run_models(bumpable.seed, args)
    support.git(bumpable.seed, "commit", "--quiet", "-am", "pushed by someone else")
    support.git(bumpable.seed, "push", "--quiet", "origin", BRANCH)
    bumpable.set_codex(smoke_reply="I cannot do that\n")

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("smoke-failed", common.EXIT_FAILED)
    assert bumpable.gh_calls("pr", "create") == []
    assert bumpable.gh_calls("pr", "merge") == []


def test_watch_with_unknown_branch_of_that_name_needs_the_owner(
    bumpable: support.Fork,
) -> None:
    support.git(bumpable.seed, "checkout", "--quiet", "-b", BRANCH)
    (bumpable.seed / "README.md").write_text(
        "# someone else's branch\n", encoding="utf-8"
    )
    support.git(bumpable.seed, "commit", "--quiet", "-am", "unrelated")
    support.git(bumpable.seed, "push", "--quiet", "origin", BRANCH)
    head = support.git(bumpable.bare, "rev-parse", BRANCH)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "unknown-branch",
        common.EXIT_NEEDS_OWNER,
    )
    assert support.git(bumpable.bare, "rev-parse", BRANCH) == head  # never overwritten
    assert bumpable.gh_calls("pr", "create") == []
    assert bumpable.codex_calls("exec") == []
    assert_clone_left_clean(bumpable)


def test_watch_with_branch_bumping_to_another_slug_needs_the_owner(
    bumpable: support.Fork,
) -> None:
    support.git(bumpable.seed, "checkout", "--quiet", "-b", BRANCH)
    for args in (["set", "codex.model", f"gpt-7.9-{FAMILY}"], ["apply"]):
        support.run_models(bumpable.seed, args)
    support.git(bumpable.seed, "commit", "--quiet", "-am", "a bump, but not this one")
    support.git(bumpable.seed, "push", "--quiet", "origin", BRANCH)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "unknown-branch",
        common.EXIT_NEEDS_OWNER,
    )
    assert bumpable.gh_calls("pr", "create") == []


def test_watch_with_unexpected_third_changed_file_pushes_nothing(
    bumpable: support.Fork,
) -> None:
    # The default branch has drifted: an agent file no longer matches the
    # manifest, so rendering the bump also rewrites that file.
    agent = sorted((bumpable.seed / ".claude" / "agents").glob("*.md"))[0]
    lines = agent.read_text(encoding="utf-8").splitlines(keepends=True)
    drifted = [
        "model: inherit\n" if line.startswith("model:") else line for line in lines
    ]
    assert drifted != lines
    bumpable.commit({str(agent.relative_to(bumpable.seed)): "".join(drifted)}, "drift")
    bumpable.fetch()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("unexpected-diff", common.EXIT_FAILED)
    assert agent.name in result.detail
    assert bumpable.codex_calls("exec") == []  # refused before the live call
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


@pytest.mark.parametrize(
    "reply",
    [
        {"smoke_reply": "Sure! OK\n"},
        {"smoke_reply": "ok\n"},
        {"smoke_reply": ""},
        {"smoke_reply": "OK\n", "exec_exit": 1},
    ],
)
def test_watch_with_failed_smoke_call_pushes_nothing(
    bumpable: support.Fork, reply: dict
) -> None:
    bumpable.set_codex(**reply)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("smoke-failed", common.EXIT_FAILED)
    assert len(bumpable.codex_calls("exec")) == 1
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


def test_watch_with_pull_request_url_outside_the_fork_stops(
    bumpable: support.Fork,
) -> None:
    bumpable.set_gh(
        pr_url_base="https://github.com/DeL-TaiseiOzaki/claude-code-orchestra"
    )

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "pr-url-outside-fork",
        common.EXIT_FAILED,
    )
    assert bumpable.gh_calls("pr", "merge") == []
    support.assert_fork_only(bumpable)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/ToRainu256/claude-code-orchestra-evil/pull/3",
        "https://github.com/ToRainu256/claude-code-orchestra/pull/3/files",
        "https://github.com.evil.example/ToRainu256/claude-code-orchestra/pull/3",
        "http://github.com/ToRainu256/claude-code-orchestra/pull/3",
        "https://github.com/ToRainu256/claude-code-orchestra/pull/0",
        "https://github.com/ToRainu256/claude-code-orchestra/issues/3",
        "",
        None,
    ],
)
def test_pull_number_accepts_only_a_pull_request_of_the_fork(url: object) -> None:
    assert model_watch._pull_number(url) is None
    assert model_watch._pull_number(f"{common.FORK_WEB_URL}/pull/42\n") == 42


def test_watch_when_auto_merge_cannot_be_enabled_retries_on_the_next_run(
    bumpable: support.Fork,
) -> None:
    bumpable.set_gh(fail=["pr merge"])
    first = watch(bumpable)
    bumpable.set_gh(fail=[])

    second = watch(bumpable)

    assert (first.result, first.exit_code) == (
        "auto-merge-not-enabled",
        common.EXIT_NEEDS_OWNER,
    )
    assert (second.result, second.exit_code) == ("pending", common.EXIT_OK)
    assert len(bumpable.gh_state()["pulls"]) == 1
    assert len(bumpable.gh_state()["merges"]) == 1


def test_watch_when_the_push_is_rejected_opens_no_pull_request(
    bumpable: support.Fork,
) -> None:
    hook = bumpable.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("push-failed", common.EXIT_FAILED)
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


def test_watch_never_touches_the_developer_checkout(bumpable: support.Fork) -> None:
    def snapshot() -> str:
        done = subprocess.run(
            ["git", "-C", str(support.REPO_ROOT), "status", "--porcelain", "--branch"],
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    before = snapshot()

    watch(bumpable)

    assert snapshot() == before
    assert not (
        support.REPO_ROOT / ".claude" / "logs" / "codex" / "prompt-model-smoke.md"
    ).exists()


# --------------------------------------------------------------------------
# The whole run, through the installed launcher, in a temp state root
# --------------------------------------------------------------------------


@pytest.fixture
def installed(tmp_path: Path) -> support.Fork:
    """A fork that carries the maintenance code, a clone approved at it, and
    the launcher copied where `launchd.sh install` would put it."""
    fork = support.make_fork(tmp_path, with_code=True)
    fork.paths.launcher.parent.mkdir(parents=True)
    fork.paths.launcher.write_bytes(
        (support.MAINT_DIR / "orchestra-maint").read_bytes()
    )
    return fork


def launch(fork: support.Fork, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(fork.paths.launcher), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**fork.env, "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0"},
        stdin=subprocess.DEVNULL,
        cwd=str(fork.root),
    )


def last_run(fork: support.Fork) -> dict:
    return json.loads(fork.paths.last_run_file.read_text(encoding="utf-8"))


def step_results(fork: support.Fork) -> dict[str, str]:
    return {step["name"]: step["result"] for step in last_run(fork)["steps"]}


def test_run_end_to_end_opens_one_pull_request_and_a_rerun_opens_none(
    installed: support.Fork,
) -> None:
    installed.set_catalog(entry(CURRENT), entry(NEWER))
    approved = installed.approved()

    first = launch(installed, "run")

    assert first.returncode == common.EXIT_OK, first.stderr
    assert step_results(installed) == {
        "promote": "up-to-date",
        "model-watch": "pr-opened",
        "report": "no-status-issue",
    }
    assert installed.remote_branches() == [BRANCH, "main"]
    assert installed.changed_on(BRANCH) == EXPECTED_BUMP
    assert len(installed.gh_calls("pr", "create")) == 1
    assert len(installed.gh_state()["merges"]) == 1
    # Nothing else changed: the clone sits clean at the approved revision.
    assert installed.approved() == approved
    assert support.git(installed.paths.clone, "rev-parse", "HEAD") == approved
    assert support.git(installed.paths.clone, "status", "--porcelain") == ""
    assert_clone_left_clean(installed)
    assert len(list(installed.paths.logs_dir.glob("*.log"))) == 1

    second = launch(installed, "run")

    assert second.returncode == common.EXIT_OK, second.stderr
    assert step_results(installed)["model-watch"] == "pending"
    assert len(installed.gh_calls("pr", "create")) == 1
    assert len(installed.gh_state()["pulls"]) == 1
    support.assert_fork_only(installed)


def test_run_promotes_an_inert_range_and_restarts_on_the_new_revision(
    installed: support.Fork,
) -> None:
    old = installed.approved()
    head = installed.commit({"README.md": "# docs only\n"}, "docs (#5)")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_OK, done.stderr
    assert installed.approved() == head != old
    assert step_results(installed)["promote"] == "promoted"
    assert last_run(installed)["approved"] == head
    assert last_run(installed)["awaiting"] == 0
    # The restart went through the launcher, which checked the new revision out.
    assert support.git(installed.paths.clone, "rev-parse", "HEAD") == head


def test_run_leaves_a_shipped_change_unpromoted_and_keeps_running_approved_code(
    installed: support.Fork,
) -> None:
    old = installed.approved()
    # Unapproved code on the default branch: it must never run here.
    marker = installed.root / "executed-unapproved"
    installed.commit(
        {"scripts/maint/run.py": f"open({str(marker)!r}, 'w').close()\n"},
        "replace the dispatcher",
    )

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_OK, done.stderr
    assert not marker.exists()
    assert installed.approved() == old
    assert step_results(installed)["promote"] == "awaiting-promotion"
    assert last_run(installed)["awaiting"] == 1
    assert support.git(installed.paths.clone, "rev-parse", "HEAD") == old


def test_run_posts_one_status_comment_and_replaces_it_on_the_next_run(
    installed: support.Fork,
) -> None:
    installed.set_gh(
        issues=[
            {"number": 3, "state": "OPEN", "body": "", "labels": [common.LABEL_STATUS]}
        ]
    )

    launch(installed, "run")
    launch(installed, "run")

    comments = installed.gh_state()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"].startswith("<!-- job:local-maint -->\n")
    assert "| model-watch | up-to-date | 0 |" in comments[0]["body"]
    assert str(installed.root) not in comments[0]["body"]
    assert step_results(installed)["report"] == "posted"
    support.assert_fork_only(installed)


def test_run_while_another_run_holds_the_lock_does_nothing(
    installed: support.Fork,
) -> None:
    held = common.acquire_lock(installed.paths)
    assert held is not None

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_OK
    assert "holds the lock" in done.stdout
    assert not installed.paths.last_run_file.exists()
    assert installed.gh_state()["calls"] == []


def test_run_with_one_failing_step_still_runs_the_others(
    installed: support.Fork,
) -> None:
    installed.set_catalog(entry(CURRENT), entry(NEWER))
    installed.set_gh(login="someone-else")
    head = installed.commit({"README.md": "# docs only\n"}, "docs")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_FAILED
    assert installed.approved() == head  # promotion still happened
    assert step_results(installed)["model-watch"] == "auth-or-tool"
    assert last_run(installed)["exit_code"] == common.EXIT_FAILED


def test_launcher_refuses_a_clone_path_that_is_a_link(installed: support.Fork) -> None:
    # Another checkout of the same repository, with local work in it: the
    # forced checkout and the clean must never reach it through a link.
    real = installed.root / "developer-checkout"
    installed.paths.clone.rename(real)
    installed.paths.clone.symlink_to(real, target_is_directory=True)
    (real / "uncommitted.txt").write_text("local work\n", encoding="utf-8")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_USAGE
    assert "is not a directory" in done.stderr
    assert (real / "uncommitted.txt").read_text(encoding="utf-8") == "local work\n"
    assert installed.gh_state()["calls"] == []
    assert not installed.paths.logs_dir.exists()


def test_launcher_without_a_clone_creates_nothing(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    done = subprocess.run(
        ["/bin/bash", str(support.MAINT_DIR / "orchestra-maint"), "run"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            common.STATE_ROOT_ENV: str(state_root),
        },
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == common.EXIT_USAGE
    assert not state_root.exists()
    assert not (tmp_path / "home").exists()


def test_launcher_refuses_a_clone_of_another_repository(
    installed: support.Fork,
) -> None:
    support.git(
        installed.paths.clone,
        "remote",
        "set-url",
        "origin",
        "https://github.com/DeL-TaiseiOzaki/claude-code-orchestra.git",
    )
    refs = support.git(installed.paths.clone, "for-each-ref")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_USAGE
    assert "is not a clone of" in done.stderr
    assert support.git(installed.paths.clone, "for-each-ref") == refs
    assert list(installed.paths.clone.iterdir()) == [installed.paths.clone / ".git"]
    assert installed.gh_state()["calls"] == []


@pytest.mark.parametrize("content", [None, "", "main\n", "abc123\n"])
def test_launcher_without_a_valid_approved_revision_runs_nothing(
    installed: support.Fork, content: str | None
) -> None:
    installed.paths.approved_file.unlink()
    if content is not None:
        installed.paths.approved_file.write_text(content, encoding="utf-8")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_USAGE
    assert "no approved revision" in done.stderr
    assert installed.gh_state()["calls"] == []


def test_launcher_when_the_fork_cannot_be_fetched_gives_up(
    installed: support.Fork,
) -> None:
    installed.bare.rename(installed.root / "gone.git")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_FAILED
    assert "cannot fetch" in done.stderr
    assert "Next:" in done.stderr
    assert "cannot fetch the fork" in installed.paths.launcher_failure_file.read_text(
        encoding="utf-8"
    )
    assert installed.gh_state()["calls"] == []


def test_status_through_the_approved_checkout_prints_the_last_run(
    installed: support.Fork,
) -> None:
    launch(installed, "run")

    done = launch(installed, "status")

    assert done.returncode == common.EXIT_OK, done.stderr
    assert f"approved  {installed.approved()}" in done.stdout
    assert "awaiting promotion: 0" in done.stdout
    assert "model-watch: up-to-date (exit 0)" in done.stdout


def test_promote_without_a_terminal_is_refused(installed: support.Fork) -> None:
    old = installed.approved()
    installed.commit({support.HOOK_REL: "print('changed')\n"}, "hook")

    done = launch(installed, "promote")

    assert done.returncode == common.EXIT_USAGE
    assert "needs a terminal" in done.stdout
    assert installed.approved() == old


# --------------------------------------------------------------------------
# Review round
# --------------------------------------------------------------------------


def fake_tools(tmp_path: Path, consult_source: str) -> Path:
    """An "approved checkout" whose Codex wrapper is a stand-in script."""
    root = tmp_path / "tools"
    shared = root / support.SHARED_REL
    shared.mkdir(parents=True)
    shutil.copy2(support.REPO_ROOT / support.SHARED_REL / "models.py", shared)
    (shared / "codex_consult.py").write_text(consult_source, encoding="utf-8")
    return root


def test_watch_with_catalog_that_lists_no_usable_slug_is_not_a_retired_model(
    bumpable: support.Fork,
) -> None:
    # A catalog whose format changed looks exactly like one without the model.
    bumpable.set_codex(catalog_text=json.dumps({"models": []}))

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "catalog-fetch-failed",
        common.EXIT_FAILED,
    )
    assert bumpable.gh_state()["issues"] == []


def test_watch_with_current_slug_of_no_known_shape_needs_the_owner(
    fork: support.Fork,
) -> None:
    fork.bump("gpt-7.5")
    fork.fetch()

    result = watch(fork)

    assert (result.result, result.exit_code) == (
        "current-unparseable",
        common.EXIT_NEEDS_OWNER,
    )
    assert_nothing_published(fork)


def test_watch_when_the_family_notice_cannot_be_filed_keeps_its_outcome(
    bumpable: support.Fork,
) -> None:
    bumpable.set_catalog(entry(CURRENT), entry(NEWER), entry(f"gpt-9-{OTHER_FAMILY}"))
    bumpable.set_gh(fail=["issue create"])

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("pr-opened", common.EXIT_OK)
    assert result.counts == {"notice-failed": 1}
    assert len(bumpable.gh_state()["pulls"]) == 1


def test_watch_when_rendering_changes_a_second_line_pushes_nothing(
    bumpable: support.Fork,
) -> None:
    # The default branch drifted inside one of the two files: its effort is a
    # value the manifest superseded, so rendering the bump rewrites that line
    # too. Still only the two files, but no longer only the model.
    manifest = json.loads(
        (bumpable.seed / support.MANIFEST_REL).read_text(encoding="utf-8")
    )
    older_effort = manifest["superseded"]["codex.reasoning_effort"][0]
    config = (bumpable.seed / support.CONFIG_REL).read_text(encoding="utf-8")
    drifted = config.replace(
        f'model_reasoning_effort = "{EFFORT}"',
        f'model_reasoning_effort = "{older_effort}"',
    )
    assert drifted != config
    bumpable.commit({support.CONFIG_REL: drifted}, "drift inside the config")
    bumpable.fetch()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("unexpected-diff", common.EXIT_FAILED)
    assert "changes 2 lines" in result.detail
    assert bumpable.codex_calls("exec") == []  # refused before the live call
    assert not bumpable.paths.bump_records_file.exists()
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


def test_watch_when_the_manifest_tool_refuses_the_tree_pushes_nothing(
    bumpable: support.Fork,
) -> None:
    # A layout the approved tool will not edit: the key it manages, twice.
    config = (bumpable.seed / support.CONFIG_REL).read_text(encoding="utf-8")
    bumpable.commit(
        {support.CONFIG_REL: f'model = "{CURRENT}"\n{config}'}, "duplicate the key"
    )
    bumpable.fetch()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("apply-failed", common.EXIT_FAILED)
    assert bumpable.codex_calls("exec") == []
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


def test_watch_with_the_manifest_tool_past_its_deadline_fails(
    bumpable: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_watch, "PREPARE_TIMEOUT_SEC", 0.001)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("apply-failed", common.EXIT_FAILED)
    assert "deadline" in result.detail
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


def test_watch_with_git_past_its_deadline_fails(
    bumpable: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, "GIT_TIMEOUT_SEC", 3)
    bumpable.stall_git("ls-remote")
    started = time.monotonic()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("git-failed", common.EXIT_FAILED)
    assert "deadline" in result.detail
    assert time.monotonic() - started < 30
    assert_nothing_published(bumpable)


def test_watch_with_gh_past_its_deadline_fails(
    bumpable: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh, "GH_TIMEOUT_SEC", 3)
    bumpable.set_gh(stall=["pr list"])
    started = time.monotonic()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("github-failed", common.EXIT_FAILED)
    assert time.monotonic() - started < 30
    assert_nothing_published(bumpable)


def test_watch_with_the_live_call_past_its_deadline_pushes_nothing(
    bumpable: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_watch, "SMOKE_DEADLINE_SEC", 3)
    bumpable.set_codex(exec_sleep=60)
    started = time.monotonic()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("smoke-failed", common.EXIT_FAILED)
    assert "deadline" in result.detail
    assert time.monotonic() - started < 40
    assert_nothing_published(bumpable)


def test_smoke_problem_does_not_take_the_wrappers_word_alone(
    fork: support.Fork, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("OK\n", encoding="utf-8")
    not_ok = fake_tools(
        tmp_path / "a",
        "import json, pathlib\n"
        "pathlib.Path('answer.md').write_text('OK')\n"
        "print(json.dumps({'ok': False, 'response_file': 'answer.md'}))\n",
    )
    elsewhere = fake_tools(
        tmp_path / "b",
        f"import json\nprint(json.dumps({{'ok': True, 'response_file': {str(outside)!r}}}))\n",
    )
    climbing = fake_tools(
        tmp_path / "c",
        "import json\n"
        f"print(json.dumps({{'ok': True, 'response_file': '../../../../{outside.name}'}}))\n",
    )

    assert "did not report ok" in str(
        model_watch.smoke_problem(fork.context(tools_root=not_ok), NEWER, EFFORT)
    )
    for tools in (elsewhere, climbing):
        problem = model_watch.smoke_problem(
            fork.context(tools_root=tools), NEWER, EFFORT
        )
        assert "no response file of its own" in str(problem)
    assert [path.name for path in fork.paths.run_dir.iterdir()] == []


def test_watch_fetches_the_catalog_from_a_scratch_directory(fork: support.Fork) -> None:
    watch(fork)

    calls = fork.codex_state()["calls"]
    where = Path(fork.codex_state()["cwds"][calls.index(["debug", "models"])])
    assert where.parent.resolve() == fork.paths.run_dir.resolve()
    assert not where.exists()


def test_watch_when_the_fork_cannot_be_reached_is_a_failure_not_a_verdict(
    bumpable: support.Fork,
) -> None:
    # A network failure says nothing about the branch: it must not be reported
    # as a branch a person has to look at, and auto-merge is left as it is.
    open_bump(bumpable)
    bumpable.bare.rename(bumpable.root / "unreachable.git")

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("git-failed", common.EXIT_FAILED)
    assert bumpable.gh_state()["disabled"] == []
    assert len(bumpable.gh_state()["merges"]) == 1


def test_watch_records_the_bump_it_vouched_for(bumpable: support.Fork) -> None:
    watch(bumpable)

    records = json.loads(bumpable.paths.bump_records_file.read_text(encoding="utf-8"))
    head = support.git(bumpable.bare, "rev-parse", BRANCH)
    assert [(record["slug"], record["commit"]) for record in records] == [(NEWER, head)]
    assert records[0]["pull"] == 10  # tied to the pull request it opened
    for rel in (support.MANIFEST_REL, support.CONFIG_REL):
        assert records[0]["blobs"][rel] == support.git(
            bumpable.bare, "rev-parse", f"{BRANCH}:{rel}"
        )
    assert oct(bumpable.paths.bump_records_file.stat().st_mode & 0o777) == "0o600"


def test_watch_checks_the_bump_against_the_branch_again_right_before_the_push(
    bumpable: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default branch moves while the live call is running.
    moved = bumpable.commit({"README.md": "# moved meanwhile\n"}, "docs")

    def smoke_while_main_moves(ctx: object, slug: str, effort: str) -> None:
        bumpable.fetch()

    monkeypatch.setattr(model_watch, "smoke_problem", smoke_while_main_moves)
    support.git(bumpable.bare, "update-ref", "refs/heads/main", f"{moved}~1")
    bumpable.fetch()
    support.git(bumpable.bare, "update-ref", "refs/heads/main", moved)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("unexpected-diff", common.EXIT_FAILED)
    assert "the default branch moved" in result.detail
    assert not bumpable.paths.bump_records_file.exists()
    assert_nothing_published(bumpable)


def test_watch_builds_the_bump_on_the_branch_not_on_a_tag_that_shadows_it(
    bumpable: support.Fork, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tag is inside the clone and the guard that would refuse it is
    # switched off: the bump must still start from the real default branch.
    side = support.never_merged_commit(
        bumpable, {".claude/hooks/backdoor.py": "print('never reviewed')\n"}
    )
    support.plant_shadow_tag(bumpable, side, in_clone=True)
    real = bumpable.fetch()
    monkeypatch.setattr(common, "require_owned_clone", lambda git: None)

    result = watch(bumpable)

    assert result.result == "pr-opened"
    assert support.git(bumpable.bare, "rev-parse", f"{BRANCH}~1") == real != side
    assert bumpable.changed_on(BRANCH) == EXPECTED_BUMP


def test_watch_with_a_tag_inside_the_clone_refuses_to_run(
    bumpable: support.Fork,
) -> None:
    side = support.never_merged_commit(bumpable, {"README.md": "# never merged\n"})
    support.plant_shadow_tag(bumpable, side, in_clone=True)
    ctx = bumpable.context()

    result = maint_run.run_step(ctx, model_watch.STEP, model_watch.run)

    assert (result.result, result.exit_code) == ("refused", common.EXIT_USAGE)
    assert_nothing_published(bumpable)
    assert bumpable.gh_state()["calls"] == []


def test_watch_never_fetches_a_tag_with_a_bump_branch(bumpable: support.Fork) -> None:
    # A bot branch pushed by someone else, on a commit that was never merged,
    # together with a tag named like the default branch.
    side = support.never_merged_commit(
        bumpable, {".claude/hooks/backdoor.py": "print('never reviewed')\n"}
    )
    support.git(
        bumpable.seed, "push", "--quiet", "origin", f"{side}:refs/heads/{BRANCH}"
    )
    support.plant_shadow_tag(bumpable, side, in_clone=False)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "unknown-branch",
        common.EXIT_NEEDS_OWNER,
    )
    assert support.git(bumpable.paths.clone, "tag", "-l") == ""
    assert bumpable.gh_calls("pr", "create") == []
    common.require_owned_clone(bumpable.context().git)


def hostile_git_config(fork: support.Fork) -> Path:
    """A global git configuration of the kind item-by-item tooling installs:
    a hook dispatcher that runs what the repository names, a rewrite rule for
    the fork's URL, and a filter driver. Returns the marker any of them would
    create. The same file is the home directory's configuration too."""
    home = Path(fork.env["HOME"])
    marker = fork.root / "PWNED"
    evil = fork.root / "evil.git"
    subprocess.run(
        [support.REAL_GIT, "init", "--quiet", "--bare", str(evil)], check=True
    )
    hooks = home / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "pre-commit", "pre-push", "post-commit"):
        support.write_script(
            hooks / name, f'#!/bin/sh\necho "{name}" >> "{marker}"\nexit 0\n'
        )
    config = home / ".gitconfig"
    config.write_text(
        f"[core]\n\thooksPath = {hooks}\n"
        f'[url "{evil}"]\n\tinsteadOf = {common.FORK_GIT_URL}\n'
        f'[filter "evil"]\n\tsmudge = sh -c \'echo smudge >> "{marker}"; cat\'\n'
        f"\tclean = sh -c 'echo clean >> \"{marker}\"; cat'\n"
        "[commit]\n\tgpgsign = true\n",
        encoding="utf-8",
    )
    fork.env["GIT_CONFIG_GLOBAL"] = str(config)
    fork.env["GIT_CONFIG_COUNT"] = "1"
    fork.env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    fork.env["GIT_CONFIG_VALUE_0"] = str(hooks)
    return marker


def test_watch_under_a_hostile_git_configuration_runs_none_of_it(
    bumpable: support.Fork,
) -> None:
    marker = hostile_git_config(bumpable)
    # The unapproved tree asks for the filter on the files the bump touches.
    bumpable.commit({"docs/.keep": "x\n"}, "docs")
    bumpable.fetch()
    attributes = bumpable.paths.clone / ".git" / "info" / "attributes"
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text("* filter=evil\n", encoding="utf-8")

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("pr-opened", common.EXIT_OK)
    assert not marker.exists()
    assert bumpable.remote_branches() == [BRANCH, "main"]  # pushed to the fork
    assert support.git(bumpable.root / "evil.git", "for-each-ref") == ""
    assert support.git(bumpable.bare, "log", "-1", "--format=%an <%ae>", BRANCH) == (
        f"{common.BOT_NAME} <{common.BOT_EMAIL}>"
    )
    assert bumpable.gh_calls("auth") == []  # no credential is asked for a local path


def test_report_never_adopts_a_look_alike_comment_by_someone_else(
    fork: support.Fork,
) -> None:
    stranger = {
        "id": 777,
        "body": f"{report.MARKER}\nposted by a stranger",
        "author": "x",
    }
    fork.set_gh(
        issues=[
            {"number": 3, "state": "OPEN", "body": "", "labels": [common.LABEL_STATUS]}
        ],
        comments=[stranger],
    )
    steps = [common.StepResult("promote", "up-to-date", 0)]

    first = report.post(fork.context(), steps)
    second = report.post(fork.context(), steps)

    assert (first.result, second.result) == ("posted", "posted")
    comments = fork.gh_state()["comments"]
    assert comments[0] == stranger  # never edited
    assert [comment["author"] for comment in comments] == ["x", common.FORK_OWNER]
    assert comments[1]["body"].startswith(report.MARKER)
    support.assert_fork_only(fork)


def test_report_under_another_login_writes_nothing(fork: support.Fork) -> None:
    fork.set_gh(
        login="some-other-account",
        issues=[
            {"number": 3, "state": "OPEN", "body": "", "labels": [common.LABEL_STATUS]}
        ],
    )

    result = report.post(fork.context(), [])

    assert (result.result, result.exit_code) == ("auth-or-tool", common.EXIT_FAILED)
    assert len(fork.gh_state()["calls"]) == 1  # the login probe, nothing after it
    assert fork.gh_state()["comments"] == []


@pytest.mark.parametrize("failing", ["issue list", "api"])
def test_report_when_github_fails_is_a_failure(
    fork: support.Fork, failing: str
) -> None:
    fork.set_gh(
        issues=[
            {"number": 3, "state": "OPEN", "body": "", "labels": [common.LABEL_STATUS]}
        ]
    )
    ctx = fork.context()
    assert ctx.github.is_owner()
    fork.set_gh(fail=["issue list"] if failing == "issue list" else ["api --hostname"])

    result = report.post(ctx, [])

    assert (result.result, result.exit_code) == ("github-failed", common.EXIT_FAILED)


def test_run_step_turns_a_refusal_and_a_crash_into_results(fork: support.Fork) -> None:
    ctx = fork.context()

    def refuse(_: common.Context) -> common.StepResult:
        raise common.Refused("not here")

    def crash(_: common.Context) -> common.StepResult:
        raise RuntimeError("boom")

    refused = maint_run.run_step(ctx, "distribute", refuse)
    crashed = maint_run.run_step(ctx, "distribute", crash)

    assert (refused.result, refused.exit_code) == ("refused", common.EXIT_USAGE)
    assert (crashed.result, crashed.exit_code) == ("crashed", common.EXIT_FAILED)
    assert "RuntimeError: boom" in crashed.detail


def test_run_reports_its_own_bump_as_promoted_after_the_merge(
    installed: support.Fork,
) -> None:
    # End to end: the Mac opens the bump, GitHub merges it, the next run
    # promotes it by itself and restarts on it.
    installed.set_catalog(entry(CURRENT), entry(NEWER))
    assert launch(installed, "run").returncode == common.EXIT_OK
    head = installed.squash_merge(BRANCH)

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_OK, done.stderr
    assert installed.approved() == head
    assert step_results(installed)["promote"] == "promoted"
    assert support.git(installed.paths.clone, "rev-parse", "HEAD") == head


def test_run_with_a_bump_merged_by_other_means_keeps_the_approved_revision(
    installed: support.Fork,
) -> None:
    old = installed.approved()
    installed.set_catalog(entry(CURRENT), entry(NEWER))
    installed.bump(NEWER)

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_OK, done.stderr
    assert installed.approved() == old
    assert step_results(installed)["promote"] == "awaiting-promotion"


def test_promote_at_a_terminal_advances_the_approved_revision(
    installed: support.Fork,
) -> None:
    head = installed.commit({support.HOOK_REL: "print('changed')\n"}, "hook (#7)")
    env = {**installed.env, "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0"}

    done = support.run_with_terminal(
        ["/bin/bash", str(installed.paths.launcher), "promote"], env, "yes\n"
    )

    assert done.returncode == common.EXIT_OK, done.stdout + done.stderr
    assert "hook (#7)" in done.stdout
    assert f"{common.FORK_WEB_URL}/commit/{head}" in done.stdout
    assert "/pull/7" not in done.stdout  # a subject's claim is not linked
    assert f"Inspect: git -C {installed.paths.clone} diff" in done.stdout
    assert installed.approved() == head


def test_promote_while_a_run_holds_the_lock_is_refused(installed: support.Fork) -> None:
    old = installed.approved()
    installed.commit({support.HOOK_REL: "print('changed')\n"}, "hook")
    held = common.acquire_lock(installed.paths)
    assert held is not None
    env = {**installed.env, "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0"}

    done = support.run_with_terminal(
        ["/bin/bash", str(installed.paths.launcher), "promote"], env, "yes\n"
    )

    assert done.returncode == common.EXIT_USAGE
    assert "holds the lock" in done.stdout
    assert installed.approved() == old


def test_run_from_a_clone_that_is_no_longer_the_jobs_refuses_before_creating_anything(
    installed: support.Fork,
) -> None:
    assert launch(installed, "run").returncode == common.EXIT_OK
    support.git(installed.paths.clone, "config", "--unset", common.OWNERSHIP_KEY)
    logs = sorted(path.name for path in installed.paths.logs_dir.iterdir())
    summary = installed.paths.last_run_file.read_text(encoding="utf-8")
    calls = len(installed.gh_state()["calls"])

    done = subprocess.run(
        [
            sys.executable,
            str(installed.paths.clone / "scripts" / "maint" / "run.py"),
            "run",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=installed.env,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == common.EXIT_USAGE
    assert "was not created by" in done.stderr
    assert sorted(path.name for path in installed.paths.logs_dir.iterdir()) == logs
    assert installed.paths.last_run_file.read_text(encoding="utf-8") == summary
    assert len(installed.gh_state()["calls"]) == calls


def test_launcher_removes_what_does_not_belong_to_the_approved_revision(
    installed: support.Fork,
) -> None:
    assert launch(installed, "run").returncode == common.EXIT_OK
    planted = installed.paths.clone / "scripts" / "maint" / "planted.py"
    planted.write_text("raise SystemExit('planted')\n", encoding="utf-8")
    edited = installed.paths.clone / "scripts" / "maint" / "run.py"
    original = edited.read_text(encoding="utf-8")
    edited.write_text("raise SystemExit('edited')\n", encoding="utf-8")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_OK, done.stderr
    assert not planted.exists()
    assert edited.read_text(encoding="utf-8") == original


def test_launcher_refuses_a_repository_kept_elsewhere(installed: support.Fork) -> None:
    elsewhere = installed.root / "elsewhere.git"
    (installed.paths.clone / ".git").rename(elsewhere)
    (installed.paths.clone / ".git").symlink_to(elsewhere, target_is_directory=True)
    refs = support.git(elsewhere, "for-each-ref")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_USAGE
    assert "does not hold its own repository" in done.stderr
    assert support.git(elsewhere, "for-each-ref") == refs
    assert installed.gh_state()["calls"] == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (["config", "--unset", "orchestra.maintclone"], "was not created by"),
        (
            ["config", "remote.origin.pushurl", "https://example.invalid/x.git"],
            "is not a clone of",
        ),
        (
            ["update-ref", "refs/heads/origin/main", "refs/remotes/origin/main"],
            "holds a ref",
        ),
    ],
    ids=["unmarked", "push-url", "shadowing-branch"],
)
def test_launcher_refuses_a_clone_it_cannot_vouch_for(
    installed: support.Fork, change: list[str], message: str
) -> None:
    support.git(installed.paths.clone, *change)

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_USAGE
    assert message in done.stderr
    assert list(installed.paths.clone.iterdir()) == [installed.paths.clone / ".git"]
    assert installed.gh_state()["calls"] == []


def test_launcher_refuses_a_clone_that_holds_a_shadowing_tag(
    installed: support.Fork,
) -> None:
    side = support.never_merged_commit(
        installed, {"scripts/maint/run.py": "raise SystemExit('never merged')\n"}
    )
    support.plant_shadow_tag(installed, side, in_clone=True)

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_USAGE
    assert "holds a ref the job never creates" in done.stderr
    assert installed.gh_state()["calls"] == []


def test_launcher_ignores_a_git_directory_named_in_the_environment(
    installed: support.Fork,
) -> None:
    # Another checkout of the same repository, with staged work, named by
    # GIT_DIR: the forced checkout must not reach it.
    dev = installed.root / "dev-checkout"
    subprocess.run(
        [support.REAL_GIT, "clone", "--quiet", str(installed.bare), str(dev)],
        check=True,
    )
    support.git(dev, "checkout", "--quiet", "-b", "my-feature")
    (dev / "README.md").write_text("# staged, uncommitted work\n", encoding="utf-8")
    support.git(dev, "add", "README.md")
    before = (
        support.git(dev, "symbolic-ref", "HEAD"),
        support.git(dev, "diff", "--cached"),
    )
    env = {
        **installed.env,
        "GIT_DIR": str(dev / ".git"),
        "GIT_WORK_TREE": str(dev),
        "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0",
    }

    done = subprocess.run(
        ["/bin/bash", str(installed.paths.launcher), "run"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
        cwd=str(installed.root),
    )

    assert done.returncode == common.EXIT_OK, done.stderr
    assert (
        support.git(dev, "symbolic-ref", "HEAD"),
        support.git(dev, "diff", "--cached"),
    ) == before
    assert (
        support.git(installed.paths.clone, "rev-parse", "HEAD") == installed.approved()
    )


def test_launcher_under_a_hostile_git_configuration_runs_none_of_it(
    installed: support.Fork,
) -> None:
    marker = hostile_git_config(installed)
    installed.set_catalog(entry(CURRENT), entry(NEWER))

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_OK, done.stderr
    assert step_results(installed)["model-watch"] == "pr-opened"
    assert not marker.exists()
    assert support.git(installed.root / "evil.git", "for-each-ref") == ""


def unusable_commit(fork: support.Fork) -> str:
    """A commit no file system can check out: one path longer than any name
    may be. Tracked like any other, so everything else about it works."""
    blob = (
        subprocess.run(
            [support.REAL_GIT, "-C", str(fork.seed), "hash-object", "-w", "--stdin"],
            input=b"x\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    name = "scripts/" + "a" * 300
    support.git(
        fork.seed, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"
    )
    support.git(fork.seed, "commit", "--quiet", "-m", "a path that cannot exist")
    support.git(fork.seed, "push", "--quiet", "origin", "HEAD:main")
    return support.git(fork.seed, "rev-parse", "HEAD")


def test_launcher_when_the_approved_revision_cannot_be_checked_out_runs_nothing(
    installed: support.Fork,
) -> None:
    # A good run first, so there is an older revision to fall back to. Then
    # the approved revision becomes one that cannot be checked out, while an
    # inert change and a newer model both wait: exactly what an older
    # revision's own rules would promote and open a pull request for.
    assert launch(installed, "run").returncode == common.EXIT_OK
    good = installed.approved()
    summary = installed.paths.last_run_file.read_text(encoding="utf-8")
    broken = unusable_commit(installed)
    installed.approve(broken)
    installed.commit({"README.md": "# inert, and waiting\n"}, "docs")
    installed.set_catalog(entry(CURRENT), entry(NEWER))
    installed.set_gh(
        issues=[
            {"number": 3, "state": "OPEN", "body": "", "labels": [common.LABEL_STATUS]}
        ]
    )
    calls = len(installed.gh_state()["calls"])

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_FAILED
    assert "cannot check out the approved revision" in done.stderr
    assert "nothing was run" in done.stderr and "Next:" in done.stderr
    assert installed.approved() == broken  # nothing promoted
    assert installed.remote_branches() == ["main"]  # no bump branch
    assert len(installed.gh_state()["calls"]) == calls  # no pull request, no comment
    assert installed.paths.last_run_file.read_text(encoding="utf-8") == summary
    assert broken in installed.paths.launcher_failure_file.read_text(encoding="utf-8")

    # `status` and `promote` still work, on the last revision that did check
    # out, and say what is wrong.
    status = launch(installed, "status")
    assert status.returncode == common.EXIT_OK
    assert "LAUNCHER FAILURE" in status.stdout
    assert support.git(installed.paths.clone, "rev-parse", "HEAD") == good

    support.git(
        installed.seed, "rm", "--quiet", "--cached", "-r", "scripts/" + "a" * 300
    )
    support.git(installed.seed, "commit", "--quiet", "-m", "remove the path")
    support.git(installed.seed, "push", "--quiet", "origin", "HEAD:main")
    fixed = support.git(installed.seed, "rev-parse", "HEAD")
    env = {**installed.env, "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0"}
    promoted = support.run_with_terminal(
        ["/bin/bash", str(installed.paths.launcher), "promote"], env, "yes\n"
    )
    after = launch(installed, "run")

    assert promoted.returncode == common.EXIT_OK, promoted.stdout + promoted.stderr
    assert installed.approved() == fixed
    assert after.returncode == common.EXIT_OK, after.stderr
    assert not installed.paths.launcher_failure_file.exists()
    assert step_results(installed)["model-watch"] == "pr-opened"


def test_run_on_a_fallback_revision_promotes_nothing_and_opens_nothing(
    installed: support.Fork,
) -> None:
    # The dispatcher's own refusal, for a `run` that reaches it on a revision
    # the launcher marked as a fallback.
    assert launch(installed, "run").returncode == common.EXIT_OK
    approved = installed.approved()
    installed.commit({"README.md": "# inert, and waiting\n"}, "docs")
    installed.fetch()
    installed.set_catalog(entry(CURRENT), entry(NEWER))
    calls = len(installed.gh_state()["calls"])
    env = {**installed.env, common.CHECKOUT_FAILED_ENV: "f" * 40}

    done = subprocess.run(
        [
            sys.executable,
            str(installed.paths.clone / "scripts" / "maint" / "run.py"),
            "run",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == common.EXIT_FAILED
    assert installed.approved() == approved
    assert installed.remote_branches() == ["main"]
    assert len(installed.gh_state()["calls"]) == calls
    assert step_results(installed) == {"launcher": "checkout-failed"}
    assert last_run(installed)["exit_code"] == common.EXIT_FAILED


def test_launcher_with_no_revision_it_can_check_out_stops_with_a_next_step(
    installed: support.Fork,
) -> None:
    installed.approve(unusable_commit(installed))

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_FAILED
    assert "cannot check out the approved revision" in done.stderr
    assert "Next:" in done.stderr
    assert "cannot check out" in installed.paths.launcher_failure_file.read_text(
        encoding="utf-8"
    )
    assert not installed.paths.last_run_file.exists()
    assert installed.gh_state()["calls"] == []


def test_launcher_without_python_stops_with_a_next_step(
    installed: support.Fork, tmp_path: Path
) -> None:
    minimal = tmp_path / "minimal-bin"
    minimal.mkdir()
    for tool in ("sleep", "date", "mv", "rm", "tr", "mkdir", "cat"):
        found = shutil.which(tool)
        assert found is not None
        (minimal / tool).symlink_to(found)
    shutil.copy2(installed.bin_dir / "git", minimal / "git")
    env = {
        **installed.env,
        "PATH": str(minimal),
        "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0",
    }

    done = subprocess.run(
        ["/bin/bash", str(installed.paths.launcher), "run"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == common.EXIT_FAILED
    assert "python3 is not on PATH" in done.stderr
    assert "python3 is not on PATH" in installed.paths.launcher_failure_file.read_text(
        encoding="utf-8"
    )


def test_launcher_ends_a_git_command_that_stalls(installed: support.Fork) -> None:
    installed.stall_git("fetch")
    env = {
        **installed.env,
        "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0",
        "ORCHESTRA_MAINT_GIT_DEADLINE_SEC": "1",
    }
    started = time.monotonic()

    done = subprocess.run(
        ["/bin/bash", str(installed.paths.launcher), "run"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == common.EXIT_FAILED
    assert "cannot fetch the fork" in done.stderr
    assert time.monotonic() - started < 15  # three attempts of one second each
    assert installed.gh_state()["calls"] == []


# --------------------------------------------------------------------------
# Round 3
# --------------------------------------------------------------------------


def test_watch_with_a_model_override_file_on_the_fork_is_not_steered_by_it(
    bumpable: support.Fork,
) -> None:
    # The file a project uses to override the manifest, merged on the fork and
    # naming a model the catalog does not list. Read through the manifest
    # tool it would become "the current model".
    override = {"schema_version": 1, "codex": {"model": f"gpt-1-{FAMILY}"}}
    bumpable.commit(
        {promote.LOCAL_OVERRIDE_REL: json.dumps(override) + "\n"}, "chore: override"
    )
    bumpable.fetch()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "override-in-tree",
        common.EXIT_NEEDS_OWNER,
    )
    assert bumpable.gh_state()["issues"] == []  # no "model not listed" notice
    assert bumpable.codex_calls("exec") == []
    assert_nothing_published(bumpable)
    assert_clone_left_clean(bumpable)


def test_watch_reads_the_current_model_from_the_manifest_at_the_branch(
    bumpable: support.Fork,
) -> None:
    ctx = bumpable.context()
    main = str(ctx.git.commit_of(common.MAIN_REF))

    assert model_watch._manifest_values(ctx, main) == (CURRENT, EFFORT)
    assert bumpable.codex_state()["calls"] == []  # by reading one blob, no tool


def prepared_elsewhere(fork: support.Fork) -> str:
    """A bump branch of exactly the right shape that this Mac did not build:
    the manifest is serialized differently and its history gains an entry."""
    support.git(fork.seed, "checkout", "--quiet", "-b", "prepared")
    for args in (["set", "codex.model", NEWER], ["apply"]):
        support.run_models(fork.seed, args)
    path = fork.seed / support.MANIFEST_REL
    document = json.loads(path.read_text(encoding="utf-8"))
    document["superseded"]["codex.model"].append(f"gpt-7.1.5-{FAMILY}")
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    support.git(fork.seed, "commit", "--quiet", "-am", "prepared elsewhere")
    support.git(fork.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}")
    head = support.git(fork.seed, "rev-parse", "HEAD")
    support.git(fork.seed, "checkout", "--quiet", "main")
    return head


def test_watch_does_not_vouch_for_a_bump_branch_someone_else_prepared(
    bumpable: support.Fork,
) -> None:
    head = prepared_elsewhere(bumpable)
    ctx = bumpable.context()
    main = str(ctx.git.commit_of(common.MAIN_REF))

    result = watch(bumpable)

    # It is a well-formed bump to the right slug...
    assert shape_problem(bumpable, main, head) is None
    # ...and still not this Mac's: no live call, no record, no pull request.
    assert (result.result, result.exit_code) == (
        "unknown-branch",
        common.EXIT_NEEDS_OWNER,
    )
    assert "not with the files this Mac builds" in result.detail
    assert bumpable.codex_calls("exec") == []
    assert not bumpable.paths.bump_records_file.exists()
    assert bumpable.gh_calls("pr", "create") == []
    assert support.git(bumpable.bare, "rev-parse", BRANCH) == head  # left as it is
    assert_clone_left_clean(bumpable)


def shape_problem(fork: support.Fork, base: str, head: str) -> str | None:
    """The shape verdict for a commit the watch has fetched."""
    return bump.bump_problem_between(fork.context().git, base, head, NEWER)


def test_watch_withdraws_auto_merge_from_an_open_pull_request_with_other_bytes(
    bumpable: support.Fork,
) -> None:
    # The Mac's own pull request, then its branch is replaced by a bump of the
    # same shape that the Mac did not build.
    open_bump(bumpable)
    support.git(bumpable.bare, "update-ref", "-d", f"refs/heads/{BRANCH}")
    prepared_elsewhere(bumpable)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "unknown-branch",
        common.EXIT_NEEDS_OWNER,
    )
    assert "auto-merge withdrawn" in result.detail
    assert len(bumpable.gh_state()["merges"]) == 1
    assert len(bumpable.gh_state()["disabled"]) == 1


def test_watch_forgets_its_record_when_the_owner_closes_the_pull_request(
    bumpable: support.Fork,
) -> None:
    open_bump(bumpable)
    pulls = bumpable.gh_state()["pulls"]
    pulls[-1]["state"] = "CLOSED"
    bumpable.set_gh(pulls=pulls)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("rejected-by-owner", common.EXIT_OK)
    records = json.loads(bumpable.paths.bump_records_file.read_text(encoding="utf-8"))
    assert records == []


def test_launcher_fails_closed_when_the_refs_cannot_be_listed(
    installed: support.Fork,
) -> None:
    installed.fail_git("for-each-ref")

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_USAGE
    assert "cannot list the refs" in done.stderr
    assert list(installed.paths.clone.iterdir()) == [installed.paths.clone / ".git"]
    assert installed.gh_state()["calls"] == []


# --------------------------------------------------------------------------
# Residuals (issue #10)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lock", ["index.lock", "refs/heads/approved.lock"])
def test_launcher_names_a_lock_a_killed_run_left_and_the_command_to_remove_it(
    installed: support.Fork, lock: str
) -> None:
    # A run killed in the middle of a checkout leaves git's lock behind, and
    # every later checkout fails on it. Merging a fix cannot clear that.
    assert launch(installed, "run").returncode == common.EXIT_OK
    stale = installed.paths.clone / ".git" / lock
    stale.write_text("", encoding="utf-8")
    calls = len(installed.gh_state()["calls"])

    done = launch(installed, "run")
    status = launch(installed, "status")

    record = installed.paths.launcher_failure_file.read_text(encoding="utf-8")
    for said in (done.stderr, status.stderr, record):
        assert "a previous run was killed and left the lock" in said
        assert f"Next: rm {stale}  " in said
        assert "merge a fix" not in said
        assert "run launchd.sh install again" not in said
    assert (done.returncode, status.returncode) == (common.EXIT_FAILED,) * 2
    assert stale.exists()  # never removed by the job
    assert len(installed.gh_state()["calls"]) == calls  # nothing ran

    stale.unlink()
    after = launch(installed, "run")

    assert after.returncode == common.EXIT_OK, after.stderr
    assert not installed.paths.launcher_failure_file.exists()


def test_launcher_names_the_first_lock_git_names_that_really_is_stale(
    installed: support.Fork,
) -> None:
    # git can name more than one lock; the first one it names may not exist.
    assert launch(installed, "run").returncode == common.EXIT_OK
    clone_git = installed.paths.clone / ".git"
    stale = clone_git / "refs" / "heads" / "approved.lock"
    stale.write_text("", encoding="utf-8")
    installed.fail_git(
        "checkout",
        said=(
            f"error: Unable to create '{clone_git / 'gone.lock'}': File exists.\n"
            f"fatal: Unable to create '{stale}': File exists.\n"
        ),
    )

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_FAILED
    assert f"left the lock {stale} in the maintenance clone" in done.stderr
    assert f"Next: rm {stale}  " in done.stderr
    assert "gone.lock in the maintenance clone" not in done.stderr
    assert stale.exists()


def test_launcher_names_a_lock_that_stops_the_fetch(installed: support.Fork) -> None:
    assert launch(installed, "run").returncode == common.EXIT_OK
    installed.commit({"README.md": "# moved on\n"}, "docs")  # the fetch must write
    stale = installed.paths.clone / ".git" / "refs" / "remotes" / "origin" / "main.lock"
    stale.write_text("", encoding="utf-8")
    calls = len(installed.gh_state()["calls"])

    done = launch(installed, "run")

    record = installed.paths.launcher_failure_file.read_text(encoding="utf-8")
    assert done.returncode == common.EXIT_FAILED
    for said in (done.stderr, record):
        assert "cannot fetch the fork: a previous run was killed and left" in said
        assert f"Next: rm {stale}  " in said
        assert "check the network" not in said
    assert stale.exists()
    assert len(installed.gh_state()["calls"]) == calls  # nothing ran


def test_launcher_without_a_lock_keeps_the_network_message_for_a_failed_fetch(
    installed: support.Fork,
) -> None:
    installed.bare.rename(installed.root / "gone.git")

    done = launch(installed, "run")

    expected = (
        "orchestra-maint: cannot fetch the fork. Next: check the network, "
        "then: bash scripts/maint/launchd.sh run-now"
    )
    record = installed.paths.launcher_failure_file.read_text(encoding="utf-8")
    assert done.returncode == common.EXIT_FAILED
    assert done.stderr.endswith(expected + "\n")
    assert record.split(" ", 1)[1] == expected + "\n"
    assert "killed" not in done.stderr


LINGER_BOUND_SEC = 20  # the lingering child lives 30 s; a hang outlasts this


def launch_with_short_deadline(fork: support.Fork) -> tuple[int, str, float]:
    """One `run` with a one-second git deadline: (exit code, stderr, seconds).
    A launcher that is still running after LINGER_BOUND_SEC fails the test."""
    env = {
        **fork.env,
        "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0",
        "ORCHESTRA_MAINT_GIT_DEADLINE_SEC": "1",
    }
    started = time.monotonic()
    try:
        done = subprocess.run(
            ["/bin/bash", str(fork.paths.launcher), "run"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=LINGER_BOUND_SEC,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"the launcher was still running after {LINGER_BOUND_SEC} s")
    return done.returncode, done.stderr, time.monotonic() - started


def test_launcher_ends_a_fetch_whose_helper_outlives_git(
    installed: support.Fork,
) -> None:
    # git-remote-https stuck on the network keeps git's output open after the
    # deadline ended git: reading that output must not wait for it.
    installed.linger_git("fetch")
    try:
        code, stderr, seconds = launch_with_short_deadline(installed)
    finally:
        installed.reap_lingering()

    assert code == common.EXIT_FAILED
    assert stderr.endswith(
        "orchestra-maint: cannot fetch the fork. Next: check the network, "
        "then: bash scripts/maint/launchd.sh run-now\n"
    )
    assert seconds < LINGER_BOUND_SEC
    assert installed.gh_state()["calls"] == []


def test_launcher_ends_a_checkout_whose_child_outlives_git(
    installed: support.Fork,
) -> None:
    installed.linger_git("checkout")
    try:
        code, stderr, seconds = launch_with_short_deadline(installed)
    finally:
        installed.reap_lingering()

    assert code == common.EXIT_FAILED
    assert "cannot check out the approved revision" in stderr
    assert "merge a fix" in stderr
    assert seconds < LINGER_BOUND_SEC
    assert installed.gh_state()["calls"] == []


def test_launcher_reads_a_bounded_part_of_a_huge_git_message(
    installed: support.Fork,
) -> None:
    # Far more than the launcher reads back, every name a lock that is not
    # the job's own: looking at them must stay cheap.
    noise = "".join(
        f"error: Unable to create '/nowhere/{index:06d}.lock': File exists.\n"
        for index in range(20000)
    )
    installed.fail_git("checkout", said=noise)
    started = time.monotonic()

    done = launch(installed, "run")

    assert time.monotonic() - started < 30
    assert done.returncode == common.EXIT_FAILED
    assert "merge a fix, then: bash scripts/maint/launchd.sh promote" in done.stderr
    assert "killed" not in done.stderr
    assert len(done.stderr) < 2 * 65536  # what git said is shown cut short


def test_launcher_ends_a_checkout_that_stalls_while_it_reads_what_git_said(
    installed: support.Fork,
) -> None:
    # git's words are now captured for the lock check: the deadline must
    # still end a checkout that hangs.
    installed.stall_git("checkout")
    env = {
        **installed.env,
        "ORCHESTRA_MAINT_FETCH_PAUSE_SEC": "0",
        "ORCHESTRA_MAINT_GIT_DEADLINE_SEC": "1",
    }
    started = time.monotonic()

    done = subprocess.run(
        ["/bin/bash", str(installed.paths.launcher), "run"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
    )

    assert done.returncode == common.EXIT_FAILED
    assert "cannot check out the approved revision" in done.stderr
    assert "killed" not in done.stderr
    assert time.monotonic() - started < 15
    assert installed.gh_state()["calls"] == []


def test_launcher_without_a_lock_keeps_the_message_for_a_broken_revision(
    installed: support.Fork,
) -> None:
    assert launch(installed, "run").returncode == common.EXIT_OK
    installed.approve(unusable_commit(installed))

    done = launch(installed, "run")

    assert done.returncode == common.EXIT_FAILED
    assert "merge a fix, then: bash scripts/maint/launchd.sh promote" in done.stderr
    assert "killed" not in done.stderr


@pytest.mark.parametrize(
    "name", [".claude/Models.Local.json", ".CLAUDE/MODELS.LOCAL.JSON"]
)
def test_watch_with_a_differently_cased_override_file_does_nothing(
    bumpable: support.Fork, name: str
) -> None:
    # macOS checks it out as the very file the manifest tool reads.
    override = {"schema_version": 1, "codex": {"model": f"gpt-1-{FAMILY}"}}
    bumpable.commit_entry(name, json.dumps(override) + "\n", "chore: override")
    bumpable.fetch()

    result = watch(bumpable)

    assert (result.result, result.exit_code) == (
        "override-in-tree",
        common.EXIT_NEEDS_OWNER,
    )
    assert name in result.detail
    assert bumpable.codex_calls("exec") == []
    assert_nothing_published(bumpable)


def write_records(fork: support.Fork, **changes: object) -> None:
    path = fork.paths.bump_records_file
    records = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps([{**r, **changes} for r in records]), encoding="utf-8")


def recorded_pulls(fork: support.Fork) -> list[object]:
    path = fork.paths.bump_records_file
    return [r["pull"] for r in json.loads(path.read_text(encoding="utf-8"))]


def test_watch_ties_its_open_pull_request_to_a_record_left_without_one(
    bumpable: support.Fork,
) -> None:
    # A run stopped between opening the pull request and recording its number.
    head = open_bump(bumpable)
    write_records(bumpable, pull=None)

    result = watch(bumpable)

    assert (result.result, result.exit_code) == ("pending", common.EXIT_OK)
    assert recorded_pulls(bumpable) == [10]
    assert bumpable.gh_state()["pulls"][0]["headRefOid"] == head
    assert len(bumpable.gh_calls("pr", "create")) == 1


@pytest.mark.parametrize("where", ["pull-head", "record-commit"])
def test_watch_never_ties_a_pull_request_with_another_head_to_a_record(
    bumpable: support.Fork, where: str
) -> None:
    open_bump(bumpable)
    if where == "pull-head":
        change = {"headRefOid": "a" * 40}
        bumpable.set_gh(pulls=[{**bumpable.gh_state()["pulls"][0], **change}])
        write_records(bumpable, pull=None)
    else:
        write_records(bumpable, pull=None, commit="b" * 40)

    watch(bumpable)

    assert recorded_pulls(bumpable) == [None]
