# Design Document — 要件定義書 (Requirements & Macro Design)

> **Role:** Macro-level requirements and design — *what* this project builds and *why*.
> Written at `/init`, kept current by `/design-tracker` (also invoked from `/checkpointing`).
>
> **Document map:** Shared rules → [rules/](../rules/) ·
> Root contract → [AGENTS.md](../../AGENTS.md) · State → [STATE.md](../STATE.md) ·
> Micro work progress (latest 5 checkpoints) → [PROGRESS.md](../../PROGRESS.md)

## 背景・目的 (Background & Purpose)

<!-- Why does this project exist? What problem does it solve, for whom?
     State the business/technical context and the goal in a few sentences. -->

`claude-code-orchestra` is the owner's fork of the Claude Code Orchestra template: a multi-agent orchestration layer (root `AGENTS.md`, `.claude/` agents, skills, rules and hooks, plus contracts for Codex and other CLI executors) that is installed into other repositories. The fork exists so that model updates land quickly and so that the role-to-model assignment is the owner's own, kept in one model manifest. It reaches the owner's projects through the user-level `/apply-orche` and `/sync-orche` skills, which drive `scripts/install.sh` and `scripts/update.sh`. The fork is itself developed with the orchestration it ships.

## スコープ (Scope)

### In Scope

<!-- What this project explicitly delivers. -->

- 

- The template content installed projects receive: root `AGENTS.md`, `.agents/AGENTS.md`, `.codex/AGENTS.md`, `.claude/{agents,skills,rules,hooks}`, the shared docs listed in `scripts/install.sh`, and the seeds in `scripts/scaffold/`.
- The installer, the updater and the gate: `scripts/install.sh`, `scripts/update.sh`, `scripts/check.sh`.
- The model manifest (`.claude/skills/_shared/models.json`) and its tools (`models.py`, `models_lint.py`).
- Fork maintenance: keeping model assignments current and taking in upstream changes under review.

### Out of Scope

<!-- What is explicitly NOT covered, to prevent scope creep. -->

- 

- Application code: this repository ships no Python package (`[tool.uv] package = false`).
- The state of installed projects: their `.claude/STATE.md`, `.claude/docs/DESIGN.md`, `PROGRESS.md` and checkpoints belong to them; this repository's own state is never shipped.
- Committing in a target project: the updater changes files, the project's owner commits them.

## 機能要件 (Functional Requirements)

<!-- What the system must do. Each requirement gets a stable ID (FR-1, FR-2, ...). -->

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-1 | Install the orchestration layer into an existing git repository without overwriting project-owned files | Must | `scripts/install.sh`: conflict-aware, backups on replace, settings merge candidates, seeds from `scripts/scaffold/` |
| FR-2 | Update an installed project to a newer template revision, replacing template-owned paths and preserving project state | Must | `scripts/update.sh`: `SAFE_DIRS` / `SAFE_FILES` swapped atomically; STATE/DESIGN seeded only when missing |
| FR-3 | Choose every role's model in one manifest and render it into agent definitions and runtime settings | Must | `models.py apply`; project overrides in `.claude/models.local.json` |
| FR-4 | Route work from the main agent to role-named subagents and peer CLIs, and verify delegated results | Must | `.claude/rules/delegation.md`, `cli_consult.py` / `codex_consult.py`, `verify_delegation.py` |
| FR-5 | Keep project context across sessions through typed writers | Must | `init`, `design-tracker`, `checkpointing`, `context-loader` skills |
| FR-6 | Gate every change with a portable self-check of the repository's own layout and contracts | Must | `bash scripts/check.sh` plus `uv run pytest`, on ubuntu and macOS CI |

## 非機能要件 (Non-Functional Requirements)

<!-- Quality attributes: performance, availability, security, maintainability, etc.
     Prefer measurable targets in the Metric column. -->

| Category | Requirement | Metric / Target |
|----------|-------------|-----------------|
| Performance | | |
| Availability | A new model release reaches installed projects quickly without hand-editing | One manifest edit, re-applied by the updater |
| Security | No secrets in the repository; delegated runs are verified before they are trusted; a target project is never committed to by automation | Guardrails in root `AGENTS.md`; `verify_delegation.py` on every delegated change |
| Maintainability | A model is named in exactly one place, and the gate fails when prose or code names one elsewhere | `models_lint.py` exit 0 inside `bash scripts/check.sh` (10 checks passed) |
| Portability | Scripts run with the system tools of both macOS (BSD) and Linux (GNU); bundled Python scripts use the standard library only | Both required CI gates (ubuntu, macos) green on `main` |

## アーキテクチャ (Architecture)

<!-- High-level architecture: components, data flow, boundaries.
     Add a diagram or description here. -->

