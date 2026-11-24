#!/usr/bin/env python3
"""A throwaway repository for looking at trees this checkout must not hold.

Taking upstream in means checking out trees someone else wrote, running the
fork's tools over them, and merging the results. None of that may happen in
the fork's own work tree, index or configuration. This module is where it
happens instead:

* :class:`Repo` is git in one directory with no user or system configuration,
  no inherited ``GIT_*`` variable, no hooks, no replacement objects, and a
  fixed set of merge and line-ending settings.
* :class:`Scratch` is a new repository in a temporary directory that reads
  the fork's objects through ``objects/info/alternates`` and keeps its own.
  Every tree is checked out byte for byte: an attributes file placed above
  every ``.gitattributes`` switches off filters, line-ending conversion and,
  for merges, every ``merge`` attribute. ``merge=union`` alone would turn a
  conflict into a silent mix of both sides.
* :func:`unsupported_entries` names what must not be checked out at all: a
  link, another repository, an attributes file, a name outside a small set of
  characters, a name that collides with another on a case-insensitive file
  system.
* :func:`inert_path` and :func:`inert_text` are what every string from a
  tree, a tool or git goes through before it is reported. The contract of
  what comes out: safe as ONE quoted argument or an argv element, and in
  Markdown prose; never unquoted in a shell. It holds no quote of either
  kind, no backtick, no ``$``, no mention, no reference and no link, but it
  does hold spaces, ``;`` and parentheses. A path is the exception to
  "Markdown prose": whoever posts one puts it in a code span
  (:func:`code_span`), because ``@`` and ``www.`` are legal in a name.

Objects made here stay here. The fork's repository gets one finished commit,
fetched from the scratch repository at the very end.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import common

MIN_GIT = (2, 40)
PLAIN_MODES = ("100644", "100755")
ATTRIBUTES_FILE = ".gitattributes"
NOTE_LIMIT = 400
# The characters a path may be made of. Everything else in a name is either
# shell syntax or Markdown, and a name is what a caller pastes into both.
NAME_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/+@-"
)
NAME_RE = re.compile(r"[A-Za-z0-9._/+@-]+")
OPTION_PREFIX = "-"
# What a sentence may additionally hold. `@` is left out: in prose it is a
# mention.
TEXT_CHARACTERS = (NAME_CHARACTERS - {"@"}) | frozenset(" ,:;()=_")
# Character sequences GitHub turns into a link or a reference although every
# character of them is harmless on its own: sequence -> the character in it
# that is written as an escape.
AUTOLINKS = (
    (re.compile(r"://"), "/"),
    (re.compile(r"(?i)www\."), "."),
    (re.compile(r"(?i)\bgh-(?=\d)"), "-"),
)
# How a line may not start: a list, a quote, a heading underline, a number.
ESCAPED_OR_ANY = re.compile(r"\\(?:x[0-9a-f]{2}|u[0-9a-f]{4})|.", re.DOTALL)
LINE_OPENERS = re.compile(r"^(?:[-+=:.]|\d+[.)])")

RESULT_REFUSED = "refused"
RESULT_GIT = "git-failed"
RESULT_TOOL = "tool-failed"

GIT_ENV_EXTRA = {"GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1"}
GIT_OPTIONS = (
    *common.GIT_OPTIONS,
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
    "-c", "core.excludesFile=/dev/null",
    "-c", "core.protectHFS=true",
    "-c", "core.protectNTFS=true",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "merge.renames=true",
    "-c", "merge.renameLimit=32767",
    "-c", "merge.directoryRenames=conflict",
    "-c", "merge.renormalize=false",
    "-c", "merge.conflictStyle=diff3",
)  # fmt: skip
# Written into the scratch repository's own configuration, so the git that
# the fork's tools start there is held to the same rules.
SCRATCH_CONFIG = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.autocrlf", "false"),
    ("core.attributesFile", "/dev/null"),
    ("core.excludesFile", "/dev/null"),
    ("submodule.recurse", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("protocol.allow", "never"),
)
# Above every `.gitattributes` in precedence. Files are checked out and added
# byte for byte, and no tree chooses how its own conflicts are merged.
SCRATCH_ATTRIBUTES = (
    "* -text -ident -filter !eol !working-tree-encoding !merge !conflict-marker-size\n"
)


class Stop(Exception):
    """The run ends here, with an outcome its command declared."""

    def __init__(self, result: str, exit_code: int, detail: str, **fields: object):
        super().__init__(detail)
        self.result = result
        self.exit_code = exit_code
        self.detail = detail
        self.fields = fields


def shown(text: str) -> str:
    """A string the caller gave (a directory, an argument): printable and
    bounded. Not for anything that came out of a tree; see ``inert_*``."""
    return common.safe_text(text, NOTE_LIMIT)


def _escape(char: str) -> str:
    code = ord(char)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def _only(text: str, allowed: frozenset[str]) -> str:
    """``text`` with every character outside ``allowed`` as an escape. An
    escape that is already there is left as it is, so making a string inert
    twice changes nothing: a line can pass through two reports."""

    def kept(match: re.Match[str]) -> str:
        piece = match.group()
        return piece if len(piece) > 1 or piece in allowed else _escape(piece)

    return ESCAPED_OR_ANY.sub(kept, text)


def inert_path(name: str) -> str:
    """A path as reported: every character outside ``NAME_CHARACTERS`` is a
    visible escape, and so is a leading dash. A supported name comes out as
    it went in; a refused one can still be shown without being acted on.

    Safe as ONE quoted argument or an argv element. In Markdown it must be
    rendered inside a code span by whoever posts it (:func:`code_span`): a
    supported name may be ``www.example.org/@someone``, which prose turns
    into a link and a mention. It can hold no backtick, so a span closes."""
    made = _only(name, NAME_CHARACTERS)
    if made.startswith(OPTION_PREFIX):
        made = _escape(OPTION_PREFIX) + made[1:]
    return made if len(made) <= NOTE_LIMIT else made[: NOTE_LIMIT - 3] + "..."


def code_span(name: str) -> str:
    """A path the way it is written into anything a person reads or posts."""
    return f"`{inert_path(name)}`"


def inert_text(text: str, limit: int = NOTE_LIMIT) -> str:
    """One line of prose from a tool, git, a commit or a note.

    Safe as ONE quoted argument or an argv element, and in Markdown prose;
    never unquoted in a shell. Both kinds of quote, backticks, ``$``, pipes,
    ``#``, ``@`` and brackets become escapes, so neither kind of shell quote
    can be closed from inside and nothing is substituted; link-forming
    sequences are broken, and the line cannot open a list or a quote. Spaces,
    ``;``, ``=`` and parentheses stay: unquoted, the text is still words."""
    made = _only(" ".join(text.split()), TEXT_CHARACTERS)
    for pattern, char in AUTOLINKS:
        made = pattern.sub(
            lambda match, char=char: match.group().replace(char, _escape(char), 1),
            made,
        )
    opener = LINE_OPENERS.match(made)
    if opener is not None:
        last = opener.end() - 1
        made = made[:last] + _escape(made[last]) + made[last + 1 :]
    return made if len(made) <= limit else made[: limit - 3] + "..."


def refuse(detail: str) -> NoReturn:
    raise Stop(RESULT_REFUSED, common.EXIT_USAGE, detail)


def failed(result: str, detail: str) -> NoReturn:
    raise Stop(result, common.EXIT_FAILED, detail)


def child_env() -> dict[str, str]:
    """The environment of every child: nothing that re-aims git."""
    return {**common.child_env(dict(os.environ)), **GIT_ENV_EXTRA}


@dataclass(frozen=True)
class Repo:
    """git in one directory, cut off from the user's configuration."""

    path: Path
    env: dict[str, str]

    def run(
        self,
        *args: str,
        ok: tuple[int, ...] = (0,),
        env: dict[str, str] | None = None,
    ) -> common.CommandResult:
        argv = ["git", *GIT_OPTIONS, "-C", str(self.path), *args]
        result = common.run_command(
            argv, common.GIT_TIMEOUT_SEC, env={**self.env, **(env or {})}
        )
        if result.returncode not in ok:
            failed(RESULT_GIT, f"git {args[0]}: {result.failure()}")
        return result

    def text(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def listed(self, *args: str) -> list[str]:
        """The entries of a NUL-separated listing."""
        return [entry for entry in self.run(*args).stdout.split("\0") if entry]

    def has(self, name: str) -> bool:
        probe = self.run("rev-parse", "--verify", "--quiet", name, ok=(0, 1))
        return probe.returncode == 0

    def blob_at(self, revision: str, rel: str) -> str | None:
        """Mode and object of the plain file ``rel`` at a revision, or None
        when it is anything else there: absent, a link, a directory."""
        entry = self.listed("ls-tree", "-z", "--full-tree", revision, "--", rel)
        if len(entry) != 1:
            return None
        meta, _, name = entry[0].partition("\t")
        mode, kind, oid = meta.split(" ")
        if name != rel or kind != "blob" or mode not in PLAIN_MODES:
            return None
        return f"{mode} {oid}"

    def text_at(self, revision: str, rel: str) -> str | None:
        """A plain file's text at a revision, read as data, or None."""
        if self.blob_at(revision, rel) is None:
            return None
        return self.run("cat-file", "blob", f"{revision}:{rel}").stdout


def require_repository(repo: Repo, full_history: bool = True) -> None:
    """Refuse a directory this cannot work in: not the top of a repository, a
    git too old for the merge, or (where a merge base is needed) a history
    with its beginning cut off."""
    probe = common.run_command(
        ["git", *GIT_OPTIONS, "-C", str(repo.path), "rev-parse", "--show-toplevel"],
        common.GIT_TIMEOUT_SEC,
        env=repo.env,
    )
    top = probe.stdout.rstrip("\n")
    if not probe.ok or not os.path.samefile(top, repo.path):
        refuse(f"{shown(str(repo.path))} is not the top level of a git work tree")
    version = re.search(r"(\d+)\.(\d+)", repo.text("version"))
    if version is None or (int(version[1]), int(version[2])) < MIN_GIT:
        refuse(f"git {MIN_GIT[0]}.{MIN_GIT[1]} or newer is required")
    shallow = repo.text("rev-parse", "--is-shallow-repository") != "false"
    if full_history and shallow:
        refuse("the clone is shallow; run: git fetch --unshallow origin")


def commit_of(repo: Repo, name: str, role: str) -> str:
    """A full commit SHA, exactly as given. A short name is resolved by git
    through tags first, so only the SHA itself is accepted."""
    if common.SHA_RE.fullmatch(name) is None:
        refuse(f"{role} must be a full commit SHA")
    if not repo.has(f"{name}^{{commit}}"):
        refuse(f"{role} {name} is not a commit in this repository")
    return name


@dataclass(frozen=True)
class Scratch:
    """A repository of its own that also reads another one's objects."""

    repo: Repo

    @staticmethod
    def create(path: Path, borrow_from: Repo) -> Scratch:
        objects = borrow_from.text(
            "rev-parse", "--path-format=absolute", "--git-path", "objects"
        )
        if "\n" in objects or objects.startswith(('"', "#")):
            refuse(
                "the object directory of this repository has a name git cannot borrow"
            )
        made = common.run_command(
            ["git", *GIT_OPTIONS, "init", "--quiet", "--template=", str(path)],
            common.GIT_TIMEOUT_SEC,
            env=borrow_from.env,
        )
        if not made.ok:
            failed(RESULT_GIT, f"git init: {made.failure()}")
        git_dir = path / ".git"
        (git_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        alternates = git_dir / "objects" / "info" / "alternates"
        alternates.write_text(objects + "\n", encoding="utf-8")
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "attributes").write_text(
            SCRATCH_ATTRIBUTES, encoding="utf-8"
        )
        repo = Repo(path, borrow_from.env)
        for key, value in SCRATCH_CONFIG:
            repo.run("config", "--local", key, value)
        return Scratch(repo)

    def load(self, tree: str) -> None:
        """Make the work tree exactly ``tree``, and prove it is."""
        self.repo.run("read-tree", "--reset", "-u", tree)
        self.repo.run("clean", "-ffdxq")
        dirty = self.repo.run("diff-files", "--quiet", ok=(0, 1)).returncode != 0
        if dirty or self.repo.listed("ls-files", "--others", "-z"):
            failed(RESULT_GIT, "a tree does not check out byte for byte here")

    def snapshot(self) -> str:
        """The tree the work tree now holds. Only tracked files count: a tool
        that leaves a new file behind did something it is not known to do."""
        self.repo.run("add", "--update")
        strays = self.repo.listed("ls-files", "--others", "-z")
        if strays:
            named = ", ".join(inert_path(name) for name in strays)
            failed(RESULT_TOOL, f"a tool left files behind: {named}")
        return self.repo.text("write-tree")

    def commit(
        self, tree: str, parents: tuple[str, ...], message: str, epoch: str
    ) -> str:
        """A commit of ``tree``. The date is given, so the same inputs make
        the same commit and a second run finds the first one's work."""
        when = f"@{epoch} +0000"
        args = ["commit-tree", tree, "-m", message]
        for parent in parents:
            args += ["-p", parent]
        dates = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        return self.repo.run(*args, env=dates).stdout.strip()

    def branch(self, name: str, commit: str) -> None:
        self.repo.run("update-ref", f"refs/heads/{name}", commit)


def unsupported_entries(repo: Repo, tree: str) -> list[str]:
    """Why ``tree`` must not be checked out, as lines; empty when it may be.

    A link can point outside the scratch directory, another repository is not
    this one's content, and an attributes file chooses filters and merge
    drivers. A name outside ``NAME_CHARACTERS``, or with a component that
    starts with a dash, is shell syntax or an option the moment someone
    pastes it. Two names that differ only in case are one file on macOS, so a
    checkout would silently hold one of them, and an update would write one
    over the other.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}
    for entry in repo.listed("ls-tree", "-r", "-z", "--full-tree", tree):
        meta, _, name = entry.partition("\t")
        mode = meta.split(" ", 1)[0]
        parts = name.split("/")
        if mode not in PLAIN_MODES:
            problems.append(f"{code_span(name)}: mode {mode} is not a plain file")
        if parts[-1] == ATTRIBUTES_FILE:
            problems.append(f"{code_span(name)}: an attributes file")
        if NAME_RE.fullmatch(name) is None:
            problems.append(f"{code_span(name)}: a character outside A-Za-z0-9._/+@-")
        if any(part.startswith(OPTION_PREFIX) for part in parts):
            problems.append(f"{code_span(name)}: a name that starts with a dash")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            if seen.setdefault(prefix.casefold(), prefix) != prefix:
                problems.append(
                    f"{code_span(prefix)}: the same name as "
                    f"{code_span(seen[prefix.casefold()])} "
                    "on a case-insensitive file system"
                )
    return sorted(set(problems))
