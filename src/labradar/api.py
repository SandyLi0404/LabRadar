"""HTTP API and static frontend host.

Every activity number leaves here with the date it was observed attached. The
API exposes filters and never a verdict: there is no ``active`` field, because
where to draw that line is the reader's decision, not ours.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, func, select, text
from sqlalchemy.orm import Session

from labradar import students
from labradar.activity import since_year as activity_since_year
from labradar.config import get_settings
from labradar.db.models import (
    ChangeEvent,
    FacultyLab,
    Lab,
    LabActivity,
    Faculty,
    FacultyActivity,
    FacultyArea,
    FacultyOpenAlexLink,
    FacultySearch,
    FacultyVersion,
    FacultyWorkCluster,
    FetchRun,
    OpenAlexAuthor,
    OpenAlexWork,
    ResearchArea,
)
from labradar.db.session import get_db


def _resolve_frontend_dir() -> Path | None:
    """Locate the static frontend across the layouts this runs in.

    Under `pip install -e .` the package sits in the repo, so the frontend is
    two levels up. Inside the image it is installed into site-packages and that
    relative walk lands in the Python prefix instead, which is how the container
    ended up serving the API with no index page.
    """
    settings = get_settings()
    candidates = [
        settings.frontend_dir,
        Path(__file__).resolve().parents[2] / "frontend",  # editable / src layout
        Path("/app/frontend"),  # container image
        Path.cwd() / "frontend",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)

    # Silence here is what made the original break hard to place: the API came
    # up healthy and only "/" was missing.
    logging.getLogger(__name__).warning(
        "frontend directory not found; the API will run without a UI. Looked in: %s. "
        "Set FRONTEND_DIR to override.",
        ", ".join(str(c) for c in candidates if c),
    )
    return None


FRONTEND_DIR = _resolve_frontend_dir()

app = FastAPI(
    title="LabRadar",
    description=(
        "Research-group activity for the Columbia CS department. Activity "
        "signals are dated observations, not claims about whether a group is "
        "alive."
    ),
    version="0.1.0",
)


class AreaOut(BaseModel):
    id: int
    slug: str
    name: str
    url: str | None = None
    faculty_count: int = 0


class ActivityOut(BaseModel):
    """Signals, always carrying the date they were observed."""

    as_of_date: date
    since_year: int = Field(
        description="Earliest publication year the count covers; render labels from this."
    )
    last_pub_year: int | None
    works_since: int
    works_total: int
    raw_records_since: int | None
    linked_author_count: int
    is_unmapped: bool = Field(
        description="No curated publication record yet; counts mean 'unknown', not 'none'."
    )
    is_inactive: bool = Field(
        default=False,
        description="Mapped, but nothing published since since_year.",
    )
    years_since_last_publication: int | None = None


class LabRef(BaseModel):
    """A lab as it appears on a faculty card.

    ``confidence`` and ``source_url`` travel with it because this is read off a
    personal web page, not a department record.
    """

    slug: str
    name: str
    url: str | None = None
    confidence: float
    source_url: str | None = None


class LabOut(BaseModel):
    id: int
    slug: str
    name: str
    url: str | None = None
    member_names: list[str] = []
    activity: "LabActivityOut | None" = None


class LabActivityOut(BaseModel):
    """A lab's combined output, deduplicated across its members."""

    as_of_date: date
    since_year: int
    last_pub_year: int | None
    works_since: int
    works_total: int
    member_count: int
    mapped_member_count: int
    is_unmapped: bool = Field(
        description="No member has a linked publication record yet."
    )


class PaperOut(BaseModel):
    """One deduplicated paper: the published version's title, and a link a
    reader can open (arXiv when any version has one)."""

    title: str
    year: int | None = None
    url: str | None = None


class StudentsOut(BaseModel):
    label: str
    names: list[str]
    source_url: str
    checked: str


def _students_out(person_id: str) -> StudentsOut | None:
    entry = students.cached().get(person_id)
    if entry is None:
        return None
    return StudentsOut(
        label=entry.label,
        names=list(entry.names),
        source_url=entry.source_url,
        checked=entry.checked,
    )


