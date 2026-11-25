---
name: orche-install
description: Install the approved Claude Code Orchestra template into the project this session is open in. Checks the project, then opens a new terminal window running `launchd.sh install-project`, where the owner types yes. Never installs, lists or adopts anything itself.
disable-model-invocation: true
---

# orche-install

Install the orchestra template into a **new** project: the one this Claude
Code session was started in. The work is done by the fork's maintenance job
(`launchd.sh install-project`), from the approved revision only. This skill
checks the project, explains, and opens a terminal window for the owner.

Source: `scripts/maint/skills/orche-install/` in the fork. `launchd.sh install`
and `launchd.sh promote` copy it here from the approved revision; do not edit
the installed copy.

## Rules (binding)

- **You never type the confirmation.** The only approval is a `yes` the owner
  types in the new window. Do not send keystrokes to any terminal, do not use
  `osascript`, do not pipe or echo `yes` into anything.
- **You never run `install-project`, `adopt` or `install.sh` yourself**, not
  even to "try". `install-project` refuses a stdin that is not a terminal, and
  that refusal is the point. You only run the helper below with `check`,
  `open` and `verify`.
- **You never commit, stage, stash or reset in the project**, and never edit
  `~/.claude/orchestra/targets.txt` or `state/distribution.json`.
- Do not offer `--force`, a manual copy of the template, or the developer
  checkout's `scripts/install.sh` as a shortcut: they bypass the approved
  revision.

The helper is `~/.claude/skills/orche-install/orche-install.sh` (call it
`HELPER` below). Run it with `bash`; pass the project path as one quoted
argument.

## Procedure

1. **Find the project.** `DIR="$(pwd -P)"`. It must be the top level of the
   project's git work tree; if the session is in a subdirectory, stop and say
   so (do not `cd` elsewhere on your own).

2. **Check.** Run `bash "$HELPER" check "$DIR"`. Exit 0 means the window can
   be opened. Exit 2 prints the reason on stderr: explain it in Japanese, say
   what the owner can do (commit or clean the tree, use `adopt` for a project
   that already has the template, promote a revision that has
   `install-project`), and stop. Do not try to fix the project yourself
   unless the owner asks.

3. **Explain, in Japanese, before opening the window.** Keep it short:
   - a new terminal window opens and runs the approved revision's
     `launchd.sh install-project <DIR>` (a private copy of that file, read
     from the maintenance clone's git objects);
   - it installs the **approved revision** (`~/.claude/orchestra/state/approved-rev`),
     after showing what would be created, changed or deleted, and asks for a
     typed `yes`; anything else changes nothing at all;
   - after `yes`: the project is listed in `~/.claude/orchestra/targets.txt`,
     the template is written as **uncommitted** changes, and the adoption is
     recorded, so scheduled runs keep it updated;
   - nothing is committed; the owner commits (or keeps the files local, see
     below). Until then the scheduled runs report the project as
     `uncommitted`, and a newer approved revision waits for it
     (`waiting-dirty`; it becomes the owner's to act on after 7 days).

4. **Open the window.** Run `bash "$HELPER" open "$DIR"`. It runs `check`
   again, writes a private one-shot script (mode 700) beside the approved
   `launchd.sh`, and opens it in Ghostty
   (`open -na Ghostty.app --args -e <script>`) when Ghostty is installed, else
   in Terminal.app. Then tell the owner, in Japanese: switch to the new window,
   read the screen, and type `yes` there to install (or anything else to
   cancel); the window waits for Return at the end. Do not wait in a loop or
   poll; end your turn.

5. **Offer verification** for when the owner is back:
   - `bash "$HELPER" verify "$DIR"`: the version stamp is there, the project
     is listed, and the adoption is recorded for this very directory with no
     write that stopped half-way;
   - `bash scripts/check.sh` in the project: the installed template's own
     self-check;
   - `git status` in the project: the installed files, uncommitted.
   If `verify` reports a write that stopped half-way, point to
   `launchd.sh status` and to the window's own message; do not repair it.

6. **Committing, or keeping it local.** Explain both, in Japanese; the owner
   decides and does it:
   - **Commit** (default): review `git status` / `git diff`, then commit. A
     merge candidate such as `.claude/settings.orchestra.json` or
     `.codex/config.orchestra.toml` is git-ignored: merge it into the
     project's own settings, then delete it.
   - **Local only**: list the template's paths in `.git/info/exclude`, which
     is never committed. A set already in use on one of the owner's
     projects:

     ```
     /.claude/
     /.agents/
     /.codex/
     /scripts/check.sh
     /scripts/install.sh
     /scripts/update.sh
     /CLAUDE.local.md
     /.orchestra-backup-*/
     ```

     An exclude line only hides files git does not track. The installer also
     writes `AGENTS.md` and appends to `.gitignore` (and moves a `CLAUDE.md`
     aside), so those still show in `git status`: commit them, or add
     `/AGENTS.md` (and `/.gitignore` when it is new) to the exclude list too.
     The scheduled runs need a clean tree; until it is clean, updates wait.
