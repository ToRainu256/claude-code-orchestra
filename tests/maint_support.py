"""Scratch fork, fake ``gh``, fake ``codex`` and a ``git`` stand-in for the
maintenance-job tests.

Nothing here reaches the network, the developer's checkout, or the home
directory. The "fork" is a bare repository in a temp directory. The job's
clone names the real fork URL as ``origin`` (the code under test insists on
that, and checks where git would really fetch from and push to), so the
redirect cannot live in any git configuration. Instead a small ``git``
stand-in is placed first on ``PATH``: for the commands that would use the
network (``fetch``, ``push``, ``ls-remote``) it adds one rewrite rule sending
the fork URL to the bare repository, allows the local-file protocol only, and
runs the real git. Every other git command passes through untouched.

``gh`` and ``codex`` are small scripts placed first on ``PATH`` that answer
from a JSON file and record every call.

Model slugs below are fixtures. They are deliberately not the template's own
model: a bump of the real manifest must not change what these tests prove.
"""

from __future__ import annotations

import atexit
import functools
import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MAINT_DIR = REPO_ROOT / "scripts" / "maint"
SHARED_REL = ".claude/skills/_shared"
MANIFEST_REL = f"{SHARED_REL}/models.json"
CONFIG_REL = ".codex/config.toml"
HOOK_REL = ".claude/hooks/example-hook.py"

FAMILY = "astra"
OTHER_FAMILY = "sol"
CURRENT = f"gpt-7.1-{FAMILY}"
NEWER = f"gpt-7.2-{FAMILY}"
EFFORT = "xhigh"
SHA_LENGTH = 40
REAL_GIT = shutil.which("git") or "git"
TERMINAL_TIMEOUT_SEC = 90

# A stand-in for scripts/update.sh: the three lists and the literal template
# sources the promotion rule reads. `{seed}` is where the state seed is copied
# from, which is the one line that differs between the two layouts tested.
UPDATER_TEMPLATE = """#!/usr/bin/env bash
SAFE_DIRS=(
    ".claude/rules"
    ".claude/skills"
    ".claude/agents"
    ".claude/hooks"
)
# Files that no SAFE_DIR already covers.
SAFE_FILES=(
    "AGENTS.md"
    "scripts/check.sh"
)
SETTINGS_FILES=(
    ".claude/settings.json"
    ".codex/config.toml"
)
cp -f "${{TEMPLATE_DIR}}/{seed}" "${{PROJECT_ROOT}}/.claude/STATE.md"
cp -f "${{TEMPLATE_DIR}}/VERSION" "${{PROJECT_ROOT}}/.claude/orchestra-version"
"""
SEED_IN_PLACE = ".claude/STATE.md"
SEED_IN_SCAFFOLD = "scripts/scaffold/STATE.md"

if str(MAINT_DIR) not in sys.path:
    sys.path.insert(0, str(MAINT_DIR))

import bump  # noqa: E402
import common  # noqa: E402
import gh  # noqa: E402
import model_watch  # noqa: E402
import promote  # noqa: E402
import report  # noqa: E402
import run as maint_run  # noqa: E402

MODULES: tuple[ModuleType, ...] = (
    bump,
    common,
    gh,
    promote,
    model_watch,
    report,
    maint_run,
)

# The git stand-in. `{real}` is the real git, `{bare}` the scratch fork.
# A file named `<this script>.stall` holding one word makes any command with
# that word as an argument hang, for the deadline tests; `<this script>.fail`
# makes it fail.
FAKE_GIT = """#!/bin/sh
GIT_ALLOW_PROTOCOL=file
export GIT_ALLOW_PROTOCOL
stall=""
[ -f "$0.stall" ] && stall="$(cat "$0.stall")"
fail=""
[ -f "$0.fail" ] && fail="$(cat "$0.fail")"
network=no
for arg in "$@"; do
    if [ -n "$fail" ] && [ "$arg" = "$fail" ]; then
        echo "fake git: forced failure of $arg" >&2
        exit 1
    fi
    if [ -n "$stall" ] && [ "$arg" = "$stall" ]; then
        sleep 20 > /dev/null 2>&1
    fi
    case "$arg" in
        fetch | push | ls-remote) network=yes ;;
        --get-url)
            network=no
            break
            ;;
    esac
done
if [ "$network" = yes ]; then
    exec "{real}" -c "url.{bare}.insteadOf={url}" "$@"
fi
exec "{real}" "$@"
"""

