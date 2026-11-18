# AGENTS.md -- Codex Adapter

This file is the **Codex-specific adapter**. The common CLI-agent contract
(response structure, handoff rules, cross-CLI subagent invocation, and the
completion-verification guardrails) is the root **`AGENTS.md`**, which Codex
loads automatically. Read that first.

Codex operates as **Tier 2 (`sol`)** in the agent hierarchy. See
`.claude/rules/tiers.md` for the full tier definitions.

## Model Configuration

Source of truth for the model and the reasoning effort: the model manifest,
`.claude/skills/_shared/models.json`. `python3 .claude/skills/_shared/models.py apply`
renders both into `.codex/config.toml`; do not edit them there (in an installed
project a value set there by hand is kept and reported as `custom`; to change
it deliberately, record it in `.claude/models.local.json`). The other two keys
are set in `.codex/config.toml` directly.

| Key                      | Value          |
|--------------------------|----------------|
| `model`                  | manifest `codex.model` |
| `model_reasoning_effort` | manifest `codex.reasoning_effort` |
| `approval_policy`        | `never`        |
| `sandbox_mode`           | `danger-full-access` |

## Sandbox Discipline

`approval_policy` is `"never"` and `sandbox_mode` is `"danger-full-access"`, so
Codex runs unattended with unrestricted filesystem and network access and no
confirmation prompts. Callers doing analysis, review, or planning should still
pass an explicit `--sandbox read-only` to avoid accidental writes during those
tasks. Completion verification (Sol Guardrails, root `AGENTS.md` section
"Guardrails (Completion Verification)") remains mandatory regardless of sandbox
mode.

## Enabled Codex Skills

Skills are enabled by `[[skills.config]]` entries in `.codex/config.toml` that
point directly at the canonical implementations under `.claude/skills/`.
Nothing is copied into `.codex/`.

| Skill            | Path                            |
|------------------|---------------------------------|
| `context-loader` | `.claude/skills/context-loader` |
| `design-tracker` | `.claude/skills/design-tracker` |

## Internal Context References

Codex may reference the following project paths as needed:

- `.claude/docs/DESIGN.md` -- macro requirements and design decisions
- `.claude/docs/research/` -- research notes and findings
- `.claude/rules/` -- coding standards and delegation rules
- `.claude/STATE.md` -- active main agent and cross-session working state
- `.claude/logs/cli-tools.jsonl` -- Codex call history log
