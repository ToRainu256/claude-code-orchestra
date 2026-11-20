# Local maintenance job (fork only)

Operator tooling for the fork `ToRainu256/claude-code-orchestra`. It is not
part of the template: `scripts/install.sh` and `scripts/update.sh` never copy
`scripts/maint/` into a project.

Nothing here runs until the owner runs `bash scripts/maint/launchd.sh install`
on the Mac. Read `bash scripts/maint/launchd.sh print` first: it shows the
exact plist and every path, and changes nothing.

## Trust model

Merging and promoting are separate.

- Green pull requests merge to `main` on their own.
- The Mac executes only the **approved revision**: one commit SHA in
  `~/.claude/orchestra/state/approved-rev`.
- The launcher (`orchestra-maint`) is a copy that lives outside the tree it
  checks out. On every run it fetches the default branch, checks out the
  approved SHA into a clean tree, and runs that revision's
  `scripts/maint/run.py`. Code from a revision that was not promoted is never
  executed here; its files are only read as data (`git show`, a scratch
  worktree).
- **The default branch is always `refs/remotes/origin/main`, by its full
  name.** A short name such as `origin/main` is resolved through tags first,
  so a tag of that name would stand in for the branch. The clone is created
  with no tags, every fetch is `--no-tags` with an explicit refspec, and the
  job refuses to run when the clone holds any ref other than the branch
  `approved` and the fork's remote-tracking refs.
- All git work happens in the dedicated clone `~/.claude/orchestra/fork` and
  in scratch worktrees under `~/.claude/orchestra/run/`. The clone is marked
  as the job's own at install (`orchestra.maintclone` in its configuration);
  the launcher and `run.py` require the mark, the fork as the only place it
  fetches from and pushes to, and real directories (no links) for everything
  the job manages. `run.py` refuses to start from any other checkout, and
  every git call refuses any other directory. The developer checkout is never
  a target.
- **git runs without the user's configuration.** Every git process, in the
  Python code and in both shell files, gets no inherited `GIT_*` variable, no
  global or system configuration (`GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_NOSYSTEM=1`), and `core.hooksPath=/dev/null`,
  `core.fsmonitor=false`, `core.attributesFile=/dev/null` on its command line.
  A hook manager, a filter driver, a URL rewrite or an include in the owner's
  own git setup therefore neither runs nor redirects anything here.
  Credentials are not read from that configuration either: a push asks
  `gh auth git-credential` for the login `gh` already holds, passed as the
  credential helper for `https://github.com` on the command line. Commits on
  the bump branch carry a fixed bot identity.
- Every `gh` call names the fork with its host (`--repo github.com/<fork>`, or
  `--hostname github.com` and a `repos/<fork>/…` REST path), built in two
  functions in `gh.py` from one constant; `GH_HOST` and `GH_REPO` are not
  passed on. Nothing can address the upstream repository.
- **Text from outside is never printed raw.** Commit subjects, path names and
  command output are shown with every character that is not printable ASCII
  replaced by a visible escape (`\x1b`, `‮`; `?` in the shell files), so
  nothing merged into the fork can redraw the screen above a prompt.
- **Approvals are typed at a terminal.** `install` and `promote` refuse a
  piped answer.
- No token is stored anywhere. The plist carries a `PATH`, a locale and one
  Python flag.

## What gets installed

| Path | Content | Created by |
|------|---------|------------|
| `~/Library/LaunchAgents/io.github.torainu256.orchestra-maint.plist` | The LaunchAgent (`orchestra-maint.plist.in`, rendered) | `install` |
| `~/.claude/orchestra/bin/orchestra-maint` | Copy of `scripts/maint/orchestra-maint`, mode 700 | `install` |
| `~/.claude/orchestra/fork/` | Repository of the fork: `origin` only, the default branch only, no tags, nothing checked out until the first run | `install` |
| `~/.claude/orchestra/state/approved-rev` | The approved SHA, shown for review and confirmed with a typed `yes` | `install` |
| `~/.claude/orchestra/logs/maint/<UTC stamp>.log` | One log per run, newest 60 kept | each run |
| `~/.claude/orchestra/logs/launchd.{out,err}.log` | What launchd captures | launchd |
| `~/.claude/orchestra/state/last-run.json` | Summary that `status` prints | each run |
| `~/.claude/orchestra/state/last-good-rev` | Last revision the launcher could check out | launcher |
| `~/.claude/orchestra/state/bump-records.json` | The model bumps this Mac built and tested | `model-watch` |
| `~/.claude/orchestra/state/launcher-failure` | Present only while the launcher is failing | launcher |
| `~/.claude/orchestra/run/` | `maint.lock`, scratch worktrees (removed after use) | each run |

