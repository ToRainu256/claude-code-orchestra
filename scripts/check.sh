#!/usr/bin/env bash
# scripts/check.sh -- Consistency checker for the repository's agent contracts.
# Validates shared contracts, bootstraps, tier IDs, model coherence, and updater config.
# Exit 0 only if all checks pass.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PASS=0
FAIL=0

check() {
    local name="$1"
    shift
    if "$@"; then
        echo "PASS: ${name}"
        PASS=$((PASS + 1))
    else
        echo "FAIL: ${name}"
        FAIL=$((FAIL + 1))
    fi
}

# Portability: every check must run on both GNU (Linux) and BSD (macOS)
# userland and on bash 3.2. No `sed`/`grep` `\s` (use [[:space:]]), no
# `find -printf`, no `realpath -m`, no `sed -i`.

# grep, except that "no match" (status 1) is an empty result rather than an
# error. A real failure (status >= 2: unreadable path, bad pattern) still
# propagates, so a check never mistakes a broken scan for a clean one.
grep_or_empty() {
    local status=0
    grep "$@" || status=$?
    ((status <= 1)) || return "${status}"
}

# Print the names of DIR's direct entries, dotfiles included, one per line.
# Portable replacement for GNU `find DIR -mindepth 1 -maxdepth 1 -printf '%f\n'`.
list_entries() {
    local dir="$1"
    [[ -d "${dir}" && -r "${dir}" ]] || return 1
    (
        shopt -s nullglob dotglob
        local entry
        for entry in "${dir}"/*; do
            printf '%s\n' "${entry##*/}"
        done
    ) | LC_ALL=C sort
}

# Fail on any direct entry of DIR that is not in the allow-list given as the
# remaining arguments. An unreadable or empty directory fails as well: an
# empty listing must never read as "nothing unexpected".
check_allowed_entries() {
    local dir="$1"
    shift
    local rel="${dir#"${ROOT}/"}"
    local listing
    if ! listing="$(list_entries "${dir}")" || [[ -z "${listing}" ]]; then
        echo "  Could not list any entries in ${rel}/"
        return 1
    fi
    local ok=true
    local entry allowed found
    while IFS= read -r entry; do
        found=false
        for allowed in "$@"; do
            if [[ "${entry}" == "${allowed}" ]]; then
                found=true
                break
            fi
        done
        if [[ "${found}" == false ]]; then
            echo "  Unexpected ${rel} entry: ${entry}"
            ok=false
        fi
    done <<< "${listing}" || {
        echo "  Could not read the ${rel}/ listing"
        return 1
    }
    ${ok}
}

# --------------------------------------------------------------------------
# 1) Every relative markdown link / canonical-file path in INDEX.md exists
# --------------------------------------------------------------------------
check_index_links() {
    local index="${ROOT}/.claude/docs/INDEX.md"
    if [[ ! -f "${index}" ]]; then
        echo "  INDEX.md not found"
        return 1
    fi
    local ok=true
    # Extract paths that look like relative refs: backtick-wrapped or link targets
    # Matches: `path/to/file` and (path/to/file) patterns
    local paths backticked linked
    if ! backticked=$(grep_or_empty -oE '`\.[^`]+`' "${index}" | tr -d '`') ||
        ! linked=$(grep_or_empty -oE '\(\.[^)]+\)' "${index}" | tr -d '()'); then
        echo "  Could not scan INDEX.md"
        return 1
    fi
    # Deduplicate and filter
    if ! paths=$(printf '%s\n%s\n' "${backticked}" "${linked}" | sort -u | grep_or_empty -v '^$'); then
        echo "  Could not deduplicate the INDEX.md path list"
        return 1
    fi
    if [[ -z "${paths}" ]]; then
        echo "  No relative paths found in INDEX.md"
        return 1
    fi
    while IFS= read -r p; do
        [[ -z "${p}" ]] && continue
        local target="${ROOT}/${p}"
        if [[ ! -e "${target}" ]]; then
            echo "  Missing: ${p}"
            ok=false
        fi
    done <<< "${paths}" || {
        echo "  Could not read the INDEX.md path list"
        return 1
    }
    ${ok}
}
check "INDEX.md links resolve" check_index_links

