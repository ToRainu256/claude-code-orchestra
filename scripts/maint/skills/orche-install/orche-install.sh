#!/bin/bash
# orche-install.sh -- the helper of the owner's /orche-install skill.
#
#   check DIR    can `launchd.sh install-project DIR` be started? Reads only.
#   open DIR     check, then open a new terminal window that runs the approved
#                revision's `launchd.sh install-project DIR` and waits for the
#                owner there
#   verify DIR   after the owner typed yes: is the project installed, listed
#                and adopted? Reads only.
#
# Installed into ~/.claude/skills/orche-install/ by `launchd.sh install` and
# `launchd.sh promote`, from the approved revision; the source is
# scripts/maint/skills/orche-install/ in the fork.
#
# This helper never installs, lists or adopts anything, and never types into
# a terminal. The one confirmation is a `yes` the owner types in the window
# `open` starts; `launchd.sh install-project` refuses any stdin that is not a
# terminal. The window runs a private copy of `launchd.sh` taken out of the
# job's clone as the approved commit holds it (git objects, not a work tree),
# so the bytes that run are the bytes that were approved. The project's path
# never reaches a command line a terminal program would parse: the window
# runs one private script, written here with every argument quoted for bash,
# and the window gets only that script's path.
#
# The checks here are a courtesy, so the window is not opened for nothing.
# `install-project` itself checks everything again, with the approved code.
#
# Portable to bash 3.2. Exit codes: 0 ok; 1 usage; 2 a check failed.
set -euo pipefail

# The system's own tools only: nothing a session's PATH (a project's bin
# directory, a version manager's shims) would find first.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

STATE_ROOT="${HOME}/.claude/orchestra"
CLONE="${STATE_ROOT}/fork"
LAUNCHER="${STATE_ROOT}/bin/orchestra-maint"
APPROVED="${STATE_ROOT}/state/approved-rev"
REGISTRY="${STATE_ROOT}/targets.txt"
LEDGER="${STATE_ROOT}/state/distribution.json"
LAUNCHD_REL="scripts/maint/launchd.sh"
VERSION_STAMP=".claude/orchestra-version"
OPEN=/usr/bin/open
GHOSTTY_APP=/Applications/Ghostty.app
# The tests replace the program that opens a window and where Ghostty lives;
# nothing else does. The two seams are read only under this explicit flag.
if [[ "${ORCHE_INSTALL_TEST_SEAMS:-}" == "1" ]]; then
    OPEN="${ORCHE_INSTALL_OPEN:-${OPEN}}"
    GHOSTTY_APP="${ORCHE_INSTALL_GHOSTTY_APP:-${GHOSTTY_APP}}"
fi
SCRIPT_NAME="install-project.command"
# The window gets the script's path and nothing else; it must need no quoting.
SAFE_PATH_PATTERN='^/[A-Za-z0-9/._-]+$'
# Repository configuration that would make `git status` run a program or read
# another file: a content filter, or an include. Refused before git looks.
UNSAFE_CONFIG_PATTERN='^(filter|include|includeif)\.'
SHA_PATTERN='^[0-9a-f]{40}$'
NOT_ADOPTED=10
EXIT_USAGE=1
EXIT_CHECK=2

# The system's own tools where they are, not whatever a session's PATH finds
# first; and a working directory of nobody's.
GIT=/usr/bin/git
[[ -x "${GIT}" ]] || GIT="$(command -v git)"
PYTHON=/usr/bin/python3
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"
cd /

# git sees nothing of the caller: no inherited GIT_* variable, no user or
# system configuration, no prompt, no lazy fetch, no transport, no
# replacement objects.
for git_variable in $(compgen -A variable GIT_ || true); do
    unset "${git_variable}"
done
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
export GIT_OPTIONAL_LOCKS=0 GIT_NO_LAZY_FETCH=1 GIT_ALLOW_PROTOCOL=none
export GIT_NO_REPLACE_OBJECTS=1

usage() {
    echo "usage: bash orche-install.sh check|open|verify DIR" >&2
    exit "${EXIT_USAGE}"
}

# Text that came from a path, made safe to show: every byte that is not
# printable ASCII becomes `?`.
shown() {
    printf '%s' "$1" | LC_ALL=C tr -c '[:print:]' '?'
}

fail() {
    echo "orche-install: $*" >&2
    exit "${EXIT_CHECK}"
}

# git with nothing of a repository run: no hooks, no file-system monitor, no
# attribute file, no index refresh, nothing done inside a submodule.
hardened_git() {
    "${GIT}" -c core.hooksPath=/dev/null -c core.fsmonitor=false \
        -c core.attributesFile=/dev/null -c diff.autoRefreshIndex=false \
        -c status.submoduleSummary=false -c submodule.recurse=false "$@"
}

project_git() {
    hardened_git -C "${DIR}" "$@"
}

# The approved SHA; fails when none is recorded.
approved_revision() {
    local approved=""
    if [[ -f "${APPROVED}" && ! -L "${APPROVED}" ]]; then
        approved="$(tr -d '[:space:]' < "${APPROVED}")"
    fi
    [[ "${approved}" =~ ${SHA_PATTERN} ]] || return 1
    printf '%s\n' "${approved}"
}