The job itself writes nothing into the developer checkout, `~/.codex`,
`~/.config/gh` or a shell profile. It does run the `gh` and `codex` programs,
and those keep their own state where they always do (the Codex CLI under
`~/.codex`). Files the job creates are private (umask 077).

**Schedule:** 08:40 and 20:40 local time. `RunAtLoad` is false, so loading the
agent runs nothing. If the Mac is asleep, launchd runs the job once at the
next wake. The launcher makes three attempts at the fetch, 20 seconds apart.

## What a run can modify

| Step | On the Mac | On GitHub (fork only) |
|------|-----------|------------------------|
| launcher | The clone: fetch of the default branch, forced checkout of the approved SHA (or, for `promote` and `status` only, of the last one that could be checked out), `git clean`; `last-good-rev`, `launcher-failure` | — |
| `promote` | `state/approved-rev`, in the two automatic cases only | — |
| `model-watch` | A scratch worktree and scratch directories under `run/`; `bump-records.json` | Branch `bot/models-<slug>`, one pull request with auto-merge (or auto-merge withdrawn from one whose branch changed), `automation:needs-owner` issues, that label when missing |
| `report` | — | Its own comment in the open issue labeled `automation:status`; that label when missing |
| every run | Its log, `last-run.json`, the lock | — |

A later distribution step is added as one more entry in `STEPS` in `run.py`.

## Promotion

`promote` compares the tree at the approved revision with the tree at the
default branch (the net change, not commit by commit: the Mac only ever runs
the tree at the end of the range).

**Never by itself: anything that is not a plain file.** If the range adds or
changes an entry whose mode is not 100644 or 100755 (a link, 120000; another
repository, 160000), wherever it is, the range waits and the result names the
path and the mode. Such an entry can make the tree impossible to check out.

**Never by itself: a path this Mac cannot hold.** If the range adds or
changes a path with a name longer than 255 bytes or a total length over 1023
bytes (UTF-8; macOS's 1024-byte `PATH_MAX` counts the terminating NUL;
`NAME_MAX_BYTES` and `PATH_MAX_BYTES` in `bump.py`), the range
waits and the result names the path, made safe to show and cut short.

Otherwise it advances `approved-rev` by itself in exactly two cases:

1. **Inert range.** No changed path is one the Mac executes or ships. A path
   counts as executed or shipped when it is, lies below, or is a parent of:
   `scripts`, `VERSION`, `.claude/skills`, `.claude/settings.json`,
   `.claude/models.local.json` (the override file the approved manifest tool
   reads), `.codex/config.toml`, any entry of `SAFE_DIRS`, `SAFE_FILES` or
   `SETTINGS_FILES` in the **approved** `scripts/update.sh`, or any literal
   `${TEMPLATE_DIR}/…` source in that file. A `.gitattributes`, `.gitmodules`
   or `.lfsconfig` in any directory also counts: git reads them at checkout,
   and they decide what bytes the executed and shipped files get. Names are
   compared without regard to case, and a name that is not plain ASCII is
   never inert. If the approved updater cannot be parsed, no range is inert.
2. **This Mac's own model bump.** All of:
   - the executed-or-shipped changes are exactly
     `.claude/skills/_shared/models.json` and `.codex/config.toml`, both
     regular files with mode 100644 before and after (a link or a mode change
     is not a bump);
   - the manifest differs only in `codex.model` (same vendor and family,
     strictly newer version) and that key's `superseded` list;
   - the configuration file differs only in the value of its top-level
     `model` line, compared byte for byte (a carriage return anywhere is a
     change; a multi-line string above the line is refused, not tokenized);
   - **it is the merge of the pull request this Mac's model watch opened
     for it.** `state/bump-records.json` holds the slug, the commit, the two
     file objects and the pull-request number. The two files must be those
     objects; GitHub must report that pull request as merged, from that
     commit; and its merge commit must lie inside the range.

   A record is used once: it is dropped when the bump is promoted (by the job
   or by the owner) and when its pull request is closed without merging. A
   bump merged by any other means, however well-formed, and the same two
   files arriving again later, wait for the owner. If GitHub cannot be asked,
   the bump waits until the next run.

Everything else waits. There is no setting that turns the gate off.

`bash scripts/maint/launchd.sh promote` is the owner's side. It shows the
commits with a link to each (built from the commit's SHA, never from a number
in its subject), the changed paths, every entry that is not a plain file, the
executed-or-shipped paths that kept it waiting (or that the next run would
have promoted it anyway), every path too long to check out on this Mac, the
command that prints the full diff, and asks for
a typed `yes`. It needs a terminal. When the default branch was rewritten
there is no range, so it shows the newest commits and the same comparison of
the approved tree with the new one; if the two cannot be compared it does not
ask.

After an automatic promotion the run restarts once through the launcher, so
the remaining steps execute the revision that was just approved.

## Model watch

