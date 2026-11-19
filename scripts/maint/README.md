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
- **Approvals are typed at a terminal.** `install`, `promote` and `adopt`
  refuse a piped answer.
- **A project is somebody's repository, and nothing of it is executed.** The
  job reads a project with five git subcommands that only read, refuses one
  whose own git configuration holds anything but inert keys, and runs the
  updater on a private copy, never in the project. See Distribution.
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
| `~/.claude/orchestra/state/distribution.json` | The projects adopted for updates, and what the last run found for each listed one | `adopt`, `distribute` |
| `~/.claude/orchestra/logs/maint/<UTC stamp>.adopt.log` | Every path one `adopt` screen stood for, one per line; pruned with the run logs | `adopt` |
| `~/.claude/orchestra/state/launcher-failure` | Present only while the launcher is failing | launcher |
| `~/.claude/orchestra/run/` | `maint.lock`, scratch worktrees (removed after use) | each run |

`~/.claude/orchestra/targets.txt` is not created here. It is the owner's list
of installed projects (the `sync-orche` skill keeps it), and the job only
reads it.

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
| `distribute` | In each adopted project: the template's own paths, the model keys of its two settings files, a missing seed file; always left uncommitted. A scratch worktree and copies under `run/distribute-<pid>/`; `state/distribution.json` | — |
| `model-watch` | A scratch worktree and scratch directories under `run/`; `bump-records.json` | Branch `bot/models-<slug>`, one pull request with auto-merge (or auto-merge withdrawn from one whose branch changed), `automation:needs-owner` issues, that label when missing |
| `report` | — | Its own comment in the open issue labeled `automation:status`; that label when missing |
| every run | Its log, `last-run.json`, the lock | — |

`adopt` (the owner's command) writes into one project the same way, after
showing what it would change.

## Promotion

`promote` compares the tree at the approved revision with the tree at the
default branch (the net change, not commit by commit: the Mac only ever runs
the tree at the end of the range).

**Never by itself: anything that is not a plain file.** If the range adds or
changes an entry whose mode is not 100644 or 100755 (a link, 120000; another
repository, 160000), wherever it is, the range waits and the result names the
path and the mode. Such an entry can make the tree impossible to check out.

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
have promoted it anyway), the command that prints the full diff, and asks for
a typed `yes`. It needs a terminal. When the default branch was rewritten
there is no range, so it shows the newest commits and the same comparison of
the approved tree with the new one; if the two cannot be compared it does not
ask.

After an automatic promotion the run restarts once through the launcher, so
the remaining steps execute the revision that was just approved.

## Distribution

`distribute` brings the approved revision into the projects listed in
`~/.claude/orchestra/targets.txt`: one absolute path per line, `#` comments
and blank lines skipped, duplicates dropped. It runs right after promotion, so
a revision reaches a project only after it was approved; since approval is
automatic only for an inert range and for this Mac's own model bump, a model
bump is the only change to what a project holds that travels unattended.

The rules, each enforced in code:

- **Approved only.** The source is a scratch worktree made from the SHA in
  `state/approved-rev`. The default branch is not read by this step. What the
  updater produced is compared with that worktree before any of it is written.
- **Adopted first.** A listed project is not touched, not even looked at,
  until the owner ran `launchd.sh adopt <project>` for it. The adoption is
  recorded in `state/distribution.json`, outside the project, together with
  the file system's numbers for the directory and its repository: another
  clone at the same path is not the adopted one.
- **Clean only.** A project with a staged, unstaged or untracked-and-not-
  ignored change is skipped and looked at again on the next run.
- **Never a commit.** Nothing is committed, staged, stashed, reset or checked
  out in a project. The update is left as uncommitted changes for the owner
  to commit. **Until then the project is not clean, so the next approved
  revision waits for it**: that is `waiting-dirty`, shown by `status` with the
  revision that waits and since when. It is a state, not a failure.
- **Its own output only.** A scheduled run replaces template-owned files only
  while they are byte for byte what the job left there, and beyond them
  changes nothing but the model keys of the two settings files and a missing
  seed file. A local edit to a template file, a retired path that came back,
  a file the template newly claims: all of these send the project back to
  `adopt` (`local-changes`).
- **Nothing of a project is executed.** The updater (`scripts/update.sh` of
  the approved revision, and the manifest tool it calls) runs on a private
  copy of the paths it manages, inside a scratch repository the job created.
  The difference between that copy before and after is then written into the
  project with plain file operations, each path first compared with what the
  copy was taken from. In the project itself git runs five subcommands, all
  of which only read (`rev-parse`, `config --list`, `status`, `ls-files`,
  `for-each-ref`), with the job's hardened options, no optional index refresh
  and no transport; the code refuses to start any other. A project whose own git
  configuration sets a key outside a list of inert ones (a filter, a diff
  driver, an include, a promisor remote, …) is refused with the key named. A
  link, or a repository of its own, on the way to a template path is refused
  too. `HEAD`, every ref and the index are compared before and after a write.

`adopt` is typed at a terminal. It needs the project listed in `targets.txt`,
the template already installed there (it updates; `scripts/install.sh`
installs), and the tree clean. It shows the project, the revision and the installed
version; what typing `yes` would create, change and delete, counted per
directory, with every path in a private file; and, one line each, every file
that would be deleted or overwritten while git does not track it, since git
could not bring that content back. The list is the difference the approved
updater made on the private copy, not a prediction, and exactly that
difference is what gets written. It is also the migration an old installation
needs: a tracked path the updater would remove with `git rm` ends up as an
unstaged deletion instead.

Limits worth knowing:

- The job's git does not read the owner's global configuration. Files ignored
  only through `core.excludesFile` there count as untracked here; patterns in
  `~/.config/git/ignore` or in the project are honoured. Content filters from
  the global configuration (LFS) do not run either, so a file git would have
  to read through one can make a project look not clean.
- "Clean" is what `git status` reports. An index told to hide changes
  (assume-unchanged, skip-worktree, a sparse checkout) passes as clean. What
  bounds the consequence is the rule above: only paths compared byte for byte
  are ever replaced.
- Changes inside a submodule's own work tree are not looked at: looking would
  run git inside it. The updater's paths never lie in one.
- The job cannot lock a project. Something else writing there between the
  comparison and the write is not detected; the write is a rename per file,
  and every path is verified afterwards.
- The updater's standard output is dropped, because it prints the difference
  between the project's settings and the template's. Its errors go to the run
  log.
- The project's own `scripts/check.sh` is not run afterwards: that would be
  executing the project.
- To stop distributing to a project, take its line out of `targets.txt`.

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
it. While such a file is on the default branch the step does nothing
(`override-in-tree`).

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
  and the outcome is `unknown-branch`.
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
| `awaiting-promotion` | 0 / 2 | Merged changes wait for approval: they touch what the Mac executes or ships, add or change an entry that is not a plain file, or are a bump this Mac's record does not cover. 2 once the oldest one landed on `main` more than 7 days ago (a merge counts once, at the time it was merged) | Review and run `launchd.sh promote` |
| `diverged` | 2 | The approved revision is no longer in the default branch's history | Run `launchd.sh promote`; it shows the new history |
| `no-approved-revision` | 1 | `state/approved-rev` is missing or not a SHA (the launcher normally stops before this) | Run `launchd.sh install` |
| `git-failed` | 3 | The range could not be read, or the branch moved mid-run | Retried |

### distribute

The step's result is the worst of its projects; its counts are the number of
listed projects (`targets`) and how many are in each state of the next table.

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `no-targets` | 0 | `targets.txt` is missing or lists nothing | — |
| `up-to-date` | 0 | Every listed project holds the approved revision's files | — |
| `distributed` | 0 | At least one project was updated; the changes are uncommitted there | Review and commit them in the project |
| `waiting` | 0 | Nothing was written, and at least one project waits (`waiting-dirty`, `busy`) | Commit in the project when you want the update |
| `needs-owner` | 2 | A project is `not-adopted`, `local-changes` or `refused` | See that project's line in `launchd.sh status` |
| `target-failed` | 3 | The update of a project failed (`failed`); the others were still visited | Read the newest log |
| `source-failed` | 3 | The approved revision could not be laid out in a scratch worktree, or its updater's path lists cannot be read; no project was touched | Retried; if it repeats, read the newest log |
| `no-approved-revision` | 1 | `state/approved-rev` is missing or not a SHA | Run `launchd.sh install` |

### distribute, per project

| Outcome | Exit | Means | What to do |
|---------|------|-------|------------|
| `up-to-date` | 0 | Holds the approved revision's files; tree clean | — |
| `uncommitted` | 0 | Holds the approved revision's files; the tree is not clean, usually because the last update is not committed yet | Commit, or the next revision will wait |
| `updated` | 0 | Updated in this run; left uncommitted | Review and commit |
| `waiting-dirty` | 0 | A revision waits because the tree is not clean. `status` shows which, and since when | Commit or clean the tree |
| `busy` | 0 | git is in the middle of a rebase, merge, pick, revert or bisect there (or holds the index lock), or a Claude Code session wrote in the project within the hour | — |
| `not-adopted` | 2 | Listed, never adopted, or another directory or repository is at the path now. No file there was read and no git ran there | `launchd.sh adopt <project>` |
| `local-changes` | 2 | Template-owned files are not what the job left, the update would change something a scheduled run does not change by itself, or an earlier write stopped half-way | `launchd.sh adopt <project>` shows it and asks |
| `refused` | 2 | Not an absolute real path to a directory with its own repository; overlaps the job's directory; a git configuration key outside the inert list; a link or nested repository on a template path; no commit yet; or model settings the manifest tool will not bring in line | Fix what the line names |
| `failed` | 3 | The updater failed on the private copy (the project was not written), the project changed between the rehearsal and the write (not written), or a write did not verify | Read the newest log; an unverified write goes to `local-changes` next |

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
| `override-in-tree` | 2 | `main` carries `.claude/models.local.json`, a project's override file; the watch does nothing while it is there | Remove the file from the fork |
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
bash scripts/maint/launchd.sh adopt DIR   # show what updating the project DIR would change, ask, update it, and let scheduled runs keep it updated
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
   the owner can see what waits and approve a fix;
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
| `bump.py` | What one model bump is, entries that are not plain files, and the single-use records of the bumps this Mac tested |
| `model_watch.py` | Candidate rule and the resumable bump |
| `report.py` | The public status comment |
| `distribute.py` | The distribution step: the approved source, the rehearsal on a private copy, the record of adoptions, one state per project |
| `targets.py` | Everything that touches a project: the registry, the reading-only git and its configuration list, what counts as clean, and the one function that writes |
| `adopt.py` | The owner's screen and prompt for one project |

Tests: `tests/test_maint_promote.py`, `tests/test_maint_model_watch.py`,
`tests/test_maint_launchd.py`, `tests/test_maint_distribute.py`, with fakes in
`tests/maint_support.py`. They use scratch repositories, a fake `gh`, `codex`
and `launchctl`, a `git` stand-in that redirects the fork URL to a scratch
repository, and a temp `HOME`; none reaches the network or the real home
directory. The distribution tests install the template into scratch
repositories with the real installer and update them with the real updater.
