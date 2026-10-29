"""Schema for LabRadar.

The governing rule is that scraped facts are immutable observations tied to a
``fetch_run``. Current state is ``valid_to IS NULL``; nothing is updated in
place and nothing is deleted. A professor who leaves the directory is marked
``is_current = False`` and stays queryable, because "who disappeared last week"
is exactly the question the change feed answers.

That history is also what makes change detection cheap: diffing run N against
N-1 needs no separate snapshot store.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


class FetchRun(Base):
    """One execution of one fetch job. Every observation points back to a run."""

    __tablename__ = "fetch_run"

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_fetch_run_source_started", "source", "started_at"),)


class Faculty(Base):
    """Stable identity, keyed on the department's internal person ID.

    Names change and collide; this key does not, which is what lets a rename
    show up as a change event instead of a spurious add plus remove.
    """

    __tablename__ = "faculty"

    id: Mapped[int] = _pk()
    person_id: Mapped[str] = mapped_column(String(32), unique=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    full_name: Mapped[str] = mapped_column(String(256))
    first_seen_run: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    last_seen_run: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

    versions: Mapped[list["FacultyVersion"]] = relationship(back_populates="faculty")
    areas: Mapped[list["FacultyArea"]] = relationship(back_populates="faculty")

    __table_args__ = (Index("ix_faculty_is_current", "is_current"),)


class FacultyVersion(Base):
    """A slowly-changing dimension over the directory's mutable fields.

    A new row is written only when ``content_hash`` differs from the current
    one, so re-running an unchanged fetch adds nothing.
    """

    __tablename__ = "faculty_version"

    id: Mapped[int] = _pk()
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    title: Mapped[str | None] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(256))
    office: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(64))
    homepage_url: Mapped[str | None] = mapped_column(Text)
    scholar_url: Mapped[str | None] = mapped_column(Text)
    cv_url: Mapped[str | None] = mapped_column(Text)
    interests_raw: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    faculty: Mapped[Faculty] = relationship(back_populates="versions")

    __table_args__ = (
        Index(
            "uq_faculty_version_current",
            "faculty_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_faculty_version_history", "faculty_id", "valid_from"),
    )


class ResearchArea(Base):
    """One of the department's canonical areas."""

    __tablename__ = "research_area"

    id: Mapped[int] = _pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(Text)


class FacultyArea(Base):
    """Faculty-to-area membership, historied and attributed to a source.

    ``source`` distinguishes an authoritative area-page listing from a weaker
    inference, so the UI can show which is which instead of flattening both
    into an unqualified claim.
    """

    __tablename__ = "faculty_area"

    id: Mapped[int] = _pk()
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    research_area_id: Mapped[int] = mapped_column(ForeignKey("research_area.id"))
    source: Mapped[str] = mapped_column(String(32))
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    faculty: Mapped[Faculty] = relationship(back_populates="areas")
    area: Mapped[ResearchArea] = relationship()

    __table_args__ = (
        Index(
            "uq_faculty_area_current",
            "faculty_id",
            "research_area_id",
            "source",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index(
            "ix_faculty_area_by_area",
            "research_area_id",
            postgresql_where=text("valid_to IS NULL"),
        ),
    )


class FacultyInterestTerm(Base):
    """Free-text interests, split into terms for search and browsing."""

    __tablename__ = "faculty_interest_term"

    id: Mapped[int] = _pk()
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    term_raw: Mapped[str] = mapped_column(String(256))
    term_norm: Mapped[str] = mapped_column(String(256))
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_interest_term_norm", "term_norm"),
        Index(
            "uq_interest_term_current",
            "faculty_id",
            "term_norm",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
    )


class OpenAlexAuthor(Base):
    __tablename__ = "openalex_author"

    id: Mapped[int] = _pk()
    openalex_id: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    orcid: Mapped[str | None] = mapped_column(String(64))
    last_known_institution: Mapped[str | None] = mapped_column(String(256))


class FacultyOpenAlexLink(Base):
    """The curated faculty-to-OpenAlex mapping. Human-owned, not inferred.

    Many-to-many because OpenAlex routinely splits one researcher across several
    author IDs -- Kathleen McKeown has thirteen Columbia-affiliated profiles, one
    real and twelve single-work fragments. Those fragments all report a current
    last-publication year, so automatic matching on recency would rank a stale
    researcher as maximally active.

    ``rejected`` rows are as important as confirmed ones: they are how a
    dismissed fragment stays dismissed instead of being re-proposed every run.
    """

    __tablename__ = "faculty_openalex_link"

    id: Mapped[int] = _pk()
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    openalex_author_id: Mapped[int] = mapped_column(ForeignKey("openalex_author.id"))
    link_status: Mapped[str] = mapped_column(String(16))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    curated_by: Mapped[str | None] = mapped_column(String(128))
    curated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    author: Mapped[OpenAlexAuthor] = relationship()

    __table_args__ = (
        UniqueConstraint("faculty_id", "openalex_author_id", name="uq_faculty_author"),
        Index(
            "ix_link_confirmed",
            "faculty_id",
            postgresql_where=text("link_status = 'confirmed'"),
        ),
    )


class OpenAlexWork(Base):
    """One OpenAlex work *record* -- not necessarily one paper.

    Preprint, conference and journal versions are separate records with separate
    DOIs and often separate years, which is why there is no global dedup key
    here. Clustering depends on which records are present in a person's corpus,
    so it is derived per run into ``faculty_work_cluster``.
    """

    __tablename__ = "openalex_work"

    id: Mapped[int] = _pk()
    openalex_id: Mapped[str] = mapped_column(String(32), unique=True)
    doi: Mapped[str | None] = mapped_column(String(256))
    title: Mapped[str | None] = mapped_column(Text)
    title_norm: Mapped[str] = mapped_column(Text)
    publication_year: Mapped[int | None] = mapped_column(Integer)
    work_type: Mapped[str | None] = mapped_column(String(64))
    is_preprint: Mapped[bool] = mapped_column(Boolean, default=False)
    publication_date: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_work_title_norm", "title_norm"),
        Index("ix_work_doi", "doi"),
        Index("ix_work_year", "publication_year"),
    )