### Agent Roles

| Agent | Role | Responsibilities |
|-------|------|------------------|
| Main agent (Claude Code) | Orchestrator | User interaction, routing, approvals, integration of results, final report |
| general-purpose-standard | Routine implementation | Well-scoped changes with clear acceptance checks |
| general-purpose-deep | Research and difficult implementation | Broad analysis, external research, cross-cutting or risk-sensitive changes, Codex delegation |
| codex-debugger | Root-cause analysis | Failing tests, builds and unexpected behaviour |
| Codex CLI (Tier 2 `sol`) | Design and complex implementation | Planning, design review, complex algorithms; verified by the caller |
| fable-advisor (Tier 3 `fable`) | Independent judgment | Arbitration, unblocking after repeated failure, final review of large changes |

### Layout

- Root `AGENTS.md` is the single contract every runtime loads; no `CLAUDE.md` may shadow it.
- `.claude/` is the physical runtime: `agents/`, `skills/` (with the bundled runtime `_shared/`), `rules/`, `hooks/`, `docs/`, `settings.json`, and the project state (`STATE.md`, `docs/DESIGN.md`, `checkpoints/`, `logs/`).
- `.agents/AGENTS.md` and `.codex/` hold the contracts for non-Claude CLI executors and reference `.claude/` by path, never by copy.
- `scripts/install.sh` and `scripts/update.sh` move template-owned paths into a project; project-owned files are created from `scripts/scaffold/` only where missing.
- `.claude/skills/_shared/models.json` is the only place a model is chosen; `models.py apply` renders it into agent definitions, `.claude/settings.json` and `.codex/config.toml`.

## 技術選定 (Tech Stack & Rationale)

<!-- Chosen technologies and why. Record alternatives considered. -->

| Area | Technology | Rationale | Alternatives Considered |
|------|------------|-----------|-------------------------|
| Scripts | Python 3, standard library only | Runs wherever the hooks already need `python3`, with no install step in the target project | Third-party dependencies (would need an environment in every target) |
| Installer / updater / gate | bash | Available on every target; written to the BSD/GNU common subset | Python entry points |
| Dev tooling | uv, ruff, ty, pytest, poethepoet | Declared in `pyproject.toml`; uv is the only package manager (`.claude/rules/dev-environment.md`) | pip / venv by hand |
| CI | GitHub Actions, ubuntu and macOS matrix | The two platforms the scripts must run on; both are required checks on `main` | Single-platform CI |
| Model manifest | JSON (`.claude/skills/_shared/models.json`) | Readable and writable with the standard library on the oldest system Python in use | TOML (no stdlib writer) |

## 制約 (Constraints)

<!-- Technical, organizational, regulatory, or resource constraints. -->

- 

- Bundled scripts use the Python standard library only; shell scripts stay within the BSD/GNU common subset.
- Model names and model IDs appear only in `.claude/skills/_shared/models.json`; prose names roles (`models_lint.py`, run by `scripts/check.sh`).
- `scripts/scaffold/` holds the pristine seeds projects receive and stays byte-identical to upstream's seeds.
- No `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` at or above the repository root.
- `main` accepts changes only through pull requests with both required gate checks green.

### Development Procedure (this repository)

Every change follows these steps. Each names the existing skill that defines it; nothing here adds process.

1. Start: `context-loader`.
2. Plan: `plan` (or `feature` for multi-step work) to `.claude/docs/plans/<slug>.md`, validated by Codex. A known edit of about 20 lines in one file needs no plan (`.claude/rules/delegation.md`, Self-Handle List).
3. Implement on a branch, delegating per `.claude/rules/delegation.md` (`tdd` for behaviour changes, `team-execute` for parallel work).
4. Gates: `bash scripts/check.sh` and `uv run pytest`.
5. Review gate before the PR may merge: `/team-execute --review-only`. Critical and High findings are fixed first.
6. Record: `design-tracker` for a decision about how this fork is built or run (this file). A decision that changes what installed projects receive is also recorded in `TEMPLATE_DESIGN_LOG.md`.
7. Checkpoint: `/checkpointing` as the last commit of the branch (`PROGRESS.md`, compacted `.claude/STATE.md`), then open the PR; it merges by auto-merge (squash) once both gate checks pass.

Cloud agents (no Codex, fresh clone) follow steps 1-6 with the substitutions in `.claude/rules/codex-delegation.md` § When Codex Is Unavailable. They skip step 7 and never write working blocks to `.claude/STATE.md` or edit `PROGRESS.md`: a fresh clone has no `.claude/checkpoints/` to rebuild `PROGRESS.md` from, and concurrent appends to `STATE.md` conflict.