class FacultyOut(BaseModel):
    id: int
    person_id: str
    slug: str
    full_name: str
    title: str | None = None
    email: str | None = None
    homepage_url: str | None = None
    scholar_url: str | None = None
    interests_raw: str | None = None
    is_current: bool = True
    areas: list[str] = []
    labs: list[LabRef] = []
    activity: ActivityOut | None = None
    recent_papers: list[PaperOut] = []
    students: StudentsOut | None = None


class FacultyDetailOut(FacultyOut):
    office: str | None = None
    phone: str | None = None
    cv_url: str | None = None
    openalex_ids: list[str] = []


class PageOut(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[FacultyOut]


class ChangeOut(BaseModel):
    id: int
    entity_type: str
    entity_id: int | None
    change_type: str
    field: str | None
    old_value: Any = None
    new_value: Any = None
    detected_at: str
    faculty_name: str | None = None


class RunOut(BaseModel):
    id: int
    source: str
    status: str
    started_at: str
    finished_at: str | None
    stats: dict[str, Any] | None = None
    error: str | None = None


def latest_activity_run(session: Session) -> int | None:
    return session.scalar(
        select(func.max(FetchRun.id)).where(
            FetchRun.source == "compute_activity", FetchRun.status == "success"
        )
    )


def _activity_out(row) -> ActivityOut | None:
    if row is None or row.as_of_date is None:
        return None
    unmapped = (row.linked_author_count or 0) == 0
    return ActivityOut(
        as_of_date=row.as_of_date,
        since_year=activity_since_year(row.as_of_date),
        last_pub_year=row.last_pub_year,
        works_since=row.works_since or 0,
        works_total=row.works_total or 0,
        raw_records_since=row.raw_records_since,
        linked_author_count=row.linked_author_count or 0,
        is_unmapped=unmapped,
        is_inactive=not unmapped and (row.works_since or 0) == 0,
        years_since_last_publication=(
            row.as_of_date.year - row.last_pub_year if row.last_pub_year else None
        ),
    )


# Shown on every card: enough to see what someone works on now.
CARD_PAPERS = 2


def _recent_papers_for(
    session: Session, faculty_ids: list[int], run_id: int | None, limit: int
) -> dict[int, list[PaperOut]]:
    """Each person's newest deduplicated papers from one activity run.

    Ordered by the paper's canonical year, then the published version's date
    within that year. Untitled records are skipped: there is nothing to show.
    """
    if not faculty_ids or run_id is None:
        return {}
    rank = func.row_number().over(
        partition_by=FacultyWorkCluster.faculty_id,
        order_by=(
            FacultyWorkCluster.canonical_year.desc().nullslast(),
            OpenAlexWork.publication_date.desc().nullslast(),
            FacultyWorkCluster.id,
        ),
    )
    ranked = (
        select(
            FacultyWorkCluster.faculty_id,
            OpenAlexWork.title,
            FacultyWorkCluster.canonical_year,
            FacultyWorkCluster.url,
            rank.label("rank"),
        )
        .join(OpenAlexWork, OpenAlexWork.id == FacultyWorkCluster.representative_work_id)
        .where(
            FacultyWorkCluster.faculty_id.in_(faculty_ids),
            FacultyWorkCluster.fetch_run_id == run_id,
            OpenAlexWork.title.isnot(None),
        )
        .subquery()
    )
    out: dict[int, list[PaperOut]] = {}
    for r in session.execute(
        select(ranked).where(ranked.c.rank <= limit).order_by(ranked.c.faculty_id, ranked.c.rank)
    ):
        out.setdefault(r.faculty_id, []).append(
            PaperOut(title=r.title, year=r.canonical_year, url=r.url)
        )
    return out


def _labs_for(session: Session, faculty_ids: list[int]) -> dict[int, list[LabRef]]:
    """Labs named on each faculty member's own page."""
    if not faculty_ids:
        return {}
    rows = session.execute(
        select(
            FacultyLab.faculty_id,
            Lab.slug,
            Lab.name,
            Lab.url,
            FacultyLab.confidence,
            FacultyLab.source_url,
        )
        .join(Lab, Lab.id == FacultyLab.lab_id)
        .where(FacultyLab.faculty_id.in_(faculty_ids), FacultyLab.valid_to.is_(None))
        .order_by(FacultyLab.confidence.desc())
    ).all()
    out: dict[int, list[LabRef]] = {}
    for r in rows:
        out.setdefault(r.faculty_id, []).append(
            LabRef(
                slug=r.slug,
                name=r.name,
                url=r.url,
                confidence=r.confidence,
                source_url=r.source_url,
            )
        )
    return out


def _areas_for(session: Session, faculty_ids: list[int]) -> dict[int, list[str]]:
    if not faculty_ids:
        return {}
    rows = session.execute(
        select(FacultyArea.faculty_id, ResearchArea.name)
        .join(ResearchArea, ResearchArea.id == FacultyArea.research_area_id)
        .where(
            FacultyArea.faculty_id.in_(faculty_ids),
            FacultyArea.valid_to.is_(None),
        )
        .order_by(ResearchArea.name)
    ).all()
    out: dict[int, list[str]] = {}
    for faculty_id, name in rows:
        out.setdefault(faculty_id, []).append(name)
    return out


@app.get("/api/areas", response_model=list[AreaOut])
def list_areas(db: Session = Depends(get_db)) -> list[AreaOut]:
    counts = dict(
        db.execute(
            select(FacultyArea.research_area_id, func.count(func.distinct(FacultyArea.faculty_id)))
            .join(Faculty, Faculty.id == FacultyArea.faculty_id)
            .where(FacultyArea.valid_to.is_(None), Faculty.is_current)
            .group_by(FacultyArea.research_area_id)
        ).all()
    )
    return [
        AreaOut(
            id=a.id,
            slug=a.slug,
            name=a.name,
            url=a.url,
            faculty_count=counts.get(a.id, 0),
        )
        for a in db.scalars(select(ResearchArea).order_by(ResearchArea.name))
    ]


@app.get("/api/faculty", response_model=PageOut)
def list_faculty(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="full-text search"),
    area: str | None = Query(None, description="research area slug"),
    include_inactive: bool = Query(
        False,
        description=(
            "Show professors with nothing published since the cutoff year. Someone "
            "with no linked publication record is never counted as inactive -- "
            "that is unknown, not silent."
        ),
    ),
    include_no_website: bool = Query(
        False, description="Show professors with no personal website listed."
    ),
    include_unmapped: bool = Query(
        True,
        description=(
            "Keep people with no curated publication record. They are unknown, "
            "not inactive, so they are included by default."
        ),
    ),
    include_departed: bool = Query(False, description="include people who left"),
    sort: Literal["name", "last_pub_year", "works_since"] = "name",
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
) -> PageOut:
    settings = get_settings()
    page_size = min(page_size or settings.api_page_size, settings.api_max_page_size)
    run_id = latest_activity_run(db)

    stmt = (
        select(
            Faculty.id,
            Faculty.person_id,
            Faculty.slug,
            Faculty.full_name,
            Faculty.is_current,
            FacultyVersion.title,
            FacultyVersion.email,
            FacultyVersion.homepage_url,
            FacultyVersion.scholar_url,
            FacultyVersion.interests_raw,
            FacultyActivity.as_of_date,
            FacultyActivity.last_pub_year,
            FacultyActivity.works_since,
            FacultyActivity.works_total,
            FacultyActivity.raw_records_since,
            FacultyActivity.linked_author_count,
        )
        .outerjoin(
            FacultyVersion,
            (FacultyVersion.faculty_id == Faculty.id)
            & (FacultyVersion.valid_to.is_(None)),
        )
        .outerjoin(
            FacultyActivity,
            (FacultyActivity.faculty_id == Faculty.id)
            & (FacultyActivity.fetch_run_id == run_id),
        )
    )

    if not include_departed:
        stmt = stmt.where(Faculty.is_current)

    if not include_no_website:
        stmt = stmt.where(FacultyVersion.homepage_url.isnot(None))

    if area:
        stmt = stmt.join(
            FacultyArea,
            (FacultyArea.faculty_id == Faculty.id) & (FacultyArea.valid_to.is_(None)),
        ).join(
            ResearchArea,
            (ResearchArea.id == FacultyArea.research_area_id)
            & (ResearchArea.slug == area),
        )

    if q:
        stmt = stmt.join(FacultySearch, FacultySearch.faculty_id == Faculty.id).where(
            FacultySearch.document.op("@@")(func.websearch_to_tsquery("english", q))
        )

    # Someone with no linked record is unknown, never inactive, so the
    # inactivity filter must never reach them.
    unmapped = FacultyActivity.linked_author_count.is_(None) | (
        FacultyActivity.linked_author_count == 0
    )
    if not include_inactive:
        stmt = stmt.where((FacultyActivity.works_since > 0) | unmapped)
    if not include_unmapped:
        stmt = stmt.where(~unmapped)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    order = {
        "name": Faculty.full_name.asc(),
        "last_pub_year": FacultyActivity.last_pub_year.desc().nullslast(),
        "works_since": FacultyActivity.works_since.desc().nullslast(),
    }[sort]
    rows = db.execute(
        stmt.order_by(order, Faculty.full_name.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    ids = [r.id for r in rows]
    areas = _areas_for(db, ids)
    labs = _labs_for(db, ids)
    papers = _recent_papers_for(db, ids, run_id, limit=CARD_PAPERS)
    return PageOut(
        total=total,
        page=page,
        page_size=page_size,
        results=[
            FacultyOut(
                id=r.id,
                person_id=r.person_id,
                slug=r.slug,
                full_name=r.full_name,
                title=r.title,
                email=r.email,
                homepage_url=r.homepage_url,
                scholar_url=r.scholar_url,
                interests_raw=r.interests_raw,
                is_current=r.is_current,
                areas=areas.get(r.id, []),
                labs=labs.get(r.id, []),
                activity=_activity_out(r),
                recent_papers=papers.get(r.id, []),
                students=_students_out(r.person_id),
            )
            for r in rows
        ],
    )


@app.get("/api/faculty/{slug}", response_model=FacultyDetailOut)
def get_faculty(slug: str, db: Session = Depends(get_db)) -> FacultyDetailOut:
    run_id = latest_activity_run(db)
    row = db.execute(
        select(
            Faculty.id,
            Faculty.person_id,
            Faculty.slug,
            Faculty.full_name,
            Faculty.is_current,
            FacultyVersion.title,
            FacultyVersion.email,
            FacultyVersion.office,
            FacultyVersion.phone,
            FacultyVersion.homepage_url,
            FacultyVersion.scholar_url,
            FacultyVersion.cv_url,
            FacultyVersion.interests_raw,
            FacultyActivity.as_of_date,
            FacultyActivity.last_pub_year,
            FacultyActivity.works_since,
            FacultyActivity.works_total,
            FacultyActivity.raw_records_since,
            FacultyActivity.linked_author_count,
        )
        .outerjoin(
            FacultyVersion,
            (FacultyVersion.faculty_id == Faculty.id)
            & (FacultyVersion.valid_to.is_(None)),
        )
        .outerjoin(
            FacultyActivity,
            (FacultyActivity.faculty_id == Faculty.id)
            & (FacultyActivity.fetch_run_id == run_id),
        )
        .where(Faculty.slug == slug)
    ).first()

    if row is None:
        raise HTTPException(status_code=404, detail=f"no faculty with slug {slug!r}")

    openalex_ids = list(
        db.scalars(
            select(OpenAlexAuthor.openalex_id)
            .join(
                FacultyOpenAlexLink,
                FacultyOpenAlexLink.openalex_author_id == OpenAlexAuthor.id,
            )
            .where(
                FacultyOpenAlexLink.faculty_id == row.id,
                FacultyOpenAlexLink.link_status == "confirmed",
            )
        )
    )

    papers = _recent_papers_for(db, [row.id], run_id, limit=20).get(row.id, [])

    return FacultyDetailOut(
        id=row.id,
        person_id=row.person_id,
        slug=row.slug,
        full_name=row.full_name,
        title=row.title,
        email=row.email,
        office=row.office,
        phone=row.phone,
        homepage_url=row.homepage_url,
        scholar_url=row.scholar_url,
        cv_url=row.cv_url,
        interests_raw=row.interests_raw,
        is_current=row.is_current,
        areas=_areas_for(db, [row.id]).get(row.id, []),
        labs=_labs_for(db, [row.id]).get(row.id, []),
        activity=_activity_out(row),
        openalex_ids=openalex_ids,
        recent_papers=papers,
        students=_students_out(row.person_id),
    )


@app.get("/api/labs", response_model=list[LabOut])
def list_labs(
    db: Session = Depends(get_db),
    include_inactive: bool = Query(
        False, description="Show groups with nothing published since the cutoff year."
    ),
    include_unmapped: bool = Query(True),
    sort: Literal["works_since", "name", "last_pub_year"] = "works_since",
    limit: int = Query(100, ge=1, le=500),
) -> list[LabOut]:
    """Research groups, with output aggregated across their members.

    A lab's figures are the deduplicated union of its members' papers, so a
    paper co-authored inside the group counts once for the group.
    """
    run_id = latest_activity_run(db)
    stmt = (
        select(Lab, LabActivity)
        .outerjoin(
            LabActivity,
            (LabActivity.lab_id == Lab.id) & (LabActivity.fetch_run_id == run_id),
        )
        .where(Lab.is_current)
    )

    unmapped = LabActivity.mapped_member_count.is_(None) | (
        LabActivity.mapped_member_count == 0
    )
    if not include_inactive:
        stmt = stmt.where((LabActivity.works_since > 0) | unmapped)
    if not include_unmapped:
        stmt = stmt.where(~unmapped)

    order = {
        "works_since": LabActivity.works_since.desc().nullslast(),
        "last_pub_year": LabActivity.last_pub_year.desc().nullslast(),
        "name": Lab.name.asc(),
    }[sort]
    rows = db.execute(stmt.order_by(order, Lab.name.asc()).limit(limit)).all()

    members: dict[int, list[str]] = {}
    for lab_id, name in db.execute(
        select(FacultyLab.lab_id, Faculty.full_name)
        .join(Faculty, Faculty.id == FacultyLab.faculty_id)
        .where(FacultyLab.valid_to.is_(None))
        .order_by(Faculty.full_name)
    ).all():
        members.setdefault(lab_id, []).append(name)

    return [
        LabOut(
            id=lab.id,
            slug=lab.slug,
            name=lab.name,
            url=lab.url,
            member_names=members.get(lab.id, []),
            activity=(
                LabActivityOut(
                    as_of_date=act.as_of_date,
                    since_year=activity_since_year(act.as_of_date),
                    last_pub_year=act.last_pub_year,
                    works_since=act.works_since,
                    works_total=act.works_total,
                    member_count=act.member_count,
                    mapped_member_count=act.mapped_member_count,
                    is_unmapped=(act.mapped_member_count or 0) == 0,
                )
                if act is not None
                else None
            ),
        )
        for lab, act in rows
    ]


@app.get("/api/changes", response_model=list[ChangeOut])
def list_changes(
    db: Session = Depends(get_db),
    entity_type: str | None = None,
    change_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[ChangeOut]:
    stmt = (
        select(ChangeEvent, Faculty.full_name)
        .outerjoin(Faculty, Faculty.id == ChangeEvent.entity_id)
        .order_by(ChangeEvent.detected_at.desc(), ChangeEvent.id.desc())
        .limit(limit)
    )
    if entity_type:
        stmt = stmt.where(ChangeEvent.entity_type == entity_type)
    if change_type:
        stmt = stmt.where(ChangeEvent.change_type == change_type)

    return [
        ChangeOut(
            id=event.id,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            change_type=event.change_type,
            field=event.field,
            old_value=event.old_value,
            new_value=event.new_value,
            detected_at=event.detected_at.isoformat(),
            faculty_name=name if event.entity_type in ("faculty", "activity") else None,
        )
        for event, name in db.execute(stmt).all()
    ]


@app.get("/api/runs", response_model=list[RunOut])
def list_runs(
    db: Session = Depends(get_db),
    source: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> list[RunOut]:
    stmt = select(FetchRun).order_by(FetchRun.id.desc()).limit(limit)
    if source:
        stmt = stmt.where(FetchRun.source == source)
    return [
        RunOut(
            id=r.id,
            source=r.source,
            status=r.status,
            started_at=r.started_at.isoformat(),
            finished_at=r.finished_at.isoformat() if r.finished_at else None,
            stats=r.stats,
            error=r.error,
        )
        for r in db.scalars(stmt)
    ]


@app.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    db.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "latest_activity_run": latest_activity_run(db),
        "faculty_current": db.scalar(
            select(func.count()).select_from(Faculty).where(Faculty.is_current)
        ),
    }


if FRONTEND_DIR is not None:

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount(
        "/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static"
    )