FAKE_GH = r"""
import json, os, subprocess, sys, time

FORK = "ToRainu256/claude-code-orchestra"
path = os.environ["FAKE_GH_STATE"]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
args = sys.argv[1:]
state["calls"].append(args)


def finish(code, out=""):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    sys.stdout.write(out)
    sys.exit(code)


def option(name):
    return args[args.index(name) + 1] if name in args else None


group = " ".join(args[:2])
named = option("--repo") == "github.com/" + FORK
if args[0] == "api":
    target = args[3] if len(args) > 3 else ""
    named = args[1:3] == ["--hostname", "github.com"] and (
        target == "user" or target.startswith("repos/" + FORK + "/")
    )
if not named:
    state["violations"].append(args)
    finish(97)
if group in state["stall"]:
    time.sleep(60)
if group in state["fail"]:
    sys.stderr.write("fake gh: forced failure\n")
    finish(1)

if args[0] == "api" and args[3] == "user":
    finish(0, state["login"] + "\n")
if group == "pr list":
    # Like the real CLI: open pull requests only unless --state says more.
    wanted = (option("--state") or "open").lower()
    found = [
        p for p in state["pulls"]
        if p["headRefName"] == option("--head")
        and (wanted == "all" or p["state"].lower() == wanted)
        and option("--base") in (None, p["baseRefName"])
        and option("--author") in (None, p["author"]["login"])
    ]
    finish(0, json.dumps(found))
if group == "pr view":
    found = [p for p in state["pulls"] if str(p["number"]) == args[4]]
    if not found:
        sys.stderr.write("fake gh: no such pull request\n")
        finish(1)
    finish(0, json.dumps(found[0]))
if group == "pr create":
    number = state["next_number"]
    state["next_number"] += 1
    head = "f" * 40
    if state.get("bare"):
        listed = subprocess.run(
            ["git", "-C", state["bare"], "rev-parse", "refs/heads/" + option("--head")],
            capture_output=True, text=True,
        )
        head = listed.stdout.strip() or head
    url = state["pr_url_base"] + "/pull/" + str(number)
    state["pulls"].append({
        "number": number, "state": "OPEN", "url": url, "headRefOid": head,
        "headRefName": option("--head"), "baseRefName": option("--base"),
        "author": {"login": state["login"]}, "isCrossRepository": False,
        "mergeCommit": None,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "title": option("--title"), "body": option("--body"),
    })
    finish(0, "Creating pull request\n" + url + "\n")
if group == "pr merge":
    if "--disable-auto" in args:
        state["disabled"].append(args)
    else:
        state["merges"].append(args)
    finish(0)
if group == "issue list":
    wanted = option("--state")
    found = [
        i for i in state["issues"]
        if option("--label") in i["labels"]
        and (wanted == "all" or i["state"].lower() == wanted)
    ]
    finish(0, json.dumps(found))
if group == "issue create":
    number = state["next_number"]
    state["next_number"] += 1
    state["issues"].append({
        "number": number, "state": "OPEN", "title": option("--title"),
        "body": option("--body"), "labels": [option("--label")],
    })
    finish(0, "https://github.com/" + FORK + "/issues/" + str(number) + "\n")
if group == "label list":
    finish(0, json.dumps([{"name": name} for name in state["labels"]]))
if group == "label create":
    state["labels"].append(args[4])
    finish(0)
if args[0] == "api":
    body = next((a[len("body="):] for a in args if a.startswith("body=")), None)
    if "PATCH" in args:
        wanted = int(args[3].rsplit("/", 1)[1])
        for comment in state["comments"]:
            if comment["id"] == wanted:
                comment["body"] = body
        finish(0)
    if "POST" in args:
        number = state["next_number"]
        state["next_number"] += 1
        state["comments"].append({"id": number, "body": body, "author": state["login"]})
        finish(0)
    finish(0, "".join(json.dumps(c) + "\n" for c in state["comments"]))
sys.stderr.write("fake gh: unsupported call\n")
finish(98)
"""