Candidate selection is a pure function, `pick_candidate`: among the visible
catalog entries, the highest version of the current slug's own vendor and
family that is strictly newer than the current one (`6.10` is newer than
`6.9`) and supports the configured effort. Never a lower version, never
another family, never an older slug in place of a newest one that lacks the
effort. Vendor and family are read from the manifest's current value; no
model name is written in this directory. The catalog is untrusted: a slug is
used only after it matches a strict pattern, and no command goes through a
shell. A catalog that lists no usable slug at all is a failed fetch, not a
retired model.

What the default branch names now (model and effort) is read from the
manifest file at that commit itself, not through the manifest tool: a project
override file (`.claude/models.local.json`) merged on the fork must not decide
it. While such a file is on the default branch, under that name in any letter
case (macOS checks `.Claude/Models.Local.JSON` out as the same file), the step
does nothing (`override-in-tree`).

The bump is always built here, with the **approved** tools pointed at a
scratch worktree of the default branch, in this order, and nothing leaves the
Mac before the last step:

1. `models.py set`, `apply`, `check --strict`;
2. one local commit of whatever that changed;
3. the bump-shape check on that commit (the same function the promotion rule
   uses: exactly the two files, regular, only the model value);
4. one live call through `codex_consult.py` naming the new slug and effort,
   whose reply must be exactly `OK`;
5. the shape check again, against the default branch as it is right then;
6. the record in `state/bump-records.json`;
7. the push, the pull request (its number goes into the record), and
   auto-merge (`--auto --squash`, pinned to that commit).

The repository's full gates run as the pull request's required checks, not
on the Mac.

What is already on GitHub decides where a run continues, and is measured
against what this run built (steps 1 to 3 are done first in every case). The
job never vouches for a bump someone else prepared: a branch counts only when
its two files are byte for byte the ones built here.

- The owner's pull requests from `bot/models-<slug>` into `main` are looked
  up in every state. Merged: done. Closed without merging: the owner said
  no, it is never opened again, and its record is dropped.
- Open: the branch is fetched and must still be one commit that is exactly
  the bump built here. Then auto-merge is asked for again, pinned to that
  commit. If the branch is anything else or gone, auto-merge is **withdrawn**
  and the outcome is `unknown-branch`. A record left without a pull-request
  number (a run stopped right after opening it) gets this pull request's
  number, but only when GitHub names exactly the recorded commit as its head.
- No pull request but the branch exists: it must be exactly the bump built
  here (a previous run stopped after its push); then the live call is made
  again, the record is written and the pull request is opened. A branch with
  the right shape but other bytes is `unknown-branch`: no live call, no
  record, no pull request.

Reruns are safe: a second run never opens a second pull request or a second
issue.

## Outcomes

Each step reports one outcome word and an exit code; a run exits with the
highest. The words and codes below are exactly what the code can report (a
test compares these tables with the lists in the code).

- **Exit 3** means a tool, the network or a login failed. The next run tries
  again; act only if the same result repeats. The reason is in the newest
  file under `~/.claude/orchestra/logs/maint/`.
- **Exit 2** means the job is waiting for the owner. Only `current-not-listed`
  and `effort-unsupported` also open an `automation:needs-owner` issue; for
  every other exit 2 the status comment and `launchd.sh status` are where it
  shows, and the "what to do" column says what it is waiting for.

### promote

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `up-to-date` | 0 | The approved revision is the default branch | — |
| `promoted` | 0 | Advanced by one of the two automatic cases | — |
| `awaiting-promotion` | 0 / 2 | Merged changes wait for approval: they touch what the Mac executes or ships, add or change an entry that is not a plain file or a path too long for this Mac, or are a bump this Mac's record does not cover. 2 once the oldest one landed on `main` more than 7 days ago (a merge counts once, at the time it was merged) | Review and run `launchd.sh promote` |
| `diverged` | 2 | The approved revision is no longer in the default branch's history | Run `launchd.sh promote`; it shows the new history |
| `no-approved-revision` | 1 | `state/approved-rev` is missing or not a SHA (the launcher normally stops before this) | Run `launchd.sh install` |
| `git-failed` | 3 | The range could not be read, or the branch moved mid-run | Retried |

