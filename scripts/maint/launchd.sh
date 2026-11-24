#!/bin/bash
# scripts/maint/launchd.sh -- the one command for the fork's local maintenance job.
#
#   print      show the rendered plist and every path; change nothing
#   install    clone, record the approved revision, copy the launcher, load the agent
#   status     loaded? last run, approved vs main, tool and login probes, launcher drift
#   promote    show approved..main, ask, advance the approved revision
#   adopt DIR  show what an update would change in one project, ask, update it,
#              and let scheduled runs keep it updated
#   run-now    start one run now through launchd
#   uninstall  unload the agent, remove the plist and the launcher; keep the rest
#
# Everything is derived from $HOME: the LaunchAgent in ~/Library/LaunchAgents
# and the state directory ~/.claude/orchestra. This script writes nothing into
# this checkout, ~/.codex, ~/.config/gh or a shell profile, and stores no
# token: the job uses the logins `gh` and `codex` already have (those two
# programs keep their own state where they always do).
#
# `install`, `promote` and `adopt` are answered at a terminal; a piped answer is
# refused.
# Text that comes from the fork (commit subjects, path names) is shown with
# every non-printable byte replaced, so it cannot redraw the screen.
#
# Portable to bash 3.2 and to BSD and GNU userland. `print` works anywhere;
# the other subcommands need macOS (launchctl, plutil).
#
# Exit codes: 0 ok; 1 usage, a failed probe, a declined prompt, or not installed.
set -euo pipefail

LABEL="io.github.torainu256.orchestra-maint"
FORK_OWNER="ToRainu256"
FORK_URL="https://github.com/${FORK_OWNER}/claude-code-orchestra.git"
MAIN_REF="refs/remotes/origin/main"
MAIN_REFSPEC="+refs/heads/main:${MAIN_REF}"
OWNERSHIP_KEY="orchestra.maintclone"
CONFIRM_WORD="yes"
JOB_TOOLS="git gh codex python3"
# The wrapper the smoke call goes through needs this interpreter version.
MIN_PYTHON="(3, 11)"
COMMAND_DEADLINE_SEC=120
PROBE_DEADLINE_SEC=60
COMMITS_SHOWN=30
NEWEST_SHOWN=10
STAT_LINES_SHOWN=40
LINE_WIDTH=200

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/orchestra-maint.plist.in"
LAUNCHER_SOURCE="${SCRIPT_DIR}/orchestra-maint"

die() {
    echo "launchd.sh: $*" >&2
    exit 1
}

# launchd expands neither `~` nor variables, so the home directory is written
# into the plist. It also goes through sed and into XML, so only a plain path
# is accepted; anything else is refused rather than escaped.
HOME_PATTERN='^/[A-Za-z0-9._/-]+$'
if [[ ! "${HOME:-}" =~ ${HOME_PATTERN} || "${HOME}" == *--* ]]; then
    die "HOME must be a plain absolute path (letters, digits, . _ - /, no double hyphen)"
fi

BASE="${HOME}/.claude/orchestra"
CLONE="${BASE}/fork"
APPROVED="${BASE}/state/approved-rev"
FAILURE="${BASE}/state/launcher-failure"
LAUNCHER="${BASE}/bin/orchestra-maint"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST="${AGENTS_DIR}/${LABEL}.plist"
# The PATH the job runs with. The probes below use the same one, so `install`
# and `status` see exactly the tools the job will see.
JOB_PATH="${HOME}/.local/share/mise/shims:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SHA_PATTERN='^[0-9a-f]{40}$'

# git sees nothing of the caller: no inherited GIT_* variable, no user or
# system configuration, no prompt. Same rule as the launcher and run.py.
for git_variable in $(compgen -A variable GIT_ || true); do
    unset "${git_variable}"
done
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0

# Every git call: a deadline, no hooks, no filesystem monitor, no attribute
# file, and the one credential helper a private fork would need.
g() {
    with_deadline "${COMMAND_DEADLINE_SEC}" git \
        -c core.hooksPath=/dev/null -c core.fsmonitor=false \
        -c core.attributesFile=/dev/null -c submodule.recurse=false \
        -c advice.detachedHead=false -c credential.helper= \
        -c 'credential.https://github.com.helper=!gh auth git-credential' "$@"
}