FAKE_CODEX = r"""
import json, os, sys, time

path = os.environ["FAKE_CODEX_STATE"]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
args = sys.argv[1:]
state["calls"].append(args)
state["cwds"].append(os.getcwd())
with open(path, "w", encoding="utf-8") as handle:
    json.dump(state, handle)

if args == ["--version"]:
    print("codex-cli 0.0.0-fake")
    sys.exit(state["version_exit"])
if args == ["debug", "models"]:
    time.sleep(state["catalog_sleep"])
    sys.stdout.write(state["catalog_text"])
    sys.exit(state["catalog_exit"])
if args and args[0] == "exec":
    time.sleep(state["exec_sleep"])
    sys.stdout.write(state["smoke_reply"])
    sys.exit(state["exec_exit"])
sys.exit(98)
"""


def git(cwd: Path, *args: str) -> str:
    """Run the real git in a scratch repository and return its stdout."""
    done = subprocess.run(
        [
            REAL_GIT,
            "-c",
            "user.name=Maint Test",
            "-c",
            "user.email=maint@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def entry(
    slug: str, efforts: tuple[str, ...] = (EFFORT,), visibility: str = "list"
) -> dict:
    """One catalog entry, shaped like the CLI's own."""
    return {
        "slug": slug,
        "visibility": visibility,
        "priority": 1,
        "supported_reasoning_levels": [{"effort": effort} for effort in efforts],
    }


def catalog(*entries: dict) -> dict:
    return {"models": list(entries)}


def write_script(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def write_fake_git(path: Path, bare: Path) -> None:
    """The git stand-in that sends the fork URL to ``bare``."""
    write_script(
        path, FAKE_GIT.format(real=REAL_GIT, bare=bare, url=common.FORK_GIT_URL)
    )


def run_with_terminal(
    argv: list[str], env: dict[str, str], answers: str = "", cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command whose stdin is a terminal, with ``answers`` typed into
    it. The approvals under test refuse anything that is not one."""
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=None if cwd is None else str(cwd),
            text=True,
        )
        os.close(slave)
        slave = -1
        # The answers, then end-of-input (Ctrl-D): a prompt nobody answered
        # reads nothing instead of waiting for ever.
        os.write(master, answers.encode("utf-8") + b"\x04")
        try:
            out, err = process.communicate(timeout=TERMINAL_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            raise AssertionError(f"still running at a terminal: {out}{err}") from None
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


@dataclass
class Fork:
    """One scratch world: the bare fork, a seed work tree to push from, the
    job's state root with its clone, and the fakes."""

    root: Path
    bare: Path
    seed: Path
    paths: Any
    bin_dir: Path
    gh_state_file: Path
    codex_state_file: Path
    env: dict[str, str]
    log_file: Path

    # -- fakes ---------------------------------------------------------------
    def gh_state(self) -> dict:
        return json.loads(self.gh_state_file.read_text(encoding="utf-8"))

    def set_gh(self, **changes: Any) -> None:
        self.gh_state_file.write_text(
            json.dumps({**self.gh_state(), **changes}), encoding="utf-8"
        )

    def codex_state(self) -> dict:
        return json.loads(self.codex_state_file.read_text(encoding="utf-8"))

    def set_codex(self, **changes: Any) -> None:
        self.codex_state_file.write_text(
            json.dumps({**self.codex_state(), **changes}), encoding="utf-8"
        )

    def set_catalog(self, *entries: dict) -> None:
        self.set_codex(catalog_text=json.dumps(catalog(*entries)))

    def gh_calls(self, *prefix: str) -> list[list[str]]:
        return [
            call
            for call in self.gh_state()["calls"]
            if call[: len(prefix)] == list(prefix)
        ]

    def codex_calls(self, first: str) -> list[list[str]]:
        return [call for call in self.codex_state()["calls"] if call[:1] == [first]]

    def fail_git(self, word: str) -> None:
        """Make every git command with ``word`` as an argument fail."""
        (self.bin_dir / "git.fail").write_text(word, encoding="utf-8")

    def stall_git(self, word: str | None) -> None:
        """Make every git command with ``word`` as an argument hang."""
        marker = self.bin_dir / "git.stall"
        if word is None:
            marker.unlink()
            return
        marker.write_text(word, encoding="utf-8")

    # -- repositories --------------------------------------------------------
    def commit(self, files: dict[str, str | None], message: str) -> str:
        """Change files on the fork's default branch and return the new SHA."""
        for rel, text in files.items():
            target = self.seed / rel
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "--quiet", "-m", message)
        git(self.seed, "push", "--quiet", "origin", "HEAD:main")
        return git(self.seed, "rev-parse", "HEAD")

    def bump(self, slug: str, message: str = "") -> str:
        """Bump the fork's default branch by hand with the real manifest tool:
        a bump merged by some other means than this Mac's model watch."""
        for args in (["set", "codex.model", slug], ["apply"]):
            run_models(self.seed, args)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "--quiet", "-m", message or f"bump to {slug}")
        git(self.seed, "push", "--quiet", "origin", "HEAD:main")
        return git(self.seed, "rev-parse", "HEAD")

    def squash_merge(self, branch: str, by_pull_request: bool = True) -> str:
        """One new commit on the default branch with the branch's changes.

        With ``by_pull_request`` this is GitHub merging the open pull request
        of that branch, which then says so. Without, the same content reaches
        the default branch by some other means and no pull request changes.
        """
        git(self.seed, "checkout", "--quiet", "main")
        git(self.seed, "fetch", "--quiet", "origin", branch)
        git(self.seed, "merge", "--quiet", "--squash", "FETCH_HEAD")
        git(self.seed, "commit", "--quiet", "-m", f"merge {branch} (#10)")
        git(self.seed, "push", "--quiet", "origin", "HEAD:main")
        merged = git(self.seed, "rev-parse", "HEAD")
        pulls = self.gh_state()["pulls"]
        mine = [p for p in pulls if p["headRefName"] == branch and p["state"] == "OPEN"]
        if by_pull_request and mine:
            mine[0].update(state="MERGED", mergeCommit={"oid": merged})
            self.set_gh(pulls=pulls)
        return merged

    def fetch(self) -> str:
        """What the launcher does before every run: fetch exactly the default
        branch, by its full name, without tags."""
        git(
            self.paths.clone,
            "-c",
            f"url.{self.bare}.insteadOf={common.FORK_GIT_URL}",
            "fetch",
            "--quiet",
            "--no-tags",
            "--prune",
            "origin",
            f"+refs/heads/main:{common.MAIN_REF}",
        )
        return git(self.paths.clone, "rev-parse", common.MAIN_REF)

    def approve(self, sha: str) -> None:
        promote.write_approved(self.paths, sha)

    def approved(self) -> str | None:
        return promote.read_approved(self.paths)

    def remote_branches(self) -> list[str]:
        listed = git(
            self.bare, "for-each-ref", "--format=%(refname:short)", "refs/heads"
        )
        return sorted(listed.split())

    def changed_on(self, branch: str) -> list[str]:
        return sorted(git(self.bare, "diff", "--name-only", "main", branch).split())

    def context(self, now: float | None = None, tools_root: Path = REPO_ROOT) -> Any:
        return common.make_context(
            self.paths, tools_root, common.Log(self.log_file), env=self.env, now=now
        )


def run_models(root: Path, args: list[str]) -> dict:
    """The repository's own manifest tool, pointed at a scratch tree."""
    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / SHARED_REL / "models.py"),
            "--project-root",
            str(root),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"models.py {args}: {done.stderr}"
    return json.loads(done.stdout)


