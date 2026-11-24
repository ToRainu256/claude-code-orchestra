#!/usr/bin/env python3
"""Run the fork's local maintenance steps, one after another.

This is what the LaunchAgent's launcher executes, from the approved checkout
and from nowhere else: it refuses to start when the file it was loaded from is
not inside the clone under the state root, so it cannot be pointed at a
developer's checkout by mistake.

``run`` promotes first, then executes every entry of ``STEPS`` in order, then
reports. Each step returns its own result and one failing does not stop the
others; the process exits with the highest step code. A step is added by
adding one entry to ``STEPS``: the lock, the log, the summary file and the
status comment already treat steps as a list.

Usage:
    python3 run.py run                 # the scheduled run
    python3 run.py promote             # the owner's typed confirmation
    python3 run.py adopt PROJECT       # the owner lets the job maintain a project
    python3 run.py status              # last run and approved-vs-main; reads only
    python3 run.py --state-root DIR …  # default: $ORCHESTRA_MAINT_HOME, else
                                       # ~/.claude/orchestra

Exit codes:
    0  ok, nothing to do, or another run holds the lock
    1  usage or configuration error, or a refused working directory
    2  a human must act
    3  failure: a tool, the network, or authentication
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import adopt
import common
import distribute
import ledger
import model_watch
import promote
import report

Step = Callable[[common.Context], common.StepResult]
# Executed in this order, after promotion and before the report, so each sees
# the revision the Mac is now allowed to act on. Distribution comes first: it
# needs nothing but the approved revision, and it must not wait for a model
# catalog or a live call.
STEPS: tuple[tuple[str, Step], ...] = (
    (distribute.STEP, distribute.run),
    (model_watch.STEP, model_watch.run),
)
LAUNCHER_STEP = "launcher"
# Outcomes this dispatcher reports for a step that did not report its own,
# and for the launcher when it had to fall back. Pinned to the README table.
OUTCOMES: dict[str, tuple[int, ...]] = {
    "refused": (common.EXIT_USAGE,),
    "crashed": (common.EXIT_FAILED,),
    "checkout-failed": (common.EXIT_FAILED,),
}
BASH = "/bin/bash"
AFTER_PROMOTION_FLAG = "--after-promotion"
REPO_DEPTH_ABOVE_SCRIPT = 2
SECONDS_PER_HOUR = 3600


def tools_root() -> Path:
    """The checkout this file was loaded from."""
    return Path(__file__).resolve().parents[REPO_DEPTH_ABOVE_SCRIPT]


def require_home_ground(paths: common.Paths) -> None:
    """Refuse unless this code runs from the clone the job owns. Called before
    anything is created, so a refused start leaves no lock, log or directory
    behind."""
    probe = common.make_context(paths, tools_root(), common.Log(None))
    common.require_owned_clone(probe.git)
    if probe.tools_root != paths.clone.resolve():
        raise common.Refused(
            f"this script runs only from {paths.clone}, not from {probe.tools_root}"
        )


def run_step(ctx: common.Context, name: str, step: Step) -> common.StepResult:
    """Run one step. A step that raises is a failed step, not a failed run."""
    try:
        return step(ctx)
    except common.Refused as exc:
        ctx.log.line(f"{name}: refused: {exc}")
        return common.checked_result(
            OUTCOMES, name, "refused", common.EXIT_USAGE, str(exc)
        )
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        ctx.log.line(f"{name}: crashed: {text}")
        return common.checked_result(
            OUTCOMES, name, "crashed", common.EXIT_FAILED, text
        )


def write_last_run(
    ctx: common.Context, steps: list[common.StepResult], exit_code: int
) -> None:
    approved, head, awaiting = promote.awaiting_summary(ctx)
    finished = time.time()
    summary = {
        "started": common.utc_stamp(ctx.now),
        "finished": common.utc_stamp(finished),
        "finished_epoch": int(finished),
        "exit_code": exit_code,
        "approved": approved,
        "main": head,
        "awaiting": awaiting,
        "steps": [
            {
                "name": step.name,
                "result": step.result,
                "exit_code": step.exit_code,
                "detail": step.detail,
                "counts": step.counts,
            }
            for step in steps
        ],
    }
    common.write_text_atomic(
        ctx.paths.last_run_file, json.dumps(summary, indent=2) + "\n"
    )


def reexec_after_promotion(ctx: common.Context, lock: int, previous: str) -> None:
    """Start over through the launcher, so the rest of this run executes the
    revision that was just approved. Returns only when there is no installed
    launcher to go through; the run then continues on the previous revision,
    which is approved code too."""
    launcher = ctx.paths.launcher
    if launcher.is_symlink() or not launcher.is_file():
        ctx.log.line("promote: no installed launcher; continuing on this revision")
        return
    ctx.log.line("promote: restarting through the launcher")
    env = {**os.environ, common.STATE_ROOT_ENV: str(ctx.paths.state_root)}
    sys.stdout.flush()
    os.close(lock)
    os.execve(BASH, [BASH, str(launcher), "run", AFTER_PROMOTION_FLAG, previous], env)


def promote_step(
    ctx: common.Context, lock: int, after_promotion: str | None
) -> common.StepResult:
    """Promotion, and the one restart it may cause. On the pass after that
    restart the range is empty, and the result says what the pass before did."""
    before = promote.read_approved(ctx.paths)
    result = run_step(ctx, promote.STEP, promote.auto_promote)
    if after_promotion is None:
        if result.result == promote.RESULT_PROMOTED and before is not None:
            reexec_after_promotion(ctx, lock, before)
        return result
    if result.result != promote.RESULT_UP_TO_DATE:
        return result
    moved = f"{common.short_sha(after_promotion)} -> {common.short_sha(before)}"
    return common.checked_result(
        promote.OUTCOMES, promote.STEP, promote.RESULT_PROMOTED, common.EXIT_OK, moved
    )


def launcher_result() -> list[common.StepResult]:
    """What the launcher had to do to get here, when it was not the plain
    case: the approved revision would not check out and this is the last one
    that did. The launcher itself stops a `run` in that state; this is the
    same refusal for a `run` that reaches this file some other way."""
    failed = os.environ.get(common.CHECKOUT_FAILED_ENV, "")
    if not failed:
        return []
    detail = (
        f"the approved revision {common.short_sha(failed)} could not be checked "
        "out, and nothing runs on an older one. Merge a fix, then run "
        "launchd.sh promote."
    )
    return [
        common.checked_result(
            OUTCOMES, LAUNCHER_STEP, "checkout-failed", common.EXIT_FAILED, detail
        )
    ]


def cmd_run(paths: common.Paths, after_promotion: str | None) -> int:
    lock = common.acquire_lock(paths)
    if lock is None:
        print("run: another run holds the lock; nothing done")
        return common.EXIT_OK
    now = time.time()
    ctx = common.make_context(
        paths, tools_root(), common.open_run_log(paths, now), now=now
    )
    ctx.log.line(f"run: start from {common.short_sha(promote.read_approved(paths))}")

    results = launcher_result()
    if results:
        # This is not the approved revision: the launcher fell back to an
        # older one. That is enough to show the owner what waits (`promote`,
        # `status`); it is never enough to promote or to open a pull request.
        write_last_run(ctx, results, common.EXIT_FAILED)
        print(common.one_line(f"run: {results[0].result}: {results[0].detail}"))
        ctx.log.line("run: refused on a fallback revision; exit 3")
        return common.EXIT_FAILED
    results.append(promote_step(ctx, lock, after_promotion))
    for name, step in STEPS:
        results.append(run_step(ctx, name, step))
    done = list(results)
    results.append(run_step(ctx, report.STEP, lambda c: report.post(c, done)))

    exit_code = max(step.exit_code for step in results)
    write_last_run(ctx, results, exit_code)
    for step in results:
        line = f"{step.name}: {step.result} (exit {step.exit_code}) {step.detail}"
        ctx.log.line(line)
        print(common.one_line(line))
    ctx.log.line(f"run: exit {exit_code}")
    return exit_code


def ask(prompt: str) -> str:
    """Read one typed answer. A closed terminal is an answer too: not
    confirmed."""
    try:
        return input(prompt)
    except EOFError:
        return ""


def attended_context(paths: common.Paths, command: str) -> common.Context | None:
    """The context for a command the owner answers at a terminal, with the run
    lock held; None, after saying why, when it cannot be given."""
    if not sys.stdin.isatty():
        print(f"{command}: needs a terminal; its confirmation is typed, never piped")
        return None
    # The lock is held until this process ends: its descriptor is never closed.
    if common.acquire_lock(paths) is None:
        print(f"{command}: a run holds the lock; try again when it has finished")
        return None
    now = time.time()
    return common.make_context(
        paths, tools_root(), common.open_run_log(paths, now), now=now
    )


def cmd_promote(paths: common.Paths) -> int:
    ctx = attended_context(paths, "promote")
    if ctx is None:
        return common.EXIT_USAGE
    return promote.manual_promote(ctx, ask, print)


def cmd_adopt(paths: common.Paths, target: str) -> int:
    if launcher_result():
        # Same rule as a run: nothing is distributed from a fallback revision.
        # The launcher already stops before this; the check is for a start
        # that reaches this file some other way.
        print("adopt: the approved revision could not be checked out; nothing done")
        return common.EXIT_FAILED
    ctx = attended_context(paths, "adopt")
    if ctx is None:
        return common.EXIT_USAGE
    return adopt.run(ctx, target, ask, print)


def cmd_status(paths: common.Paths) -> int:
    ctx = common.make_context(paths, tools_root(), common.Log(None))
    try:
        failure = paths.launcher_failure_file.read_text(encoding="utf-8")
    except OSError:
        failure = ""
    if failure.strip():
        # Written by the launcher when it could not start the run normally;
        # everything below is then from before that failure.
        print(f"LAUNCHER FAILURE  {common.one_line(failure)}")
    approved, head, awaiting = promote.awaiting_summary(ctx)
    waiting = "unknown" if awaiting is None else str(awaiting)
    print(f"approved  {approved or '(none)'}")
    print(f"main      {head or '(unknown)'}  (as of the last fetch)")
    print(f"awaiting promotion: {waiting}")
    try:
        summary = json.loads(paths.last_run_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("last run  (none recorded)")
        return common.EXIT_OK
    if not isinstance(summary, dict):
        print("last run  (unreadable summary)")
        return common.EXIT_OK
    finished = summary.get("finished_epoch")
    age = "unknown age"
    if isinstance(finished, int):
        age = f"{max(time.time() - finished, 0) / SECONDS_PER_HOUR:.1f} hours ago"
    print(
        common.one_line(
            f"last run  finished {summary.get('finished')} ({age}), "
            f"exit {summary.get('exit_code')}"
        )
    )
    steps = summary.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        line = (
            f"  {step.get('name')}: {step.get('result')} "
            f"(exit {step.get('exit_code')}) {step.get('detail') or ''}"
        )
        print(common.one_line(line))
    for line in ledger.status_lines(paths):
        print(common.one_line(line))
    return common.EXIT_OK


def _sha(value: str) -> str:
    if not common.SHA_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("not a commit SHA")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the fork's local maintenance steps."
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help=f"State directory (default: ${common.STATE_ROOT_ENV}, else ~/.claude/orchestra)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    run_parser = commands.add_parser("run", help="Run every step once")
    run_parser.add_argument(
        AFTER_PROMOTION_FLAG,
        type=_sha,
        default=None,
        metavar="SHA",
        help="Set by this script when it restarts after a promotion",
    )
    commands.add_parser("promote", help="Show the waiting range and ask to approve it")
    adopt_parser = commands.add_parser(
        "adopt", help="Show what an update would change in a project and ask"
    )
    adopt_parser.add_argument("target", metavar="PROJECT", help="Its absolute path")
    commands.add_parser("status", help="Print the last run and the approved revision")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = common.Paths(common.resolve_state_root(args.state_root, dict(os.environ)))
    try:
        require_home_ground(paths)
        # Children resolve tools from here, never from wherever the command
        # was typed: a version manager reads its configuration from the
        # working directory and its parents.
        os.chdir(str(paths.state_root))
        if args.command == "run":
            return cmd_run(paths, args.after_promotion)
        if args.command == "promote":
            return cmd_promote(paths)
        if args.command == "adopt":
            return cmd_adopt(paths, args.target)
        return cmd_status(paths)
    except common.Refused as exc:
        print(f"refused: {common.safe_text(str(exc))}", file=sys.stderr)
        return common.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