class OpenAlexAuthorWork(Base):
    """Authorship, with the run bounds that drive the staleness filter.

    ``last_seen_run`` lets ``compute_activity`` count only works still attached
    to the profile this run, so a work OpenAlex later reassigns stops counting
    while its row is retained for history.
    """

    __tablename__ = "openalex_author_work"

    openalex_author_id: Mapped[int] = mapped_column(
        ForeignKey("openalex_author.id"), primary_key=True
    )
    openalex_work_id: Mapped[int] = mapped_column(
        ForeignKey("openalex_work.id"), primary_key=True
    )
    first_seen_run: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    last_seen_run: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))

    __table_args__ = (Index("ix_author_work_last_seen", "last_seen_run"),)


class FacultyWorkCluster(Base):
    """The materialized dedup result: one row per person, run and real paper.

    Storing it makes the counts auditable and lets the UI say "17 papers
    (20 records)" rather than asking the reader to trust a bare number.
    """

    __tablename__ = "faculty_work_cluster"

    id: Mapped[int] = _pk()
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    cluster_key: Mapped[str] = mapped_column(Text)
    canonical_year: Mapped[int | None] = mapped_column(Integer)
    record_count: Mapped[int] = mapped_column(Integer)
    representative_work_id: Mapped[int | None] = mapped_column(
        ForeignKey("openalex_work.id")
    )
    # Chosen across the cluster's records, so the arXiv version's link survives
    # even when the journal version represents the paper.
    url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "faculty_id", "fetch_run_id", "cluster_key", name="uq_cluster_per_run"
        ),
        Index("ix_cluster_lookup", "faculty_id", "fetch_run_id", "canonical_year"),
    )


class OpenAlexAuthorSnapshot(Base):
    """Raw per-run profile observation, kept as a cross-check.

    ``counts_by_year`` here is OpenAlex's own *record* count, which is the
    inflated figure. It is stored unmodified so the deduped numbers can be
    reconciled against the source.
    """

    __tablename__ = "openalex_author_snapshot"

    id: Mapped[int] = _pk()
    openalex_author_id: Mapped[int] = mapped_column(ForeignKey("openalex_author.id"))
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    works_count: Mapped[int | None] = mapped_column(Integer)
    cited_by_count: Mapped[int | None] = mapped_column(Integer)
    counts_by_year: Mapped[dict[str, Any]] = mapped_column(JSONB)
    topics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "openalex_author_id", "fetch_run_id", name="uq_author_snapshot_per_run"
        ),
    )