# Run a command and end it when it takes longer than SECONDS. macOS has no
# timeout(1). The watchdog's own sleep is ended with it.
with_deadline() {
    local seconds="$1"
    shift
    "$@" &
    local pid=$!
    (
        sleeper=""
        trap '[[ -n "${sleeper}" ]] && kill "${sleeper}" 2>/dev/null; exit 0' TERM
        sleep "${seconds}" &
        sleeper=$!
        wait "${sleeper}"
        kill -TERM "${pid}" 2>/dev/null
    ) > /dev/null 2>&1 &
    local watchdog=$!
    local status=0
    wait "${pid}" || status=$?
    kill -TERM "${watchdog}" 2> /dev/null || true
    wait "${watchdog}" 2> /dev/null || true
    return "${status}"
}

# Text from the fork, made safe to show: every byte that is not printable
# ASCII becomes `?`, and a line longer than LINE_WIDTH is cut and says so.
sanitize() {
    LC_ALL=C tr -c '[:print:]\n' '?' |
        LC_ALL=C awk -v width="${LINE_WIDTH}" '{
            if (length($0) > width) print substr($0, 1, width) " [line cut]"
            else print
        }'
}

# Print at most LIMIT lines of stdin. When there are more, say how many were
# left out and which command shows all of them: nothing is cut silently.
show_limited() {
    local limit="$1" all_of_it="$2" text total
    text="$(cat)"
    [[ -n "${text}" ]] || return 0
    total="$(wc -l <<< "${text}" | tr -d ' ')"
    head -n "${limit}" <<< "${text}"
    if [[ "${total}" -gt "${limit}" ]]; then
        echo "  ($((total - limit)) more lines are not shown; all of them: ${all_of_it})"
    fi
}

render_plist() {
    sed -e "s|@HOME@|${HOME}|g" -e "s|@LABEL@|${LABEL}|g" -e "s|@PATH@|${JOB_PATH}|g" \
        "${TEMPLATE}"
}

# What exists after `install` and after the first run. One list, printed by
# both `print` and `install`. No line may contain two hyphens in a row: `print`
# emits it inside an XML comment.
describe_paths() {
    cat << EOF
Created by install:
  ${PLIST}
      the LaunchAgent: 08:40 and 20:40 local time, not run at load
  ${LAUNCHER}
      copy of scripts/maint/orchestra-maint
  ${CLONE}/
      repository of ${FORK_URL} (remote origin only, default branch only, no tags)
  ${APPROVED}
      the one commit this Mac may execute; shown for review and confirmed at install
Created by runs:
  ${BASE}/logs/maint/              one log per run, newest 60 kept
  ${BASE}/logs/launchd.out.log     what launchd captures
  ${BASE}/logs/launchd.err.log
  ${BASE}/state/last-run.json      summary that status prints
  ${BASE}/state/last-good-rev      last revision that checked out
  ${BASE}/state/bump-records.json  bumps this Mac tested
  ${BASE}/state/distribution.json  projects adopted for updates, and what the last run found
  ${BASE}/state/launcher-failure   present only while the launcher is failing
  ${BASE}/run/                     lock and scratch worktrees
EOF
}

# Run a command line the way the job will: empty environment, the job's PATH.
in_job_env() {
    env -i HOME="${HOME}" PATH="${JOB_PATH}" LANG=en_US.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
        ORCHESTRA_MAINT_HOME="${BASE}" /bin/bash -c "$1"
}

# Print one line per tool the job needs; fail when one is missing or too old.
probe_tools() {
    local ok=0 tool found
    for tool in ${JOB_TOOLS}; do
        if found="$(in_job_env "command -v ${tool}")"; then
            echo "  ${tool}: ${found}"
        else
            echo "  ${tool}: NOT FOUND on the job's PATH (install it, or edit JOB_PATH in launchd.sh)"
            ok=1
        fi
    done
    if ! in_job_env "python3 -c 'import sys; raise SystemExit(sys.version_info < ${MIN_PYTHON})'" \
        > /dev/null 2>&1; then
        echo "  python3: older than ${MIN_PYTHON} on the job's PATH"
        ok=1
    fi
    return "${ok}"
}

# Print the login `gh` has in the job's environment; fail unless it is the owner.
probe_login() {
    local login
    login="$(with_deadline "${PROBE_DEADLINE_SEC}" in_job_env \
        "gh api --hostname github.com user --jq .login" 2> /dev/null | sanitize || true)"
    if [[ "${login}" != "${FORK_OWNER}" ]]; then
        echo "  gh login: '${login}' is not ${FORK_OWNER} (run: gh auth login)"
        return 1
    fi
    echo "  gh login: ${login}"
}

require_macos_tools() {
    local tool
    for tool in launchctl plutil; do
        command -v "${tool}" > /dev/null 2>&1 ||
            die "${tool} not found: this subcommand manages a macOS LaunchAgent"
    done
}