def _seed_tree(seed: Path, with_code: bool, updater_seed: str) -> None:
    """A minimal template: the real model locations, moved to a fixture slug."""
    for rel in (MANIFEST_REL, ".claude/settings.json", CONFIG_REL):
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, seed / rel)
    shutil.copytree(REPO_ROOT / ".claude" / "agents", seed / ".claude" / "agents")
    (seed / "scripts").mkdir()
    (seed / "scripts" / "update.sh").write_text(
        UPDATER_TEMPLATE.format(seed=updater_seed), encoding="utf-8"
    )
    (seed / "VERSION").write_text("0.0.1\n", encoding="utf-8")
    (seed / "README.md").write_text("# scratch fork\n", encoding="utf-8")
    (seed / ".claude" / "STATE.md").write_text("# Agent State\n", encoding="utf-8")
    (seed / HOOK_REL).parent.mkdir(parents=True, exist_ok=True)
    (seed / HOOK_REL).write_text("print('hook')\n", encoding="utf-8")
    if with_code:
        shutil.copytree(
            MAINT_DIR,
            seed / "scripts" / "maint",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        for name in ("models.py", "codex_consult.py", "edit_provenance.py"):
            shutil.copy2(REPO_ROOT / SHARED_REL / name, seed / SHARED_REL / name)
    run_models(seed, ["set", "codex.model", CURRENT])
    run_models(seed, ["set", "codex.reasoning_effort", EFFORT])
    run_models(seed, ["apply"])
    run_models(seed, ["check", "--strict"])


def init_job_clone(clone: Path) -> None:
    """Create the job's repository the way `launchd.sh install` does: no
    checkout, the fork as the only remote, the default branch as the only
    fetched ref, no tags, and the ownership marker."""
    subprocess.run([REAL_GIT, "init", "--quiet", str(clone)], check=True)
    git(clone, "remote", "add", "origin", common.FORK_GIT_URL)
    git(clone, "config", "remote.origin.fetch", f"+refs/heads/main:{common.MAIN_REF}")
    git(clone, "config", "remote.origin.tagOpt", "--no-tags")
    git(clone, "config", common.OWNERSHIP_KEY, common.OWNERSHIP_VALUE)


@functools.cache
def _template_world(with_code: bool, updater_seed: str) -> Path:
    """The three repositories of a scratch world, built once per layout."""
    root = Path(tempfile.mkdtemp(prefix="maint-world-")) / "world"
    atexit.register(shutil.rmtree, str(root.parent), ignore_errors=True)
    bare = root / "fork.git"
    seed = root / "seed"
    clone = root / "state-root" / common.CLONE_DIR
    for directory in (bare, seed, clone.parent):
        directory.mkdir(parents=True)
    subprocess.run([REAL_GIT, "init", "--quiet", "--bare", str(bare)], check=True)
    git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    subprocess.run([REAL_GIT, "init", "--quiet", str(seed)], check=True)
    git(seed, "checkout", "--quiet", "-b", "main")
    git(seed, "remote", "add", "origin", str(bare))
    _seed_tree(seed, with_code, updater_seed)
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "seed")
    git(seed, "push", "--quiet", "origin", "HEAD:main")
    init_job_clone(clone)
    return root