class FacultyActivity(Base):
    """Derived activity signals, deduped across every confirmed profile.

    These are reported as dated observations, never as an active/inactive
    verdict. ``linked_author_count = 0`` means nobody has mapped this person
    yet, which the UI must render as unknown rather than as silence.
    """

    __tablename__ = "faculty_activity"

    id: Mapped[int] = _pk()
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    as_of_date: Mapped[date] = mapped_column(Date)
    last_pub_year: Mapped[int | None] = mapped_column(Integer)
    works_since: Mapped[int] = mapped_column(Integer, default=0)
    works_total: Mapped[int] = mapped_column(Integer, default=0)
    raw_records_since: Mapped[int | None] = mapped_column(Integer)
    linked_author_count: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("faculty_id", "fetch_run_id", name="uq_activity_per_run"),
        Index("ix_activity_last_pub_year", "last_pub_year"),
        Index("ix_activity_run", "fetch_run_id"),
    )


class ChangeEvent(Base):
    """One difference between consecutive runs.

    Thresholds for what counts as an activity shift live in settings, because a
    too-sensitive rule buries the real departures under weekly indexing noise.
    """

    __tablename__ = "change_event"

    id: Mapped[int] = _pk()
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    prev_fetch_run_id: Mapped[int | None] = mapped_column(ForeignKey("fetch_run.id"))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    change_type: Mapped[str] = mapped_column(String(32))
    field: Mapped[str | None] = mapped_column(String(64))
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_change_run_entity", "fetch_run_id", "entity_type"),
        Index("ix_change_detected", "detected_at"),
    )


class FacultySearch(Base):
    """Search document, rebuilt per run.

    A generated column cannot serve here: the text spans faculty, the current
    version row and the joined area names, and Postgres generated columns must
    depend only on their own row. Rebuilding ~70 rows per run is cheaper and
    more predictable than triggers across three tables.
    """

    __tablename__ = "faculty_search"

    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"), primary_key=True)
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    document: Mapped[Any] = mapped_column(TSVECTOR)

    __table_args__ = (
        Index("ix_faculty_search_document", "document", postgresql_using="gin"),
    )


class Lab(Base):
    """A research group named on a faculty member's own page.

    Labs are the softest layer in the schema. Unlike faculty, which have a
    department-issued person ID, a lab has only the name someone wrote on a web
    page, so the slug is derived from that name and identity is best-effort.
    """

    __tablename__ = "lab"

    id: Mapped[int] = _pk()
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(Text)
    first_seen_run: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    last_seen_run: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (Index("ix_lab_is_current", "is_current"),)


class FacultyLab(Base):
    """Which faculty a lab was found on, and how confident we are.

    Many-to-many in both directions: a lab can list several Columbia faculty,
    and a faculty member can belong to more than one group. ``confidence`` and
    ``method`` travel with the row so the interface can show this as something
    observed on a page rather than an established fact.
    """

    __tablename__ = "faculty_lab"

    id: Mapped[int] = _pk()
    faculty_id: Mapped[int] = mapped_column(ForeignKey("faculty.id"))
    lab_id: Mapped[int] = mapped_column(ForeignKey("lab.id"))
    method: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lab: Mapped[Lab] = relationship()

    __table_args__ = (
        Index(
            "uq_faculty_lab_current",
            "faculty_id",
            "lab_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_faculty_lab_by_lab", "lab_id", postgresql_where=text("valid_to IS NULL")),
    )


class LabActivity(Base):
    """A lab's output, deduplicated across all of its members.

    Publication lists on lab websites proved unusable as a source -- a quarter
    delegate to Google Scholar, a quarter have none, and the rest are bespoke
    and undated. So a lab's activity is the deduplicated union of its members'
    OpenAlex records, which also means a paper co-authored inside the group
    counts once rather than once per member.
    """

    __tablename__ = "lab_activity"

    id: Mapped[int] = _pk()
    lab_id: Mapped[int] = mapped_column(ForeignKey("lab.id"))
    fetch_run_id: Mapped[int] = mapped_column(ForeignKey("fetch_run.id"))
    as_of_date: Mapped[date] = mapped_column(Date)
    last_pub_year: Mapped[int | None] = mapped_column(Integer)
    works_since: Mapped[int] = mapped_column(Integer, default=0)
    works_total: Mapped[int] = mapped_column(Integer, default=0)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    mapped_member_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("lab_id", "fetch_run_id", name="uq_lab_activity_per_run"),
        Index("ix_lab_activity_last_pub", "last_pub_year"),
    )