# An approval is typed by a person looking at the screen, never piped.
require_terminal() {
    [[ -t 0 ]] || die "$1 needs a terminal: its confirmation is typed, never piped"
}

domain() {
    echo "gui/$(id -u)"
}

is_loaded() {
    launchctl print "$(domain)/${LABEL}" > /dev/null 2>&1
}

confirm() {
    local answer=""
    printf '%s' "$1"
    read -r answer || true
    [[ "${answer}" == "${CONFIRM_WORD}" ]]
}

read_approved() {
    local rev=""
    if [[ -f "${APPROVED}" && ! -L "${APPROVED}" ]]; then
        rev="$(tr -d '[:space:]' < "${APPROVED}")"
    fi
    if [[ "${rev}" =~ ${SHA_PATTERN} ]]; then
        echo "${rev}"
    fi
}

cmd_print() {
    render_plist
    echo "<!--"
    echo "launchd.sh print: nothing was changed."
    describe_paths
    echo "-->"
}

# Fail unless the clone holds only what the job creates: the branch
# `approved` and the fork's remote-tracking refs. A tag named origin/main
# would otherwise stand in for the branch wherever a short name was used.
require_expected_refs() {
    # A list that cannot be read is not an empty list: the check fails closed.
    local refs ref
    if ! refs="$(g -C "${CLONE}" for-each-ref --format='%(refname)')"; then
        die "cannot list the refs of ${CLONE}"
    fi
    while IFS= read -r ref; do
        case "${ref}" in
            "" | refs/heads/approved | refs/remotes/origin/*) ;;
            *) die "${CLONE} holds a ref the job never creates; list them with: git -C ${CLONE} for-each-ref" ;;
        esac
    done <<< "${refs}"
}

ensure_clone() {
    if [[ -L "${CLONE}" ]]; then
        die "${CLONE} is a link; the job only works in a real directory there"
    fi
    if [[ -d "${CLONE}/.git" && ! -L "${CLONE}/.git" ]]; then
        if [[ "$(g -C "${CLONE}" config --get "${OWNERSHIP_KEY}" || true)" != "true" ]]; then
            die "${CLONE} was not created by launchd.sh install; move it away and retry"
        fi
        if [[ "$(g -C "${CLONE}" remote get-url --all origin || true)" != "${FORK_URL}" ]]; then
            die "${CLONE} is not a clone of ${FORK_URL}; move it away and retry"
        fi
        echo "clone: ${CLONE} exists; fetching"
    elif [[ -e "${CLONE}" ]]; then
        die "${CLONE} exists and is not this job's repository; move it away and retry"
    else
        echo "clone: creating ${CLONE} for ${FORK_URL}"
        # No checkout and no tags: the launcher puts the approved revision
        # there on each run, and only the default branch is ever fetched.
        g init --quiet "${CLONE}"
        g -C "${CLONE}" remote add origin "${FORK_URL}"
        g -C "${CLONE}" config remote.origin.fetch "${MAIN_REFSPEC}"
        g -C "${CLONE}" config remote.origin.tagOpt --no-tags
        g -C "${CLONE}" config "${OWNERSHIP_KEY}" true
    fi
    g -C "${CLONE}" fetch --quiet --no-tags --prune \
        origin "${MAIN_REFSPEC}" || die "cannot fetch ${FORK_URL}; check the network and retry"
    require_expected_refs
}

# Show what is about to be approved, enough to review it: the full SHA and
# date, the commits and changed paths this checkout does not have yet, and
# the command that shows the whole diff.
show_for_review() {
    local rev="$1" reviewed=""
    echo "The fork's default branch (${MAIN_REF}) is at:"
    g -C "${CLONE}" log -1 --format='  %H%n  committed %cI' "${rev}" | sanitize
    reviewed="$(g -C "${SCRIPT_DIR}" rev-parse --verify --quiet 'HEAD^{commit}' 2> /dev/null || true)"
    if [[ "${reviewed}" == "${rev}" ]]; then
        echo "That is exactly the commit this checkout is at."
        echo "Inspect: git -C ${CLONE} show --stat ${rev}"
        return
    fi
    if [[ "${reviewed}" =~ ${SHA_PATTERN} ]] &&
        g -C "${CLONE}" merge-base --is-ancestor "${reviewed}" "${rev}" 2> /dev/null; then
        local count
        count="$(g -C "${CLONE}" rev-list --count "${reviewed}..${rev}")"
        echo "${count} commits are on the fork that this checkout (${reviewed}) does not have:"
        g -C "${CLONE}" log --format='  %h %s' "${reviewed}..${rev}" | sanitize |
            show_limited "${COMMITS_SHOWN}" "git -C ${CLONE} log ${reviewed}..${rev}"
        echo "Changed paths:"
        g -C "${CLONE}" diff --stat=100 "${reviewed}" "${rev}" | sanitize |
            show_limited "${STAT_LINES_SHOWN}" "git -C ${CLONE} diff --stat ${reviewed} ${rev}"
        echo "Inspect: git -C ${CLONE} diff ${reviewed} ${rev}"
        return
    fi
    echo "This checkout's commit is not in the fork's history, so no range can be shown."
    echo "The newest ${NEWEST_SHOWN} commits there:"
    g -C "${CLONE}" log --format='  %h %s' -n "${NEWEST_SHOWN}" "${rev}" | sanitize
    echo "Changed paths in the newest commit:"
    g -C "${CLONE}" show --stat=100 --format= "${rev}" | sanitize |
        show_limited "${STAT_LINES_SHOWN}" "git -C ${CLONE} show --stat ${rev}"
    echo "Inspect: git -C ${CLONE} log --stat ${rev}"
}

ensure_approved() {
    local rev
    rev="$(read_approved)"
    if [[ -n "${rev}" ]]; then
        echo "approved revision: ${rev} (kept)"
        return
    fi
    rev="$(g -C "${CLONE}" rev-parse --verify "${MAIN_REF}^{commit}")"
    [[ "${rev}" =~ ${SHA_PATTERN} ]] || die "cannot read ${MAIN_REF}"
    show_for_review "${rev}"
    if ! confirm "Type ${CONFIRM_WORD} to let this Mac execute ${rev}: "; then
        die "not confirmed; no approved revision was recorded and nothing was loaded"
    fi
    mkdir -p "${BASE}/state"
    printf '%s\n' "${rev}" > "${APPROVED}.tmp.$$"
    mv -f "${APPROVED}.tmp.$$" "${APPROVED}"
    echo "approved revision: ${rev} (recorded)"
}

install_launcher() {
    mkdir -p "${BASE}/bin"
    cp "${LAUNCHER_SOURCE}" "${LAUNCHER}.tmp.$$"
    chmod 700 "${LAUNCHER}.tmp.$$"
    mv -f "${LAUNCHER}.tmp.$$" "${LAUNCHER}"
    echo "launcher: ${LAUNCHER}"
}

install_agent() {
    local staged="${PLIST}.tmp.$$"
    mkdir -p "${AGENTS_DIR}"
    render_plist > "${staged}"
    if ! plutil -lint "${staged}" > /dev/null; then
        rm -f "${staged}"
        die "the rendered plist does not pass plutil -lint"
    fi
    if [[ -f "${PLIST}" ]] && cmp -s "${staged}" "${PLIST}" && is_loaded; then
        rm -f "${staged}"
        echo "agent: ${LABEL} already loaded with this plist"
        return
    fi
    mv -f "${staged}" "${PLIST}"
    if is_loaded; then
        launchctl bootout "$(domain)/${LABEL}"
    fi
    launchctl bootstrap "$(domain)" "${PLIST}"
    echo "agent: ${LABEL} loaded from ${PLIST}"
}

cmd_install() {
    require_macos_tools
    require_terminal install
    echo "launchd.sh install will:"
    describe_paths
    echo "Probing the job's environment (PATH=${JOB_PATH}):"
    # Both probes run before anything is created.
    local probes=0
    probe_tools || probes=1
    probe_login || probes=1
    if [[ "${probes}" -ne 0 ]]; then
        die "a probe failed; nothing was changed"
    fi
    if ! confirm "Type ${CONFIRM_WORD} to install: "; then
        die "not confirmed; nothing was changed"
    fi
    umask 077
    mkdir -p "${BASE}"
    ensure_clone
    ensure_approved
    install_launcher
    install_agent
    echo "Installed. Next: bash scripts/maint/launchd.sh run-now, then status."
}

cmd_uninstall() {
    require_macos_tools
    if is_loaded; then
        launchctl bootout "$(domain)/${LABEL}"
        echo "agent: ${LABEL} unloaded"
    else
        echo "agent: ${LABEL} was not loaded"
    fi
    rm -f "${PLIST}"
    echo "removed: ${PLIST}"
    rm -f "${LAUNCHER}"
    rmdir "${BASE}/bin" 2> /dev/null || true
    echo "removed: ${LAUNCHER}"
    echo "Kept (delete by hand if you want them gone):"
    local kept
    for kept in "${CLONE}" "${BASE}/state" "${BASE}/logs" "${BASE}/run"; do
        if [[ -e "${kept}" ]]; then
            echo "  ${kept}"
        fi
    done
}

cmd_status() {
    require_macos_tools
    if [[ -f "${FAILURE}" && ! -L "${FAILURE}" ]]; then
        echo "LAUNCHER FAILURE: the job is not running normally. Everything below"
        echo "this line about the last run is from before the failure."
        sanitize < "${FAILURE}"
    fi
    if is_loaded; then
        echo "agent: ${LABEL} loaded"
        launchctl print "$(domain)/${LABEL}" 2> /dev/null |
            grep -E '^[[:space:]]*(state|last exit code|runs) = ' || true
    else
        echo "agent: ${LABEL} NOT loaded"
    fi
    if [[ ! -f "${PLIST}" ]]; then
        echo "plist: not installed"
    elif render_plist | cmp -s - "${PLIST}"; then
        echo "plist: matches this checkout"
    else
        echo "plist: DIFFERS from this checkout; run launchd.sh install"
    fi
    # The launcher is the one piece that does not update itself.
    if [[ ! -f "${LAUNCHER}" ]]; then
        echo "launcher: not installed"
    elif cmp -s "${LAUNCHER_SOURCE}" "${LAUNCHER}"; then
        echo "launcher: matches this checkout"
    else
        echo "launcher: DIFFERS from scripts/maint/orchestra-maint; run launchd.sh install"
    fi
    echo "job environment (PATH=${JOB_PATH}):"
    probe_tools || true
    probe_login || true

    local rev head=""
    rev="$(read_approved)"
    if [[ -z "${rev}" || ! -d "${CLONE}/.git" || -L "${CLONE}" || -L "${CLONE}/.git" ]]; then
        echo "approved revision: none recorded; run launchd.sh install"
        return 0
    fi
    head="$(g -C "${CLONE}" rev-parse --verify --quiet 'HEAD^{commit}' || true)"
    if [[ "${head}" != "${rev}" || ! -f "${CLONE}/scripts/maint/run.py" ]]; then
        # The work tree is not at the approved revision (no run since the
        # last promotion), so none of its code is run here.
        echo "approved revision: ${rev}"
        echo "last run: the clone is not at the approved revision yet; run-now first"
        return 0
    fi
    in_job_env "python3 '${CLONE}/scripts/maint/run.py' status" || true
}

cmd_promote() {
    require_terminal promote
    [[ -f "${LAUNCHER}" && ! -L "${LAUNCHER}" ]] || die "not installed; run launchd.sh install"
    # Through the launcher: it fetches, checks out the approved revision, and
    # that revision's code shows the range and asks.
    ORCHESTRA_MAINT_HOME="${BASE}" PATH="${JOB_PATH}" exec /bin/bash "${LAUNCHER}" promote
}

cmd_adopt() {
    require_terminal adopt
    [[ -f "${LAUNCHER}" && ! -L "${LAUNCHER}" ]] || die "not installed; run launchd.sh install"
    # The job changes its working directory, so the project is named by its
    # real path. Through the launcher, like promote: the approved revision's
    # code shows the change and asks.
    local target
    target="$(CDPATH='' cd -- "$1" 2> /dev/null && pwd -P)" ||
        die "adopt: not a directory: $(printf '%s' "$1" | sanitize)"
    ORCHESTRA_MAINT_HOME="${BASE}" PATH="${JOB_PATH}" exec /bin/bash "${LAUNCHER}" adopt "${target}"
}

cmd_run_now() {
    require_macos_tools
    is_loaded || die "${LABEL} is not loaded; run launchd.sh install"
    launchctl kickstart "$(domain)/${LABEL}"
    echo "started: ${LABEL}; see launchd.sh status"
}

usage() {
    echo "usage: bash scripts/maint/launchd.sh print|install|status|promote|adopt DIR|run-now|uninstall" >&2
    exit 1
}

# `adopt` names one project; every other subcommand stands alone.
case "${1:-}" in
    adopt) [[ $# -eq 2 ]] || usage ;;
    *) [[ $# -eq 1 ]] || usage ;;
esac
case "$1" in
    print) cmd_print ;;
    install) cmd_install ;;
    status) cmd_status ;;
    promote) cmd_promote ;;
    adopt) cmd_adopt "$2" ;;
    run-now) cmd_run_now ;;
    uninstall) cmd_uninstall ;;
    *) usage ;;
esac