A change reaches the owner's Mac and installed projects only after it is promoted (see the maintenance and trust-policy decisions).

## Key Decisions

<!-- Durable architectural/design decisions. Append-only log. -->

| Decision | Rationale | Alternatives Considered | Date |
|----------|-----------|------------------------|------|
| Fork topology and merge policy: `origin` is the fork and `upstream` is fetch-only; `main` is protected by two required checks (the ubuntu and macOS gates), enforced for admins, with no force-push; a PR that originates in the fork squash-merges automatically once the checks are green; a PR that brings in upstream changes uses a merge commit and is never auto-merged | Fork-only work is small and gated, so a linear squash history with unattended merge is safe; upstream intake must keep upstream's commits as ancestors for later three-way merges and needs a human look at what it changes | Manual merge of every PR (slows model updates); squash upstream intake too (loses the merge base and makes every later intake conflict) | 2026-11-18 |
| One model manifest: every role's model comes from `.claude/skills/_shared/models.json`, rendered by `models.py apply` into agent definitions, `.claude/settings.json` and `.codex/config.toml`, and re-applied in installed projects by the updater; a project overrides keys in `.claude/models.local.json`, and a custom value already in a project is preserved | A model used to live in several files plus prose, and installs had drifted apart; one source makes a model bump a one-line change that reaches every project without erasing a project's deliberate choice | Per-file model settings; overwriting the managed keys unconditionally; diff-only reporting on update | 2026-11-18 |
| Roles are named for their work, not for a model (`general-purpose-standard`, `general-purpose-deep`, `codex-debugger`, `fable-advisor`), and a tree-wide lint (`models_lint.py`, run by `scripts/check.sh`) keeps model names in the manifest only | A role name or sentence that states a model becomes false at the next bump; routing by the kind of work stays true when the manifest changes | Model-named roles; a documentation convention without a gate | 2026-11-18 |
| Scaffold separation: installed projects are seeded from `scripts/scaffold/`, never from this repository's live `.claude/STATE.md`, `.claude/docs/DESIGN.md` or `PROGRESS.md`, so this repository's own state can be real and committed | Dogfooding needs `init`, `design-tracker` and `checkpointing` to write here, and the skills hard-code the canonical paths; moving the seeds keeps that state out of every install | Keep the seeds at the canonical paths and this repository's records elsewhere (every skill would need another path) | 2026-11-19 |
| Automation trust policy (owner decision): only pure model bumps reach installed projects unattended; every other merged change reaches them only after the owner promotes it; upstream intake may open a PR but is never merged unattended; a target project is updated only when its working tree is clean and is never committed to | A model bump is a reviewed one-line manifest change with a narrow blast radius; behaviour changes to hooks, skills or rules can break a working project and need the owner's decision; leaving commits to the project's owner keeps their history theirs | Distribute every merged change automatically; distribute nothing without the owner; commit the update in the target project | 2026-11-18 |
| Maintenance runs locally: a launchd job on the owner's Mac executes only an approved revision recorded in a local file, so merging to `main` and executing on the Mac are separate decisions; cloud routines are used only for judgment work (upstream intake, issue work). Status: implemented in PR #9, not yet installed | The job touches the owner's local projects and credentials, so what it runs must be pinned by an explicit approval rather than by whatever `main` holds; cloud sessions have no access to the Mac and are suited to reading and proposing | Run the job from `main` directly (a merge would execute unattended); run maintenance in the cloud (no access to local projects) | 2026-11-18 |
| When Codex is unavailable (cloud sessions, installs without the CLI), each Codex step is substituted per `.claude/rules/codex-delegation.md` § When Codex Is Unavailable and the substitution is recorded in the work log or PR body | Skipping the step silently or retrying a missing CLI both hide that no second model reviewed the change; a named substitute keeps the review and makes its absence visible | Skip Codex steps when the CLI is missing; block the work until Codex is available | 2026-11-18 |
| Dogfooding: every change to this repository goes through its own routes (`.claude/rules/delegation.md`) and the `/team-execute --review-only` ship gate before merge, following the Development Procedure under Constraints | The template is only trustworthy if its own workflows survive daily use; defects found here are found before they reach installed projects | Develop the template by hand outside its own orchestration | 2026-11-18 |

## TODO / Open Questions

<!-- Open design questions and deferred decisions for this project. -->

- 

- [ ] Confirm the minimum Python version for bundled scripts: the manifest tooling is described as working on the macOS system Python 3.9, while `pyproject.toml` requires `>=3.11` and several `_shared` scripts import `datetime.UTC` (3.11+).
- [ ] Decide whether `.claude/docs/plans/` and `.claude/docs/reviews/` notes are committed with the PR they belong to (today they are kept local).