### model-watch

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `up-to-date` | 0 | No newer slug in the family | — |
| `pr-opened` | 0 | Bump pushed, pull request open, auto-merge on | — |
| `pending` | 0 / 2 | The bump pull request is open and still exactly the bump; 2 once it is older than 2 days | Open it and read its failing check |
| `already-merged` | 0 | A merged bump pull request for that slug exists | — |
| `rejected-by-owner` | 0 | It was closed without merging; never reopened | — |
| `current-not-listed` | 0 / 2 | The current slug is not in the catalog; one issue is opened. 0 once that issue is closed | Decide, then close the issue |
| `effort-unsupported` | 0 / 2 | The newest slug lacks the configured effort; one issue is opened. 0 once that issue is closed | Decide, then close the issue |
| `current-unparseable` | 2 | The current slug is not `<vendor>-<version>-<family>`; the watch cannot tell what "newer" is | Bump by hand when you want to |
| `manifest-unreadable` | 2 | The manifest file on `main` is not a regular file, not JSON, or names a model or effort of an unexpected shape | Fix the manifest on `main` |
| `override-in-tree` | 2 | `main` carries `.claude/models.local.json` (in any letter case), a project's override file; the watch does nothing while it is there | Remove the file from the fork |
| `unknown-branch` | 2 | `bot/models-<slug>` exists and is not exactly the bump this Mac builds: other files, another slug, or the right shape with other bytes (or an open pull request's branch is gone); auto-merge was withdrawn from an open pull request | Inspect the branch; delete it or close its pull request |
| `auto-merge-not-enabled` | 2 | The pull request is open but GitHub refused auto-merge; every later run asks again | If it repeats, enable "Allow auto-merge" on the fork |
| `auth-or-tool` | 3 | `gh` is not logged in as the fork owner, or the Codex CLI does not start | `gh auth login`, or fix the CLI |
| `catalog-fetch-failed` | 3 | The catalog command failed, exceeded 120 s, or listed no usable slug | Retried |
| `apply-failed` | 3 | `set`, `apply` or `check --strict` failed or timed out; nothing pushed | Retried |
| `unexpected-diff` | 3 | The bump commit touches another file or value, or `main` moved before the push; nothing pushed | Retried |
| `smoke-failed` | 3 | The live call failed, timed out, or replied with other text; nothing pushed | Retried |
| `push-failed` | 3 | The push was rejected; no pull request | Retried |
| `pr-create-failed` | 3 | `gh pr create` failed; the next run resumes from the branch | Retried |
| `pr-url-outside-fork` | 3 | The created pull request's URL is not under the fork; auto-merge is not requested | Look at what `gh` created |
| `notice-failed` | 3 | An issue the owner must see could not be filed | Retried |
| `git-failed` | 3 | A git command failed or the fork could not be reached | Retried |
| `github-failed` | 3 | Pull requests could not be listed | Retried |

A newer model of **another** family opens one informational issue per
(family, version) and never changes the outcome. If that issue cannot be
filed the outcome stays what it was and the step's counts show
`notice-failed 1`.

### report

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `posted` | 0 | The status comment was replaced | — |
| `no-status-issue` | 0 | No open issue is labeled `automation:status` | Create it (see Health) |
| `auth-or-tool` | 3 | `gh` is not logged in as the fork owner; nothing was posted | `gh auth login` |
| `github-failed` | 3 | Issues or comments could not be read or written | Retried |

### any step, and the launcher

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `refused` | 1 | A step found the clone is not the job's own (a link, a foreign ref, another remote) | Read the message in the log; reinstall if needed |
| `crashed` | 3 | A step raised an unexpected error; the other steps still ran | Read the newest log |
| `checkout-failed` | 3 | Reported for the step `launcher`: the approved revision could not be checked out. Nothing is run on an older revision; the launcher normally stops before this is written | Merge a fix, then `launchd.sh promote` |

## The one command

```bash
bash scripts/maint/launchd.sh print       # the rendered plist and every path; changes nothing
bash scripts/maint/launchd.sh install     # show the plan, probe tools and login, ask; create the repository, show the revision for review, ask; copy the launcher, lint and load the agent
bash scripts/maint/launchd.sh status      # launcher failure, loaded?, plist and launcher drift, tool and login probes, approved vs main, last run per step with its age
bash scripts/maint/launchd.sh promote     # show the waiting range, ask, advance the approved revision
bash scripts/maint/launchd.sh run-now     # start one run now through launchd
bash scripts/maint/launchd.sh uninstall   # unload the agent, remove the plist and the launcher; clone, logs and state are listed, not deleted
```

`install` asks twice, both at a terminal. First for the plan, after every job
tool resolved on the plist's `PATH`, `python3` there is new enough for the
Codex wrapper, and `gh api user` returned the fork owner. Then for the
revision: it shows the full SHA and commit date of the default branch, the
commits and changed paths the fork has that the checkout `launchd.sh` is run
from does not (or the newest ten when that checkout is not in the fork's
history), and the command that prints the full diff. A long list is cut, and
then it says how many lines are not shown and which command shows them all; a
line that is too long is cut and marked `[line cut]`. It is idempotent: a
second run keeps the approved revision, refreshes the launcher, and reloads
the agent only when the plist changed. `print | plutil -lint -` passes.

The launcher is the one piece that does not update itself. `status` says so
when the installed copy differs from `scripts/maint/orchestra-maint`; run
`install` again to refresh it.

## Health

Create one issue titled `Automation status` with the label
`automation:status` and pin it. Each run replaces the job's comment there:
the one that starts with `<!-- job:local-maint -->` **and** was written by
the fork owner. A look-alike comment by anyone else is never edited or
trusted. The comment holds outcome words, exit codes and counts only, never
a path, a project name or a log line.

**The comment's time is the heartbeat.** The job runs twice a day, so a
comment older than about a day (two missed runs, allowing for a Mac that was
asleep) means the job is not running. A run that fails before it can post
leaves the last comment as it was, which is why its age matters and not only
its content. Then look, in this order, at:

1. `bash scripts/maint/launchd.sh status`: it prints `LAUNCHER FAILURE` with
   the reason and the next step when the launcher could not start the run
   (fetch, checkout, missing `python3`), and the age of the last run;
   when the approved revision cannot be checked out, **nothing is run**: the
   job does not fall back to an older revision for a run, because that
   revision would promote and open pull requests by its own, older rules.
   Only `promote` and `status` use the last revision that did check out, so
   the owner can see what waits and approve a fix. When the checkout failed
   on a lock a killed run left in the clone (`.git/index.lock`, or the lock
   git names), the message says so and gives the `rm` command for it instead;
   the job never removes a lock itself, because it may belong to a git
   command that is still running;
2. `~/.claude/orchestra/logs/launchd.err.log`;
3. the newest file in `~/.claude/orchestra/logs/maint/`.

A second run while one holds the lock exits 0 and leaves no trace.

## Exit codes

Every Python script here: `0` ok or nothing to do · `1` usage, configuration,
or a refused working directory · `2` the owner must act · `3` a tool, the
network or authentication failed. `run.py` exits with the highest step code.

The launcher exits `1` when it is not installed, the clone is not the job's
own (or its refs cannot be listed), or there is no approved revision; `3`
when the fork cannot be fetched, the approved revision cannot be checked out
(always for `run`; for `promote` and `status` only when no earlier one can
either), or `python3` is missing (the reason
and the next step go to stderr and to `state/launcher-failure`); otherwise
with `run.py`'s code. `launchd.sh` exits `0` or `1`.

## Files

| File | Role |
|------|------|
| `launchd.sh` | The one command above |
| `orchestra-maint` | The launcher that gets copied |
| `orchestra-maint.plist.in` | Plist template (`@HOME@`, `@LABEL@`, `@PATH@`) |
| `run.py` | Dispatcher: lock, log, promotion, steps, report, `last-run.json` |
| `common.py` | Paths, the git guard and its isolation, subprocess with deadline, safe text |
| `gh.py` | Every `gh` call, with the fork built in |
| `promote.py` | The approved-revision rule and the owner's prompt |
| `bump.py` | What one model bump is, entries that are not plain files or whose path is too long, and the single-use records of the bumps this Mac tested |
| `model_watch.py` | Candidate rule and the resumable bump |
| `report.py` | The public status comment |

Tests: `tests/test_maint_promote.py`, `tests/test_maint_model_watch.py`,
`tests/test_maint_launchd.py`, with fakes in `tests/maint_support.py`. They
use scratch repositories, a fake `gh`, `codex` and `launchctl`, a `git`
stand-in that redirects the fork URL to a scratch repository, and a temp
`HOME`; none reaches the network or the real home directory.

## Upstream intake

`fork_invariants.py` says what makes this repository a fork that still holds,
and prepares the merge of upstream's `main` under those invariants. It is a
helper, run by hand: it is not a step of the local job, and **no routine that
runs it unattended is defined yet**. It never pushes and contacts no remote;
whoever runs it fetches upstream first and pushes the branch afterwards.

```bash
python3 scripts/maint/fork_invariants.py check [--rev SHA]
python3 scripts/maint/fork_invariants.py intake --fork-rev SHA --upstream-rev SHA [--resolve-in DIR]
python3 scripts/maint/fork_invariants.py seal --fork-rev SHA --upstream-rev SHA --resolve-in DIR --resolutions FILE
```

Stdout is one JSON object; findings and conflicts are repeated on stderr.
Every revision is a full commit SHA: a short name is resolved through tags
first.

### The invariants

| Invariant | Holds when | Decided by |
|-----------|-----------|------------|
| `roles-renamed` | No retired role name outside the allow-list | `scripts/rename-roles.py` (check mode) |
| `model-names` | No model name outside the manifest | `models_lint.py` |
| `manifest-applied` | Every rendered model value equals the manifest | `models.py check --strict` |
| `manifest-needs-owner` | That tool does not refuse the tree. It refuses when the manifest names an agent the tree lacks, or the tree has one the manifest does not name. Rendering the manifest again cannot clear this, and the manifest is the owner's: the owner assigns the model | `models.py check --strict`, its `refused:` lines |
| `seeds-pinned` | Each file under `scripts/scaffold/` has the SHA-256 that `tests/test_scaffold.py` pins | Read here, from that test file's `SEED_SHA256` |
| `install-urls` | `scripts/update.sh` assigns `TEMPLATE_REPO` exactly once, whatever the indentation or keyword, and that line is the fork's default byte for byte; no README line clones upstream's default branch or fetches an installer from upstream (`curl`, `wget`, a raw URL) | Read here |
| `no-claude-md` | None of the three files `scripts/check.sh` refuses exists | Read here, the same list |
| `plain-entries` | (`check --rev` only) The commit holds no entry that is not checked out; see below | Read here |

The first four come from the repository's own checkers, run with
`--project-root` on the tree in question. The rest have no checker that can
be aimed at another tree without running that tree's code, so they are read
as data.

### How the merge is prepared

With `T` the fork's mechanical changes (the role rename by the fork's own
codemod, upstream's two seed files moved to `scripts/scaffold/`, the default
install lines pointed at the fork):