def make_fork(
    tmp_path: Path, with_code: bool = False, updater_seed: str = SEED_IN_SCAFFOLD
) -> Fork:
    """Build the scratch world. The clone is approved at the fork's first
    commit, and the catalog lists the current slug only."""
    root = tmp_path / "world"
    bare = root / "fork.git"
    seed = root / "seed"
    state_root = root / "state-root"
    bin_dir = root / "bin"
    # Building the repositories costs a dozen processes, so each layout is
    # built once per session and copied; only the absolute paths are rewired.
    shutil.copytree(_template_world(with_code, updater_seed), root, symlinks=True)
    bin_dir.mkdir()
    git(seed, "remote", "set-url", "origin", str(bare))
    paths = common.Paths(state_root)

    gh_state_file = root / "gh-state.json"
    gh_state_file.write_text(
        json.dumps(
            {
                "login": common.FORK_OWNER,
                "pulls": [],
                "issues": [],
                "labels": [],
                "comments": [],
                "merges": [],
                "disabled": [],
                "calls": [],
                "violations": [],
                "fail": [],
                "stall": [],
                "next_number": 10,
                "pr_url_base": common.FORK_WEB_URL,
                "bare": str(bare),
            }
        ),
        encoding="utf-8",
    )
    codex_state_file = root / "codex-state.json"
    codex_state_file.write_text(
        json.dumps(
            {
                "calls": [],
                "cwds": [],
                "catalog_text": json.dumps(catalog(entry(CURRENT))),
                "catalog_sleep": 0,
                "catalog_exit": 0,
                "version_exit": 0,
                "smoke_reply": "OK\n",
                "exec_sleep": 0,
                "exec_exit": 0,
            }
        ),
        encoding="utf-8",
    )
    write_fake_git(bin_dir / "git", bare)
    write_script(bin_dir / "gh", f"#!{sys.executable}\n{FAKE_GH}")
    write_script(bin_dir / "codex", f"#!{sys.executable}\n{FAKE_CODEX}")
    write_script(bin_dir / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_STATE": str(gh_state_file),
        "FAKE_CODEX_STATE": str(codex_state_file),
        "HOME": str(root / "home"),
        common.STATE_ROOT_ENV: str(state_root),
    }
    env.pop("CODEX_MODEL", None)
    (root / "home").mkdir()
    # The fakes must be what the code under test finds, never the real tools.
    for tool in ("git", "gh", "codex"):
        assert shutil.which(tool, path=env["PATH"]) == str(bin_dir / tool)

    fork = Fork(
        root=root,
        bare=bare,
        seed=seed,
        paths=paths,
        bin_dir=bin_dir,
        gh_state_file=gh_state_file,
        codex_state_file=codex_state_file,
        env=env,
        log_file=root / "test.log",
    )
    fork.approve(fork.fetch())
    return fork


