# Agent Tier Definitions

Three stable tiers for multi-agent orchestration. Tier IDs are permanent
and referenced by skills, rules, and configuration.

## Tier 1 -- `default` (Main Agent and Default Executors)

- **Scope**: User interaction and integration by the active main agent, plus
  routine direct or delegated work.
- **Selection criteria**: Default for all tasks unless escalation is needed.
- **Permission boundary**: The active runtime's native permissions; changing
  the main agent must follow `.claude/docs/change_main.md`.
- **Inputs**: User prompt, `AGENTS.md`, tier definitions, and relevant
  product-native rules.
- **Outputs**: Direct edits, user-facing responses, delegation calls to other tiers.
- **Default runtime**: Claude Code. The active runtime is recorded in
  `.claude/STATE.md`.
- **Default executor models**: Assigned per role in the model manifest
  (`.claude/skills/_shared/models.json`) and rendered from it into the agent
  definitions and `.claude/settings.json`. Documents name the role and leave
  the model to the manifest.
- **Tier-1 norm**: Routine, well-scoped implementation goes to
  `general-purpose-standard`; research, large-scale analysis, and difficult
  implementation go to `general-purpose-deep`. The role is chosen by the work,
  never by the model it currently runs on. The manifest is expected to assign
  `general-purpose-deep` the stronger model and `general-purpose-standard` one
  that costs less to spend on routine work. Every routing rule in this
  repository assumes that, and this is the only place it is stated: the other
  documents describe the work each role takes, not the model behind it.

## Tier 2 -- `sol` (Long-Duration Executor)

- **Scope**: Design, planning, complex implementation, long-running tasks.
- **Selection criteria**: Multi-file changes with behavior impact, architecture
  decisions, complex algorithms, root-cause-unknown debugging.
- **Permission boundary**: Full filesystem and network access by default (no
  sandbox) (`sandbox_mode = "danger-full-access"` in `.codex/config.toml`);
  `approval_policy` stays `"never"`. Callers doing planning/review/analysis
  should still pass an explicit `--sandbox read-only`.
- **Inputs**: Shared orchestration context plus a structured prompt following
  the Prompt Contract
  (`.claude/rules/codex-delegation.md` section "Prompt Contract").
- **Outputs**: Structured response (TL;DR / Analysis / Plan / Patch Strategy /
  Validation / Risks); file patches; validation commands.
- **Model**: Codex CLI model (`codex.model` in the model manifest,
  `.claude/skills/_shared/models.json`, rendered into `.codex/config.toml`).
- **Guardrails**: See root `AGENTS.md` section "Guardrails (Completion Verification)".

## Tier 3 -- `fable` (Independent Judgment / Final Authority)

- **Scope**: Design arbitration, unblocking stuck problems, final review of
  large changes -- and implementing the resolution when it is faster and safer
  to land the fix than to hand it back down a tier that has already failed at
  it repeatedly.
- **Selection criteria**: Escalation only -- used when lower tiers are stuck,
  conflicting, or a high-stakes decision requires independent judgment.
  Its value is a judgment formed in a fresh context, independent of the
  attempts that led to the escalation -- not a stronger model: the tier ID is
  a permanent identifier, and other roles may run on the same model. It stays
  an escalation route; the widened permission does not widen the scope.
- **Permission boundary**: Full filesystem access, same as Tier 2. Review
  notes still belong in `.claude/docs/reviews/`, but that is now a convention
  about where judgment is recorded, not a restriction on what can be written.
  A read-only advisor could only ever describe a fix, which meant every
  escalation ended by delegating the resolution back to the tier that had
  already failed at it.
- **Inputs**: Context summary, competing proposals or stuck-state description.
- **Outputs**: Judgment, arbitration decision, review notes, and -- when it
  implements -- a diff subject to the same completion verification as any
  other tier.
- **Model**: Claude `fable-advisor` agent
  (`.claude/agents/fable-advisor.md`); its model is assigned in the model
  manifest like every other agent's.
- **Guardrails**: See root `AGENTS.md` section "Guardrails
  (Completion Verification)". Fable's judgment is not self-certifying: a
  change it lands is verified like any other.

## Fable Differentiation

Multiple review mechanisms exist. Their scopes are distinct:

| Mechanism                        | Scope                                          | Trigger                          |
|----------------------------------|-------------------------------------------------|----------------------------------|
| team-execute Phase 2 reviewers   | Per-change ship gate (security, quality, tests) | Every team-execute change        |
| `/codex:adversarial-review`      | Code-level design challenge (external plugin)   | On-demand via codex-plugin-cc    |
| **Fable (Tier 3)**               | Independent fresh-context judgment: arbitration, unblocking after repeated failure, large-change final review, and landing the resolution | Manual escalation when stuck or high-stakes |