# --------------------------------------------------------------------------
# 2) Tier IDs default/sol/fable all appear in tiers.md
# --------------------------------------------------------------------------
check_tier_ids() {
    local tiers="${ROOT}/.claude/rules/tiers.md"
    if [[ ! -f "${tiers}" ]]; then
        echo "  tiers.md not found"
        return 1
    fi
    local ok=true
    for id in default sol fable; do
        if ! grep -q "\`${id}\`" "${tiers}"; then
            echo "  Missing tier ID: ${id}"
            ok=false
        fi
    done
    ${ok}
}
check "Tier IDs present in tiers.md" check_tier_ids

# --------------------------------------------------------------------------
# 3) Model coherence: every place a model value lives matches the manifest
#    (.claude/skills/_shared/models.json, merged with the project's own
#    .claude/models.local.json). `models.py check` compares the agent
#    frontmatter, .claude/settings.json and .codex/config.toml against it and
#    writes nothing; the process environment is ignored.
#
#    In an installed project (.claude/orchestra-version exists) a value the
#    project chose on purpose is reported as `custom`, not as drift. The
#    template has no project choices, so there the check is strict: a value
#    the manifest does not name, a managed file that is missing or opted out,
#    or a committed local override fails it.
# --------------------------------------------------------------------------
check_model_coherence() {
    local models="${ROOT}/.claude/skills/_shared/models.py"
    if [[ ! -f "${models}" ]]; then
        echo "  .claude/skills/_shared/models.py not found"
        return 1
    fi
    if ! command -v python3 > /dev/null 2>&1; then
        echo "  python3 is required to check the model manifest"
        return 1
    fi

    # The findings are the script's stderr lines; its stdout is one JSON
    # object for programmatic callers and is not needed here.
    local strict=""
    if [[ ! -e "${ROOT}/.claude/orchestra-version" ]]; then
        strict="--strict"
    fi
    local report status=0
    report="$(python3 "${models}" --project-root "${ROOT}" check ${strict:+"${strict}"} \
        2>&1 > /dev/null)" || status=$?
    local line
    while IFS= read -r line; do
        [[ -z "${line}" ]] || echo "  ${line}"
    done <<< "${report}"
    ((status == 0))
}
check "Model coherence" check_model_coherence

# --------------------------------------------------------------------------
# 4) Template-owned runtime subdirectories are listed in SAFE_DIRS
# --------------------------------------------------------------------------
check_safe_dirs() {
    local update="${ROOT}/scripts/update.sh"
    if [[ ! -f "${update}" ]]; then
        echo "  scripts/update.sh not found"
        return 1
    fi
    local dir
    for dir in .claude/rules .claude/skills .claude/agents .claude/hooks; do
        if ! grep -q "\"${dir}\"" "${update}"; then
            echo "  ${dir} not found in SAFE_DIRS"
            return 1
        fi
    done
}
check "Template runtime paths in SAFE_DIRS" check_safe_dirs

