#!/usr/bin/env python3
"""Post the job's status comment: the public heartbeat of the local run.

One issue on the fork, labeled ``automation:status``, is the dashboard. This
step replaces the job's own comment there after every run. The comment's time
is the heartbeat: a job that stopped is a comment that stopped changing.

The repository is public, so two rules hold here:

* **Outcomes and counts only.** A step's free-text detail, a path, a project
  name or a log line is never printed. Anything that is not a plain outcome
  word or an integer is replaced, not escaped.
* **Only the owner's own comment.** Anyone can comment on a public issue. The
  job's comment is the one that starts with the marker **and** was written by
  the fork owner; a look-alike by someone else is never edited or trusted,
  and nothing is posted when ``gh`` is logged in as another account.

Outcomes and exit codes: ``OUTCOMES`` below, explained in
``scripts/maint/README.md``.
"""

from __future__ import annotations

import re

import common
import promote

STEP = "report"
# Every outcome this step reports, with the exit codes it may carry. The
# README table is pinned to this by a test.
OUTCOMES: dict[str, tuple[int, ...]] = {
    "posted": (common.EXIT_OK,),
    "no-status-issue": (common.EXIT_OK,),
    "auth-or-tool": (common.EXIT_FAILED,),
    "github-failed": (common.EXIT_FAILED,),
}
MARKER = common.job_marker(common.LOCAL_JOB)
# The only shape a step outcome or a count name may have in a public comment.
PUBLIC_WORD_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
REDACTED_WORD = "unprintable"


def public_word(value: object) -> str:
    """``value`` when it is a plain outcome word, else a fixed stand-in."""
    if isinstance(value, str) and PUBLIC_WORD_RE.fullmatch(value):
        return value
    return REDACTED_WORD


def render(
    steps: list[common.StepResult],
    approved: str | None,
    head: str | None,
    awaiting: int | None,
    now: float,
) -> str:
    """The comment body: time, exit code, revisions, one row per step."""
    worst = max([step.exit_code for step in steps], default=common.EXIT_OK)
    waiting = "unknown" if awaiting is None else str(int(awaiting))
    lines = [
        MARKER,
        f"**Local maintenance** · {common.utc_display(now)} · exit {int(worst)}",
        "",
        f"approved `{common.short_sha(approved)}` · main `{common.short_sha(head)}`"
        f" · awaiting promotion: {waiting}",
        "",
        "| step | result | exit | counts |",
        "|------|--------|------|--------|",
    ]
    for step in steps:
        counts = ", ".join(
            f"{public_word(key)} {int(value)}"
            for key, value in sorted(step.counts.items())
        )
        lines.append(
            f"| {public_word(step.name)} | {public_word(step.result)}"
            f" | {int(step.exit_code)} | {counts} |"
        )
    return "\n".join(lines) + "\n"


def _result(result: str, exit_code: int, detail: str = "") -> common.StepResult:
    return common.checked_result(OUTCOMES, STEP, result, exit_code, detail)


def post(ctx: common.Context, steps: list[common.StepResult]) -> common.StepResult:
    """Replace this job's comment in the status issue. The owner creates that
    issue once; until then there is nowhere to post and that is not an error."""
    if not ctx.github.is_owner():
        return _result(
            "auth-or-tool", common.EXIT_FAILED, "gh is not the fork owner; not posting"
        )
    approved, head, awaiting = promote.awaiting_summary(ctx)
    issues = ctx.github.issues_with_label(common.LABEL_STATUS, state="open")
    if issues is None:
        return _result("github-failed", common.EXIT_FAILED, "cannot list issues")
    numbers = sorted(
        issue["number"] for issue in issues if isinstance(issue.get("number"), int)
    )
    if not numbers:
        ctx.github.ensure_label(common.LABEL_STATUS)
        hint = f"create one issue labeled {common.LABEL_STATUS}"
        return _result("no-status-issue", common.EXIT_OK, hint)
    comments = ctx.github.comments(numbers[0])
    if comments is None:
        return _result("github-failed", common.EXIT_FAILED, "cannot list comments")
    mine = [
        comment["id"]
        for comment in comments
        if str(comment.get("body", "")).startswith(MARKER)
        and comment.get("author") == common.FORK_OWNER
        and isinstance(comment.get("id"), int)
    ]
    body = render(steps, approved, head, awaiting, ctx.now)
    if mine:
        posted = ctx.github.update_comment(mine[0], body)
    else:
        posted = ctx.github.create_comment(numbers[0], body)
    if not posted.ok:
        return _result("github-failed", common.EXIT_FAILED, posted.failure())
    return _result("posted", common.EXIT_OK)