# Print the approved revision's launchd.sh, read from the clone's objects.
approved_launchd() {
    local approved
    approved="$(approved_revision)" || return 1
    [[ ! -L "${CLONE}" && -d "${CLONE}/.git" && ! -L "${CLONE}/.git" ]] || return 1
    hardened_git -C "${CLONE}" cat-file blob "${approved}:${LAUNCHD_REL}" 2> /dev/null
}

# What the record of adoptions says about DIR. Exit 0 it names it, and it
# prints `whole` when that is this very directory and repository, recorded
# the way the job records one, with no write that stopped half-way; exit
# NOT_ADOPTED it does not name it; anything else (a traceback is exit 1), it
# cannot be read. A record that cannot be read is never "nothing adopted".
adopted() {
    [[ -e "${LEDGER}" || -L "${LEDGER}" ]] || return "${NOT_ADOPTED}"
    "${PYTHON}" -I -c '
import json, os, re, sys
path, target, not_adopted = sys.argv[1], sys.argv[2], int(sys.argv[3])
if os.path.islink(path):
    sys.exit(3)
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
adopted = data.get("adopted") if isinstance(data, dict) else None
if not isinstance(adopted, dict) or not isinstance(data.get("last"), list):
    sys.exit(3)
entry = adopted.get(target)
if entry is None:
    sys.exit(not_adopted)
root = os.lstat(target)
repository = os.lstat(os.path.join(target, ".git"))
numbers = [root.st_dev, root.st_ino, repository.st_dev, repository.st_ino]
texts = ("revision", "source_print", "target_print", "interrupted")
times = ("adopted_at", "updated_at")
shaped = (
    isinstance(entry, dict)
    and all(isinstance(entry.get(key), str) for key in texts)
    and all(type(entry.get(key)) in (int, float) for key in times)
    and re.fullmatch("[0-9a-f]{40}", entry["revision"]) is not None
)
if not shaped:
    print("the record of it is not one the job wrote")
elif entry.get("identity") != numbers:
    print("another directory or repository was adopted at this path")
elif entry["interrupted"]:
    print("a write stopped half-way")
else:
    print("whole")
' "${LEDGER}" "${DIR}" "${NOT_ADOPTED}" 2> /dev/null
}

# Whether the registry lists DIR, read the way the job reads it: one path per
# line, white space around it and `#` comments ignored, normalized.
listed() {
    [[ -f "${REGISTRY}" && ! -L "${REGISTRY}" ]] || return 1
    "${PYTHON}" -I -c '
import os, sys
with open(sys.argv[1], "rb") as handle:
    lines = handle.read().split(b"\n")
for raw in lines:
    line = os.fsdecode(raw).strip()
    if line and not line.startswith("#"):
        entry = os.path.normpath(line) if os.path.isabs(line) else line
        if entry == sys.argv[2]:
            sys.exit(0)
sys.exit(1)
' "${REGISTRY}" "${DIR}" 2> /dev/null
}