# --------------------------------------------------------------------------
# 5) Root AGENTS.md is the complete contract: the main-agent orchestration
#    policy and the CLI-executor contract now live in the one file every
#    runtime auto-loads.
# --------------------------------------------------------------------------
check_root_contract() {
    local contract="${ROOT}/AGENTS.md"
    if [[ ! -f "${contract}" || -L "${contract}" ]]; then
        echo "  AGENTS.md not found, or is a symlink instead of a real file"
        return 1
    fi

    local ok=true
    # Both halves of the merged contract. The main-agent policy sections come
    # first, the CLI-executor sections after; a delegated run loads this file
    # and nothing else, so every section has to be in it.
    local headings=(
        "## Mission"
        "## Non-Goals"
        "## Agent Topology"
        "## Routing Policy"
        "## Skill Catalog"
        "## Execution Patterns"
        "## Context and Document Ownership"
        "## Quality Gates"
        "## Language Protocol"
        "## Native Runtime Boundary"
        "## Required Response Structure"
        "## Handoff Rules"
        "## Cross-CLI Subagent Invocation"
        "## Internal Context References"
        "## Guardrails (Completion Verification)"
    )
    local heading
    for heading in "${headings[@]}"; do
        if ! grep -Fxq "${heading}" "${contract}"; then
            echo "  Missing contract heading: ${heading}"
            ok=false
        fi
    done

    local definition
    for definition in "${ROOT}"/.claude/agents/*.md; do
        local agent_name
        agent_name="$(basename "${definition}" .md)"
        grep -Fq "\`${agent_name}\`" "${contract}" || {
            echo "  Missing agent in AGENTS.md catalog: ${agent_name}"
            ok=false
        }
    done
    for definition in "${ROOT}"/.claude/skills/*/SKILL.md; do
        local skill_name
        skill_name="$(basename "$(dirname "${definition}")")"
        grep -Fq "\`${skill_name}\`" "${contract}" || {
            echo "  Missing skill in AGENTS.md catalog: ${skill_name}"
            ok=false
        }
    done

    # Runtime adapters stay reachable from the one contract every CLI loads.
    local reference
    for reference in ".agents/AGENTS.md" ".codex/AGENTS.md" \
        ".claude/rules/tiers.md"; do
        grep -Fq "${reference}" "${contract}" || {
            echo "  Missing route in root AGENTS.md: ${reference}"
            ok=false
        }
    done

    if [[ -e "${ROOT}/.claude/rules/orchestration.md" ]]; then
        echo "  Duplicate orchestration contract still exists under .claude/rules/"
        ok=false
    fi

    local index_entry
    index_entry=$(grep -F 'Root agent contract' "${ROOT}/.claude/docs/INDEX.md" || true)
    if [[ "${index_entry}" != *"normative"* ]]; then
        echo "  Root agent contract is not registered as normative in INDEX.md"
        ok=false
    fi

    ${ok}
}
check "Root orchestration contract" check_root_contract

# --------------------------------------------------------------------------
# 5b) No CLAUDE.md anywhere: Claude Code reads root AGENTS.md directly ONLY
#     while no CLAUDE.md sits in the working directory or above it. A
#     CLAUDE.md shadows the contract silently -- no warning, no error.
# --------------------------------------------------------------------------
check_no_claude_md() {
    local ok=true
    local shadow
    for shadow in CLAUDE.md .claude/CLAUDE.md CLAUDE.local.md; do
        if [[ -e "${ROOT}/${shadow}" || -L "${ROOT}/${shadow}" ]]; then
            echo "  ${shadow} exists: Claude Code reads it instead of root AGENTS.md"
            echo "  and silently stops loading the contract. Delete it; the merged"
            echo "  main-agent policy belongs in AGENTS.md."
            ok=false
        fi
    done
    ${ok}
}
check "No CLAUDE.md shadows AGENTS.md" check_no_claude_md

# --------------------------------------------------------------------------
# 6) Root instructions stay bounded and carry the always-needed information
# --------------------------------------------------------------------------
check_ordered_references() {
    local file="$1"
    shift
    local previous_line=0
    local reference
    local current_line

    for reference in "$@"; do
        current_line=$(grep -nF -- "${reference}" "${file}" | head -1 | cut -d: -f1 || true)
        if [[ -z "${current_line}" ]]; then
            echo "  Missing reference in ${file#"${ROOT}/"}: ${reference}"
            return 1
        fi
        if ((current_line <= previous_line)); then
            echo "  Out-of-order reference in ${file#"${ROOT}/"}: ${reference}"
            return 1
        fi
        previous_line=${current_line}
    done
}