def assert_fork_only(fork: Fork) -> None:
    """Every recorded ``gh`` call named the fork, and none named anything else."""
    state = fork.gh_state()
    assert state["violations"] == []
    for call in state["calls"]:
        if call[0] == "api":
            assert call[1:3] == ["--hostname", common.GITHUB_HOST], call
            assert call[3] == "user" or call[3].startswith(
                f"repos/{common.FORK_SLUG}/"
            ), call
            continue
        assert call[2:4] == ["--repo", common.FORK_REPO], call


def never_merged_commit(fork: Fork, files: dict[str, str]) -> str:
    """A commit that exists on the fork but was never on its default branch."""
    git(fork.seed, "checkout", "--quiet", "-b", "side")
    for rel, text in files.items():
        (fork.seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (fork.seed / rel).write_text(text, encoding="utf-8")
    git(fork.seed, "add", "-A")
    git(fork.seed, "commit", "--quiet", "-m", "never merged, never ran CI")
    side = git(fork.seed, "rev-parse", "HEAD")
    git(fork.seed, "checkout", "--quiet", "main")
    return side


def plant_shadow_tag(fork: Fork, commit: str, in_clone: bool) -> None:
    """A tag named `origin/main` on the fork: the short name `origin/main`
    resolves to it before the branch. Optionally forced into the job's clone."""
    git(fork.seed, "push", "--quiet", "origin", f"{commit}:refs/tags/origin/main")
    if in_clone:
        refspec = "refs/tags/origin/main:refs/tags/origin/main"
        git(fork.paths.clone, "fetch", "--quiet", str(fork.bare), refspec)
