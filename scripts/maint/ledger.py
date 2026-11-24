#!/usr/bin/env python3
"""``state/distribution.json``: which projects the owner adopted, and what the
last run found for every listed one.

The file lives outside every project, is private, and is replaced in one
rename. It is the only proof that a project was adopted, so a file that is
there and cannot be read is never treated as "nothing adopted": it is moved
aside under a dated name (:func:`set_aside`), and the step reports that the
owner must act for as long as such a file exists.

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import common
import targets

LEDGER_FILE = "distribution.json"
ASIDE_MARK = ".unreadable-"
FORMAT_VERSION = 1


class Unreadable(Exception):
    """The record exists and is not one this code wrote."""


@dataclass(frozen=True)
class Adoption:
    """One project the owner allowed the job to maintain.

    ``identity`` is the directory and repository that were adopted.
    ``source_print`` is the approved template at the job's last write there,
    without the per-project model lines; ``target_print`` is the project's
    template-owned content right after that write, every byte of it, so that
    anything the owner changes since is seen as the owner's.
    ``waiting_since`` is when a revision first had to wait for the project and
    is kept until the wait ends, however many revisions arrive meanwhile.
    ``interrupted`` names a revision whose write did not finish.
    ``temporaries`` are the paths, relative to the project, of the files a
    write creates beside the ones it places: recorded before the first of
    them exists, and the only files a later run may delete as its own.
    """

    identity: tuple[int, ...]
    adopted_at: float
    revision: str
    source_print: str
    target_print: str
    updated_at: float
    waiting_revision: str = ""
    waiting_since: float = 0.0
    interrupted: str = ""
    temporaries: tuple[str, ...] = ()


@dataclass(frozen=True)
class Visit:
    """What one run found for one project. ``pending`` and ``since`` are set
    while a revision waits for the project; ``overdue`` once it has waited
    long enough to be the owner's to look at."""

    target: str
    state: str
    detail: str = ""
    pending: str = ""
    since: float = 0.0
    overdue: bool = False


def _adoption_from(raw: object) -> Adoption:
    """An adoption read from the record; ``Unreadable`` when it is not one."""
    if not isinstance(raw, dict):
        raise Unreadable("an adoption is not an object")
    # A record written before temporary files were recorded has none.
    listed = raw.get("temporaries", [])
    if not isinstance(listed, list):
        raise Unreadable("an adoption holds a value of the wrong kind")
    try:
        adoption = Adoption(
            **{**raw, "identity": tuple(raw["identity"]), "temporaries": tuple(listed)}
        )
    except (KeyError, TypeError):
        raise Unreadable("an adoption has other fields than this code writes") from None
    numbers = (
        *adoption.identity,
        adoption.adopted_at,
        adoption.updated_at,
        adoption.waiting_since,
    )
    texts = (
        adoption.revision,
        adoption.source_print,
        adoption.target_print,
        adoption.waiting_revision,
        adoption.interrupted,
        *adoption.temporaries,
    )
    well_formed = (
        len(adoption.identity) == targets.IDENTITY_LENGTH
        and all(type(value) in (int, float) for value in numbers)
        and all(isinstance(value, str) for value in texts)
    )
    if not well_formed:
        raise Unreadable("an adoption holds a value of the wrong kind")
    return adoption


def ledger_path(paths: common.Paths) -> Path:
    return paths.state_dir / LEDGER_FILE


def set_aside_files(paths: common.Paths) -> list[Path]:
    """Records that could not be read and were moved aside, oldest first."""
    return sorted(paths.state_dir.glob(f"{LEDGER_FILE}{ASIDE_MARK}*"))


def set_aside(paths: common.Paths, now: float) -> Path:
    """Move an unreadable record out of the way, keeping it as evidence."""
    aside = paths.state_dir / f"{LEDGER_FILE}{ASIDE_MARK}{common.utc_stamp(now)}"
    os.replace(str(ledger_path(paths)), str(aside))
    return aside


class Ledger:
    """The adoptions and the last run's table. Reading one that exists and is
    not well formed raises :class:`Unreadable` and changes nothing."""

    def __init__(self, paths: common.Paths) -> None:
        self.path = ledger_path(paths)
        self.adoptions: dict[str, Adoption] = {}
        self.last: list[dict[str, object]] = []
        self.last_at = 0.0
        if self.path.is_symlink():
            raise common.Refused(f"{self.path} is a link; it must be a real file")
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            raise Unreadable("the record cannot be read") from None
        try:
            data = json.loads(text)
        except ValueError:
            raise Unreadable("the record is not JSON") from None
        adopted = data.get("adopted") if isinstance(data, dict) else None
        last = data.get("last") if isinstance(data, dict) else None
        last_at = data.get("last_at", 0.0) if isinstance(data, dict) else None
        shaped = (
            isinstance(adopted, dict)
            and isinstance(last, list)
            and type(last_at) in (int, float)
            and all(isinstance(target, str) for target in adopted)
            and all(isinstance(row, dict) for row in last)
        )
        if not shaped:
            raise Unreadable("the record does not have the shape this code writes")
        self.adoptions = {t: _adoption_from(raw) for t, raw in adopted.items()}
        self.last, self.last_at = last, float(last_at)

    def _write(self) -> None:
        body = {
            "version": FORMAT_VERSION,
            "adopted": {target: asdict(a) for target, a in self.adoptions.items()},
            "last": self.last,
            "last_at": self.last_at,
        }
        common.write_text_atomic(self.path, json.dumps(body, indent=2) + "\n")

    def put(self, target: str, adoption: Adoption) -> None:
        """Record one adoption. What is in memory changes only when the file
        did: a later write of this object must not put on disk a mark (an
        interrupted write, say) that the failed one never recorded."""
        previous = self.adoptions
        self.adoptions = {**previous, target: adoption}
        try:
            self._write()
        except BaseException:
            self.adoptions = previous
            raise

    def close(self, visits: list[Visit], now: float) -> None:
        """Record what this run found, and when."""
        self.last = [asdict(visit) for visit in visits]
        self.last_at = now
        self._write()


def status_lines(paths: common.Paths) -> list[str]:
    """The last run's table for ``status``: when it was taken, then one line
    per listed project."""
    asides = [f"  UNREADABLE RECORD set aside: {a}" for a in set_aside_files(paths)]
    return [*asides, *_table_lines(paths)]


def _table_lines(paths: common.Paths) -> list[str]:
    try:
        ledger = Ledger(paths)
    except (common.Refused, Unreadable):
        return ["  targets: the record of adoptions cannot be read"]
    if not ledger.last:
        return []
    lines = [f"  targets as of {common.utc_display(ledger.last_at)}"]
    for row in ledger.last:
        line = f"  target {row.get('state')}  {row.get('target')}"
        since = row.get("since")
        if row.get("pending") and type(since) in (int, float):
            waiting = common.short_sha(row.get("pending"))
            line += f"  waits for {waiting} since {common.utc_display(since)}"
        lines.append(f"{line}  {row.get('detail') or ''}")
    return lines