check_bootstrap_references() {
    local root_agents="${ROOT}/AGENTS.md"
    local ok=true

    # The merged contract carries two roles, so the cap is 400 rather than the
    # 150 that bounded the old router-only file. It is a ceiling, not a target:
    # detail belongs under .claude/rules/.
    if (( $(wc -l < "${root_agents}") > 400 )); then
        echo "  AGENTS.md exceeds 400 lines"
        ok=false
    fi
    local reference
    for reference in ".claude/rules/" ".claude/skills/" ".claude/agents/" \
        ".claude/STATE.md" ".claude/docs/DESIGN.md" ".claude/docs/change_main.md"; do
        grep -Fq "${reference}" "${root_agents}" || {
            echo "  Missing essential root instruction: ${reference}"
            ok=false
        }
    done
    grep -Fq "Japanese" "${root_agents}" || ok=false
    grep -Fqi "verify" "${root_agents}" || ok=false
    if grep -q '@orchestra:' "${root_agents}"; then
        echo "  Legacy boundary marker found in AGENTS.md"
        ok=false
    fi

    ${ok}
}
check "Bootstrap references" check_bootstrap_references

# --------------------------------------------------------------------------
# 7) Runtime layout: .claude/ is the physical source, .agents/ and .codex/
#    carry subagent schema only
# --------------------------------------------------------------------------
check_native_boundaries() {
    local ok=true
    local canonical_dir
    for canonical_dir in rules skills agents hooks docs logs checkpoints; do
        local canonical="${ROOT}/.claude/${canonical_dir}"
        if [[ ! -d "${canonical}" || -L "${canonical}" ]]; then
            echo "  Canonical runtime directory is missing or a symlink: .claude/${canonical_dir}"
            ok=false
        fi
    done

    # The layout is symlink-free: every contract file is a real file so the
    # checkout survives filesystems and CI runners that do not honour symlinks.
    local real_file
    for real_file in AGENTS.md .agents/AGENTS.md .codex/AGENTS.md \
        .claude/STATE.md .claude/rules/tiers.md .claude/docs/INDEX.md \
        .claude/docs/change_main.md; do
        if [[ ! -f "${ROOT}/${real_file}" || -L "${ROOT}/${real_file}" ]]; then
            echo "  Must be a real file, not a symlink or missing: ${real_file}"
            ok=false
        fi
    done

    # Shared runtime content must not be duplicated back into the subagent
    # schema directories.
    local forbidden_path
    for forbidden_path in \
        .agents/rules .agents/skills .agents/agents .agents/hooks \
        .agents/docs .agents/logs .agents/checkpoints .agents/STATE.md \
        .codex/skills; do
        if [[ -e "${ROOT}/${forbidden_path}" || -L "${ROOT}/${forbidden_path}" ]]; then
            echo "  Runtime content duplicated into a subagent schema directory: ${forbidden_path}"
            ok=false
        fi
    done

    check_allowed_entries "${ROOT}/.claude" \
        settings.json settings.local.json settings.orchestra.json orchestra-version \
        models.local.json \
        agents skills rules hooks docs logs checkpoints STATE.md || ok=false
    check_allowed_entries "${ROOT}/.codex" \
        config.toml config.orchestra.toml AGENTS.md || ok=false
    # .agents/ is Antigravity's native directory and holds exactly one thing:
    # the CLI-subagent contract it loads. Shared policy lives under .claude/
    # and is referenced by path.
    check_allowed_entries "${ROOT}/.agents" AGENTS.md || ok=false

    if [[ ! -f "${ROOT}/.claude/settings.json" ]] ||
        ! grep -Fq '.claude/hooks/' "${ROOT}/.claude/settings.json" ||
        grep -Fq '.agents/hooks/' "${ROOT}/.claude/settings.json"; then
        echo "  Claude settings must reference .claude/hooks directly"
        ok=false
    fi
    if [[ ! -f "${ROOT}/.codex/config.toml" ]] ||
        ! grep -Fq '.claude/skills/context-loader' "${ROOT}/.codex/config.toml" ||
        ! grep -Fq '.claude/skills/design-tracker' "${ROOT}/.codex/config.toml" ||
        grep -Fq '.codex/skills/' "${ROOT}/.codex/config.toml"; then
        echo "  Codex config must reference canonical .claude skills directly"
        ok=false
    fi

    ${ok}
}
check "Native runtime boundaries" check_native_boundaries

