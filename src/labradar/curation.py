"""The curated faculty-to-OpenAlex mapping.

This file is the source of truth for who is who. It lives in git rather than in
the database so that every decision has an author, a diff and a review -- the
mapping is a series of human judgements, and judgements should be reviewable.

Automatic matching is not good enough here. Filtering OpenAlex to
Columbia-affiliated authors still returns thirteen candidate profiles for
Kathleen McKeown: one real record of 466 works, and a dozen fragments holding a
single paper each. Every one of those fragments reports a current
last-publication year, so picking the freshest match would rank a researcher who
has stopped publishing as the most active person in the department.

Rejections are recorded alongside confirmations. Without them the suggester
re-proposes the same wrong fragment on every run and the curator re-rejects it
forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import yaml

CONFIRMED = "confirmed"
REJECTED = "rejected"
CANDIDATE = "candidate"
VALID_STATUSES = frozenset({CONFIRMED, REJECTED, CANDIDATE})

DEFAULT_PATH = Path("curation/openalex_links.yaml")


class CurationError(ValueError):
    """Raised for a malformed mapping file, with the offending entry named."""


@dataclass
class Link:
    """One curated decision about one OpenAlex author ID."""

    openalex_id: str
    status: str = CONFIRMED
    is_primary: bool = False
    confidence: float | None = None
    note: str | None = None
    curated_by: str | None = None
    curated_at: datetime | None = None
    evidence: dict[str, Any] | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.status == CONFIRMED


@dataclass
class FacultyLinks:
    """Every decision recorded for one faculty member."""

    person_id: str
    name: str | None = None
    links: list[Link] = field(default_factory=list)

    @property
    def confirmed_ids(self) -> list[str]:
        return [l.openalex_id for l in self.links if l.is_confirmed]

    @property
    def rejected_ids(self) -> set[str]:
        return {l.openalex_id for l in self.links if l.status == REJECTED}

    @property
    def primary_id(self) -> str | None:
        for link in self.links:
            if link.is_confirmed and link.is_primary:
                return link.openalex_id
        confirmed = self.confirmed_ids
        return confirmed[0] if confirmed else None

    @property
    def is_mapped(self) -> bool:
        return bool(self.confirmed_ids)


@dataclass
class CurationFile:
    """The whole mapping, keyed by department person ID."""

    entries: dict[str, FacultyLinks] = field(default_factory=dict)

    def __iter__(self) -> Iterator[FacultyLinks]:
        return iter(self.entries.values())

    def get(self, person_id: str) -> FacultyLinks | None:
        return self.entries.get(person_id)

    def confirmed_ids(self, person_id: str) -> list[str]:
        entry = self.entries.get(person_id)
        return entry.confirmed_ids if entry else []

    @property
    def mapped_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.is_mapped)

    @property
    def unmapped_person_ids(self) -> list[str]:
        return [pid for pid, e in self.entries.items() if not e.is_mapped]

    def all_confirmed(self) -> Iterator[tuple[str, str]]:
        """(person_id, openalex_id) for every confirmed link."""
        for person_id, entry in self.entries.items():
            for openalex_id in entry.confirmed_ids:
                yield person_id, openalex_id


def _parse_link(raw: Any, person_id: str) -> Link:
    # A bare string is shorthand for a confirmed link, which keeps the common
    # case in the file readable.
    if isinstance(raw, str):
        return Link(openalex_id=raw)

    if not isinstance(raw, dict):
        raise CurationError(f"{person_id}: link entries must be a string or mapping")

    openalex_id = raw.get("openalex_id") or raw.get("id")
    if not openalex_id:
        raise CurationError(f"{person_id}: link entry is missing openalex_id")

    status = raw.get("status", CONFIRMED)
    if status not in VALID_STATUSES:
        raise CurationError(
            f"{person_id}/{openalex_id}: unknown status {status!r} "
            f"(expected one of {sorted(VALID_STATUSES)})"
        )

    curated_at = raw.get("curated_at")
    if isinstance(curated_at, str):
        curated_at = datetime.fromisoformat(curated_at)

    return Link(
        openalex_id=str(openalex_id),
        status=status,
        is_primary=bool(raw.get("is_primary", False)),
        confidence=raw.get("confidence"),
        note=raw.get("note"),
        curated_by=raw.get("curated_by"),
        curated_at=curated_at,
        evidence=raw.get("evidence"),
    )


def load(path: Path | str = DEFAULT_PATH) -> CurationFile:
    """Read and validate the mapping file.

    A missing file is an empty mapping rather than an error: before curation
    starts, every faculty member is legitimately unmapped.
    """
    path = Path(path)
    if not path.exists():
        return CurationFile()

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    faculty_block = raw.get("faculty", raw)
    if not isinstance(faculty_block, dict):
        raise CurationError("top-level 'faculty' must be a mapping of person_id -> entry")

    entries: dict[str, FacultyLinks] = {}
    for person_id, entry in faculty_block.items():
        person_id = str(person_id)
        if entry is None:
            entries[person_id] = FacultyLinks(person_id=person_id)
            continue
        if isinstance(entry, list):
            entry = {"links": entry}
        if not isinstance(entry, dict):
            raise CurationError(f"{person_id}: entry must be a mapping or a list")

        links = [_parse_link(l, person_id) for l in entry.get("links") or []]

        confirmed_primaries = [l for l in links if l.is_confirmed and l.is_primary]
        if len(confirmed_primaries) > 1:
            raise CurationError(
                f"{person_id}: {len(confirmed_primaries)} links marked is_primary; "
                "at most one confirmed link may be primary"
            )

        seen: set[str] = set()
        for link in links:
            if link.openalex_id in seen:
                raise CurationError(
                    f"{person_id}: {link.openalex_id} appears more than once"
                )
            seen.add(link.openalex_id)

        entries[person_id] = FacultyLinks(
            person_id=person_id, name=entry.get("name"), links=links
        )

    return CurationFile(entries=entries)


def dump(curation: CurationFile, path: Path | str = DEFAULT_PATH) -> None:
    """Write the mapping back out, preserving the readable shorthand."""
    faculty: dict[str, Any] = {}
    for person_id, entry in sorted(curation.entries.items()):
        links: list[Any] = []
        for link in entry.links:
            body: dict[str, Any] = {"openalex_id": link.openalex_id}
            if link.status != CONFIRMED:
                body["status"] = link.status
            if link.is_primary:
                body["is_primary"] = True
            for key in ("confidence", "note", "curated_by"):
                value = getattr(link, key)
                if value is not None:
                    body[key] = value
            if link.evidence:
                body["evidence"] = link.evidence
            if link.curated_at is not None:
                body["curated_at"] = link.curated_at.isoformat()
            links.append(body)
        faculty[person_id] = {"name": entry.name, "links": links}

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"faculty": faculty}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