# DIR as given: an absolute path that is its own real spelling, with no
# control character, so it fits one line of the registry.
require_plain_path() {
    case "${DIR}" in
        /*) ;;
        *) fail "give the project's absolute path" ;;
    esac
    if [[ "${DIR}" == *[[:cntrl:]]* ]]; then
        fail "the path holds a control character: $(shown "${DIR}")"
    fi
    local real
    real="$(CDPATH='' cd -- "${DIR}" 2> /dev/null && pwd -P)" ||
        fail "not a directory: $(shown "${DIR}")"
    if [[ "${real}" != "${DIR}" ]]; then
        fail "$(shown "${DIR}") is a link or not its real spelling; use $(shown "${real}")"
    fi
}

cmd_check() {
    require_plain_path
    [[ -f "${LAUNCHER}" && ! -L "${LAUNCHER}" ]] ||
        fail "the maintenance job is not installed; run: bash scripts/maint/launchd.sh install"
    local launchd
    launchd="$(approved_launchd)" ||
        fail "the approved revision's ${LAUNCHD_REL} cannot be read from ${CLONE}"
    grep -q '^cmd_install_project()' <<< "${launchd}" ||
        fail "the approved revision has no install-project; promote one that has"
    if [[ -L "${DIR}/.git" || ! -d "${DIR}/.git" ]]; then
        fail "$(shown "${DIR}") does not hold its own git repository"
    fi
    local keys top status
    keys="$(project_git config --list --name-only --no-includes 2> /dev/null)" ||
        fail "git cannot read the repository's configuration"
    if grep -Eiq "${UNSAFE_CONFIG_PATTERN}" <<< "${keys}"; then
        fail "the repository's configuration sets a filter or an include; install-project refuses it too"
    fi
    top="$(project_git rev-parse --show-toplevel 2> /dev/null)" ||
        fail "git cannot read the repository at $(shown "${DIR}")"
    [[ "${top}" == "${DIR}" ]] || fail "$(shown "${DIR}") is not the top level of its work tree"
    project_git rev-parse --verify --quiet 'HEAD^{commit}' > /dev/null ||
        fail "the repository has no commit yet"
    status="$(project_git status --porcelain --untracked-files=all --ignore-submodules=all)" ||
        fail "git cannot report on the project"
    if [[ -n "${status}" ]]; then
        fail "the tree is not clean ($(printf '%s\n' "${status}" | wc -l | tr -d ' ') paths); commit or clean it first"
    fi
    if [[ -e "${DIR}/${VERSION_STAMP}" || -L "${DIR}/${VERSION_STAMP}" ]]; then
        fail "the template is already installed there; to let the job update it, run launchd.sh adopt $(shown "${DIR}") in the fork checkout"
    fi
    local found=0
    adopted > /dev/null || found=$?
    case "${found}" in
        0) fail "the project is already adopted; see: bash scripts/maint/launchd.sh status in the fork checkout" ;;
        "${NOT_ADOPTED}") ;;
        *) fail "the record of adoptions (${LEDGER}) cannot be read" ;;
    esac
    # Listed, not adopted and not installed: listed by hand, or by an
    # install-project that never began to write. It is not listed twice.
    if listed; then
        echo "note: already listed in ${REGISTRY}, never adopted; install-project does not list it twice"
    fi
    echo "ok: $(shown "${DIR}") can be installed"
}

cmd_open() {
    cmd_check
    local work script copy terminal
    work="$(mktemp -d "${TMPDIR:-/tmp}/orche-install.XXXXXX")"
    chmod 700 "${work}"
    script="${work}/${SCRIPT_NAME}"
    copy="${work}/launchd.sh"
    if [[ ! "${script}" =~ ${SAFE_PATH_PATTERN} ]]; then
        rm -rf "${work}"
        fail "the temporary directory's path needs quoting; set TMPDIR to a plain path"
    fi
    # The approved bytes, written once into the private directory: the
    # window runs exactly these, whatever happens in the clone meanwhile.
    if ! approved_launchd > "${copy}"; then
        rm -rf "${work}"
        fail "the approved revision's ${LAUNCHD_REL} cannot be read from ${CLONE}"
    fi
    chmod 600 "${copy}"
    # Every value the window runs is quoted for bash here; the window's own
    # command line holds nothing but this script's path.
    {
        echo '#!/bin/bash'
        echo '# Written by the /orche-install skill; runs once and removes itself.'
        echo 'rm -f -- "$0"'
        printf '/bin/bash %q install-project %q\n' "${copy}" "${DIR}"
        echo 'status=$?'
        printf 'rm -f -- %q\n' "${copy}"
        printf 'rmdir -- %q 2> /dev/null || true\n' "${work}"
        echo 'echo'
        echo 'echo "install-project exited ${status}. Press Return to close this window."'
        echo 'read -r _ || true'
        echo 'exit "${status}"'
    } > "${script}"
    chmod 700 "${script}"
    terminal=Terminal
    if [[ -d "${GHOSTTY_APP}" ]]; then
        terminal=Ghostty
        "${OPEN}" -na "${GHOSTTY_APP}" --args -e "${script}" || terminal=""
    else
        "${OPEN}" -a Terminal "${script}" || terminal=""
    fi
    if [[ -z "${terminal}" ]]; then
        rm -rf "${work}"
        fail "no terminal window could be opened; run it yourself: bash scripts/maint/launchd.sh install-project $(shown "${DIR}") in the fork checkout"
    fi
    echo "opened: a ${terminal} window running install-project for $(shown "${DIR}")"
    echo "Type yes there to install; anything else changes nothing."
}

cmd_verify() {
    require_plain_path
    local ok=0 found=0 how=""
    if [[ -f "${DIR}/${VERSION_STAMP}" && ! -L "${DIR}/${VERSION_STAMP}" ]]; then
        echo "installed: template $(shown "$(head -n 1 "${DIR}/${VERSION_STAMP}")")"
    else
        echo "installed: NO (${VERSION_STAMP} is missing)"
        ok=1
    fi
    if listed; then
        echo "listed:    yes, in ${REGISTRY}"
    else
        echo "listed:    NO"
        ok=1
    fi
    how="$(adopted)" || found=$?
    if [[ "${found}" -eq 0 && "${how}" != "whole" ]]; then
        echo "adopted:   NOT WHOLE: ${how}; see launchd.sh status in the fork checkout"
        ok=1
    elif [[ "${found}" -eq 0 ]]; then
        echo "adopted:   yes, in ${LEDGER}"
    elif [[ "${found}" -eq "${NOT_ADOPTED}" ]]; then
        echo "adopted:   NO"
        ok=1
    else
        echo "adopted:   the record cannot be read"
        ok=1
    fi
    [[ "${ok}" -eq 0 ]] || exit "${EXIT_CHECK}"
}

[[ $# -eq 2 ]] || usage
DIR="$2"
case "$1" in
    check) cmd_check ;;
    open) cmd_open ;;
    verify) cmd_verify ;;
    *) usage ;;
esac