# --------------------------------------------------------------------------
# 8) Bundled skill scripts and the docs that invoke them stay in sync:
#    every script path named in shared markdown exists, and every bundled
#    script is reachable from at least one document.
# --------------------------------------------------------------------------
check_skill_scripts() {
    local ok=true
    # Generated content is not documentation: run logs, checkpoints, project
    # research, and review notes can quote any path and must not drive this
    # check. Review notes in particular record audit findings and proposals, so
    # they name scripts that do not exist yet by design.
    local -a doc_scope=(
        --include='*.md'
        --exclude-dir=logs
        --exclude-dir=checkpoints
        --exclude-dir=research
        --exclude-dir=reviews
    )
    # README.md is project-owned in an installed project and may not exist;
    # AGENTS.md is template-owned and always scanned, so its absence fails.
    local readme=()
    if [[ -f "${ROOT}/README.md" ]]; then
        readme=("${ROOT}/README.md")
    fi

    # 8a) Every .claude/skills/**.py|.sh path mentioned in shared markdown resolves.
    local referenced
    if ! referenced=$(
        grep_or_empty -rhoE "${doc_scope[@]}" '\.claude/skills/[A-Za-z0-9_/-]+\.(py|sh)' \
            "${ROOT}/.claude" "${ROOT}/.agents" "${ROOT}/.codex" &&
            grep_or_empty -hoE '\.claude/skills/[A-Za-z0-9_/-]+\.(py|sh)' \
                "${ROOT}/AGENTS.md" ${readme[@]+"${readme[@]}"}
    ); then
        echo "  Could not scan the documentation for script references"
        return 1
    fi
    if ! referenced=$(printf '%s\n' "${referenced}" | sort -u); then
        echo "  Could not deduplicate the documented script list"
        return 1
    fi
    if [[ -z "${referenced}" ]]; then
        echo "  No .claude/skills script references found in any documentation"
        return 1
    fi
    local ref
    while IFS= read -r ref; do
        [[ -z "${ref}" ]] && continue
        if [[ ! -f "${ROOT}/${ref}" ]]; then
            echo "  Documented script does not exist: ${ref}"
            ok=false
        fi
    done <<< "${referenced}" || {
        echo "  Could not read the documented script list"
        return 1
    }

    # 8b) Every bundled script is documented somewhere, so orphans surface.
    #     Matching on the bare filename is enough: a script's own directory
    #     README refers to it by name, while callers use the full path.
    local scripts
    if ! scripts=$(find "${ROOT}/.claude/skills" -type f \( -name '*.py' -o -name '*.sh' \) | sort) ||
        [[ -z "${scripts}" ]]; then
        echo "  Could not find any bundled scripts under .claude/skills"
        return 1
    fi
    local script
    while IFS= read -r script; do
        [[ -z "${script}" ]] && continue
        local rel="${script#"${ROOT}/"}"
        if ! grep -rqF "${doc_scope[@]}" "$(basename "${rel}")" \
            "${ROOT}/.claude" "${ROOT}/.agents" ${readme[@]+"${readme[@]}"} 2>/dev/null; then
            echo "  Bundled script is not documented in any markdown: ${rel}"
            ok=false
        fi
    done <<< "${scripts}" || {
        echo "  Could not read the bundled script list"
        return 1
    }

    ${ok}
}
check "Skill scripts and docs in sync" check_skill_scripts

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"

if [[ ${FAIL} -gt 0 ]]; then
    exit 1
fi
exit 0