1. `T` is applied to the merge base and to upstream's tip, each checked out
   in a scratch repository that reads this repository's objects.
2. `git merge-tree --write-tree --merge-base=T(base) <fork> T(upstream)`
   merges them, with no index and no work tree involved.
3. A clean result that satisfies the invariants becomes one commit with the
   parents `[fork, upstream]` on the local branch `bot/upstream-<12 hex>`.
   The second parent is upstream's real tip, so the next intake starts there.

The fork's own tree must satisfy the invariants first (`fork-not-clean`
otherwise). `T` then changes nothing in it, so every difference between
`T(base)` and the fork is kept as the fork's side and can only meet
upstream's as a conflict. A changed `T` costs conflicts, never content.

A conflict that survives `T` is a real one. It is reported with what git
called it and is never resolved here. `--resolve-in DIR` leaves the automatic
result in `DIR` (a scratch repository: markers included, on top of the fork's
commit, with the branches `fork-main`, `upstream-main`, `merge-base` and
`auto-merge` to read from).

`seal` builds the same two-parent commit from `DIR`. It believes nothing the
directory says about itself: it builds the automatic merge again from the two
commits, and then requires that `--resolutions` names exactly the paths that
merge conflicts in, each with a note; that none of them still holds a marker
line (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`); and that the tree passes
every check a clean merge passes. A marker scan alone is not accepted: a file
deleted on one side, or a binary one, conflicts without a marker. Edits
outside the conflicted paths are accepted, because fixing an invariant needs
them, and every one is reported in `edited_by_hand`.

To check a pull request from outside, run `intake --resolve-in DIR` on the
same two commits. A clean merge gives the same commit SHA as the pull
request's, or the pull request is not the automatic merge. Otherwise
`git -C DIR diff auto-merge <pull request commit>` is what was done by hand.

### What the result carries

Everything in the JSON that came from a tree, a tool or git is already inert:
a path is given with every character outside `A-Za-z0-9._/+@-` as a `\xNN`
escape, and a line of text with backticks, pipes, `#`, `@`, brackets and
link-forming sequences escaped as well. It can be pasted into a shell or
posted as Markdown under the owner's name without acting there.

| Field | On | Holds |
|-------|----|-------|
| `upstream_log` | every outcome past the merge base | Upstream's commits, newest first, at most 100: full SHA and the subject in the inert form. The only form in which a subject should be shown |
| `conflicts` | `intake` | Each conflicted path and what git called the conflict |
| `conflicts_without_transform` | `intake` | How many paths a plain merge conflicts in; a measure, nothing more |
| `findings` | `invariants-failed`, `violated`, `fork-not-clean` | Each failed invariant by name, with the checker's line |
| `sensitive_changes` | `merged` | The paths this merge changes that define the required checks or that a later session executes or loads as instructions. It is the issue worker's stop-list (below), computed, so the list a reviewer reads first is not somebody's search |
| `edited_by_hand` | `seal` | Every path that differs from the automatic merge |
| `resolutions`, `seed_pins_changed`, `ignored_files_left_out` | `seal` | The notes as given; whether a seed's pin changed; files `git add` left out because they are ignored |

### What it holds upstream's content to

- **Nothing from another tree is executed.** The three tools that run
  (`scripts/rename-roles.py`, `models.py`, `models_lint.py`) are read out of
  the fork's commit into a private directory. A file of the same name in the
  tree being merged, or an edited one in the work tree, is never run.
- **Some trees are not checked out at all** (`unsupported-entry`): one with a
  link, another repository, a `.gitattributes` file, a name with a character
  outside `A-Za-z0-9._/+@-` or a component that starts with a dash, or two
  names that are one file on a case-insensitive file system. The merge base
  and upstream's tip are scanned, and so is the merged tree as a whole,
  before it is checked out and again in `seal`: upstream's `scripts/Maint/`
  beside the fork's `scripts/maint/` is clean on each side and one directory
  on the owner's disk. `merge` attributes are switched off in the scratch
  repository as well: `merge=union` alone turns a conflict into a silent mix
  of both sides.
- **Five things leave an intake exactly as the fork has them**, compared
  object by object in the final tree: everything under `.github/` (a pushed
  workflow change would run), the model manifest, its override
  `.claude/models.local.json` (absent stays absent), `.claude/STATE.md` and
  `.claude/docs/DESIGN.md`. `--owner-allows-workflow-changes` lifts the first,
  for the owner at a terminal.
- **A seed changes only with its pin.** `intake` holds the seeds to the
  digests the fork pins; `seal` accepts a new seed together with its new
  digest and reports `seed_pins_changed`.
- **This repository changes once**, at the end: the finished commit is
  fetched from the scratch repository into the new branch, read back, and
  removed again if it is not the merge that was built. No index, work tree,
  configuration, hook or other ref of this repository is used or touched,
  and no inherited `GIT_*` variable is followed.

### Intake outcomes

The words and codes below are exactly what the code can report (a test
compares these tables with `OUTCOMES` in `fork_invariants.py`).

#### `check`

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `satisfied` | 0 | Every invariant holds | — |
| `violated` | 2 | At least one finding; each names its invariant | Fix what it names |
| `refused` | 1 | Not the top of a repository, git older than 2.40, or `--rev` is not a full SHA of a commit here | Fix the call |
| `git-failed` | 3 | A git command failed | Read the message |
| `tool-failed` | 3 | One of the fork's tools failed or exceeded 120 s | Read the message |
| `crashed` | 3 | The helper itself raised an unexpected error; the traceback is on stderr | Report it |

#### `intake`

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `up-to-date` | 0 | The fork already contains upstream's tip | — |
| `merged` | 0 | The two-parent commit is on the local branch | Read `sensitive_changes`; push the branch, open the pull request |
| `conflicts` | 2 | Paths conflict after `T`; nothing was committed | Resolve in `--resolve-in`, then `seal` |
| `invariants-failed` | 2 | The merge is clean but a check fails, or a protected path changed; nothing was committed | Fix in `--resolve-in`, then `seal`; a `manifest-needs-owner` finding is the owner's |
| `workflows-changed` | 2 | Upstream changed a file under `.github/`; nothing was merged | The owner decides, at a terminal |
| `no-single-merge-base` | 2 | No common history, or more than one merge base | The owner merges by hand |
| `unsupported-entry` | 2 | The merge base, upstream's tip or the merged tree holds an entry that is not checked out | Read the listed entries |
| `transform-refused` | 2 | The codemod refused a file, or a tree has both seed layouts | Read the message |
| `fork-not-clean` | 2 | The fork's own commit fails an invariant | Fix `main` first |
| `branch-exists` | 2 | The local branch exists and is another commit; it is left as it is | Delete it, or inspect it |
| `refused` | 1 | Not the top of a repository, git older than 2.40, a shallow clone, a revision that is not a full SHA, `--resolve-in` exists | Fix the call |
| `git-failed` | 3 | A git command failed, or the branch read back was not the merge that was built (it is removed again) | Read the message |
| `tool-failed` | 3 | One of the fork's tools failed, or left a file behind | Read the message |
| `crashed` | 3 | The helper itself raised an unexpected error; nothing was committed | Report it |

#### `seal`

`seal` builds the automatic merge again, so what stops `intake` before a
merge exists can stop it too.

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `merged` | 0 | The two-parent commit is on the local branch | Read `edited_by_hand` and `sensitive_changes`; push the branch, open the pull request |
| `unresolved` | 2 | `--resolutions` does not name exactly the conflicted paths, or a marker line is left in one of them | Finish the paths it lists |
| `invariants-failed` | 2 | The finished tree fails a check, or changes a protected path | Fix what it names, seal again |
| `unsupported-entry` | 2 | The finished tree, the merge base or upstream's tip holds an entry that is not checked out | Remove the entry it lists |
| `workflows-changed` | 2 | As for `intake` | As for `intake` |
| `no-single-merge-base` | 2 | As for `intake` | As for `intake` |
| `transform-refused` | 2 | As for `intake` | As for `intake` |
| `fork-not-clean` | 2 | As for `intake` | As for `intake` |
| `branch-exists` | 2 | The local branch exists and is another commit; it is left as it is | Delete it, or inspect it |
| `refused` | 1 | As for `intake`; `--resolve-in` was not prepared by `intake`, or for other commits; a damaged `--resolutions` | Fix the call |
| `git-failed` | 3 | A git command failed, or the branch read back was not the merge that was built | Read the message |
| `tool-failed` | 3 | One of the fork's tools failed | Read the message |
| `crashed` | 3 | The helper itself raised an unexpected error; nothing was committed | Report it |

### Issue worker

`routines/issue-worker.md` is the prompt of one cloud routine, kept here so a
change to it is reviewed like code. A session starts from a fresh clone with
no other context, loads the repository's rules through the `context-loader`
skill, takes one open issue the owner wrote and labeled `agent:ready`, and
ends by reporting in its own comment on the `automation:status` issue
(`<!-- job:issue-worker -->`). A test pins every hard rule of the prompt as
an exact sentence, and their number: an edit to a rule has to change the test
in the same diff. Creating or changing the routine itself is the owner's.

A cloud session acts under the owner's GitHub account. Three things follow,
and the prompt is built on them:

- **An author proves nothing.** Jobs open issues and post comments as the
  owner, quoting text the owner did not write. Every body a job writes
  starts with `<!-- job:` (`common.JOB_MARKER_PREFIX`; the local job's issues
  and status comment too). The worker drops an issue whose body starts with
  it or that carries an `automation:` label, never reads a comment, and
  checks in the issue's events that the owner's account applied the label.
- **Whether a pull request may merge is not the session's to judge.** The
  worker runs `worker_gate.py` as it is on `main`
  (`git show refs/remotes/origin/main:scripts/maint/worker_gate.py`), never
  as it is in the pull request, and merges only on exit 0. Everything else
  stays open for the owner.
- **The merge is pinned.** One REST squash call, at the commit the review
  read, the gate allowed and both required checks, produced by GitHub
  Actions, succeeded on.

`worker_gate.py --base SHA --head SHA [--project-root DIR]` judges the change
between the merge base of the two commits and `--head`, which must be the
commit checked out, with nothing beside it. It is one file with no import
from the repository, so that it can be read out of a commit and run; the
verifier it starts, `verify_delegation.py`, is read out of `--base` the same
way (it needs Python 3.11; under an older one the gate fails closed with
`tool-failed`).

The stop-list, any path below or equal to one of these, in any letter case:
`.github/`, `.claude/hooks/`, `.claude/rules/`, `.claude/skills/`,
`.claude/agents/`, `.agents/`, `.codex/`, `scripts/`, `tests/`,
`.claude/settings.json`, `.mcp.json`, `pyproject.toml`, `uv.lock`, and any
`AGENTS.md`, `CLAUDE.md` or `CLAUDE.local.md`, in whatever directory. These
define the required checks, or are what a
later session executes or loads as instructions: a pull request that changes
one would certify itself, or instruct whoever merges the next one.

#### `worker_gate`

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `self-merge-allowed` | 0 | No changed path is on the stop-list, every one is a plain file with a supported name, no test file is deleted, and the verifier reports no weakened test and no placeholder | The worker may send its merge call |
| `needs-owner` | 2 | At least one of those does not hold; `reasons` lists each | The pull request stays open; the owner merges or closes it |
| `refused` | 1 | A revision is not a full SHA of a commit here, `--head` is not what is checked out, or the work tree holds anything else | Fix the call; the worker does not merge |
| `git-failed` | 3 | A git command failed | The worker does not merge |
| `tool-failed` | 3 | The verifier is not in `--base`, failed, or did not report | The worker does not merge |
| `crashed` | 3 | The gate itself raised an unexpected error | The worker does not merge; report it |

**Trigger and schedule.** The prompt does not depend on what starts it.
Recommended: a schedule, daily at `37 18 * * *` UTC, which is 03:37 the next
day in Asia/Tokyo (UTC+9, no daylight saving), away from the local job's
08:40 and 20:40. An issue-event trigger on `labeled` can be added to the same
routine later: the prompt works out by itself whether an issue is eligible.
For an intake run by hand or by a later routine, upstream's cadence suggests
weekly: `7 0 * * 1` UTC is Monday 09:07 in Asia/Tokyo.

### Files and tests

| File | Role |
|------|------|
| `fork_invariants.py` | The invariants, `check`, and the command line for all three commands |
| `upstream_intake.py` | `T`, the merge, the protected paths, what is reported, `intake` |
| `intake_seal.py` | `seal`: the automatic merge built again, the answers, the finished tree |
| `scratch_repo.py` | git cut off from the user's configuration; the scratch repository; which trees are not checked out; the inert forms of a path and of text |
| `worker_gate.py` | The issue worker's merge gate and the stop-list; one file, no imports from the repository |
| `routines/issue-worker.md` | The issue worker's prompt |

Tests: `tests/test_maint_fork_invariants.py`, on real scratch repositories
that model an upstream and a fork made from it with the repository's own
codemod; `tests/test_maint_worker_gate.py`, the gate on real scratch
repositories and the prompt's rules.
