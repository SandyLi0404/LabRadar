"""Discover research groups from faculty homepages.

The softest job in the pipeline, and the only one that touches pages with no
agreed structure. It is therefore written to fail small: a page that times out,
returns 403 or has changed shape costs one faculty member's lab, never the run.

Not finding a lab is recorded as not found. It does not mean the person has no
group -- plenty simply never name one on their page.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from labradar.config import get_settings
from labradar.db.models import (
    Faculty,
    FacultyLab,
    FacultyVersion,
    FetchRun,
    Lab,
)
from labradar.sources.labs import LabMention, extract_labs

log = logging.getLogger(__name__)

SOURCE = "faculty_labs"

# Below this, a mention is more likely a coincidence than a group.
MIN_CONFIDENCE = 0.5

# Faculty do belong to more than one group, but a page yielding many is usually
# a page full of links rather than a person running five labs.
MAX_LABS_PER_FACULTY = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_url(url: str | None) -> str | None:
    """Normalize a lab URL enough to recognise it written two ways."""
    if not url:
        return None
    return url.strip().rstrip("/").lower().replace("://www.", "://")


# Statuses from a site that is up but turning away scripts. A browser gets
# through, so the link is good for the people it is shown to.
BLOCKS_SCRIPTS = frozenset({401, 403, 429})


def _url_variants(url: str) -> list[str]:
    """The link as written, then the forms faculty pages most often get wrong.

    Old pages carry ``http://`` for sites that are now HTTPS-only, and ``www.``
    on hosts that never had it (``www.psl.cs.columbia.edu`` does not resolve;
    ``psl.cs.columbia.edu`` does).
    """
    variants = [url]
    if url.startswith("http://"):
        variants.append("https://" + url[len("http://"):])
    for v in list(variants):
        if "://www." in v:
            variants.append(v.replace("://www.", "://", 1))
    return variants


def resolve_lab_url(client: httpx.Client, url: str) -> str | None:
    """The address a lab link actually opens at, or None if it is dead.

    A link that 404s or never answers is worse than no link: the interface
    would send a student to a dead end with our name on it. Redirects are
    followed and the final address kept, so a lab that moved is shown where it
    now lives.
    """
    for candidate in _url_variants(url):
        try:
            response = client.get(candidate)
        except httpx.HTTPError:
            continue
        if response.status_code < 400 or response.status_code in BLOCKS_SCRIPTS:
            return str(response.url)
    return None


def upsert_lab(session: Session, mention: LabMention, run: FetchRun) -> Lab:
    """Find or create the lab, keyed on its URL first and then its name.

    The URL is the better key: one group is commonly written both "Software
    Systems Lab" and "Software Systems Laboratory", and matching on the address
    they both point at collapses those into a single group instead of two
    half-populated ones. Falling back to the name slug still merges groups that
    are named consistently but linked inconsistently.
    """
    lab = None
    canonical = _canonical_url(mention.url)
    if canonical:
        for existing in session.scalars(select(Lab).where(Lab.url.isnot(None))):
            if _canonical_url(existing.url) == canonical:
                lab = existing
                break
    if lab is None:
        lab = session.scalar(select(Lab).where(Lab.slug == mention.slug))
    if lab is None:
        lab = Lab(
            slug=mention.slug,
            name=mention.name,
            url=mention.url,
            first_seen_run=run.id,
            last_seen_run=run.id,
            is_current=True,
        )
        session.add(lab)
        session.flush()
        return lab

    lab.last_seen_run = run.id
    lab.is_current = True
    # A mention's URL has just been checked, so it replaces whatever was stored
    # -- that is how a moved or mistyped address gets corrected. Never
    # overwrite a known URL with nothing.
    if mention.url:
        lab.url = mention.url
    return lab


def record_membership(
    session: Session, faculty: Faculty, lab: Lab, mention: LabMention,
    run: FetchRun, at: datetime,
) -> bool:
    """Attach a faculty member to a lab, or refresh the existing attachment."""
    row = session.scalar(
        select(FacultyLab).where(
            FacultyLab.faculty_id == faculty.id,
            FacultyLab.lab_id == lab.id,
            FacultyLab.valid_to.is_(None),
        )
    )
    if row is not None:
        row.confidence = mention.confidence
        row.method = mention.method
        row.source_url = mention.source_url
        return False

    session.add(
        FacultyLab(
            faculty_id=faculty.id,
            lab_id=lab.id,
            method=mention.method,
            confidence=mention.confidence,
            source_url=mention.source_url,
            fetch_run_id=run.id,
            valid_from=at,
        )
    )
    return True


def run(session: Session, client: httpx.Client | None = None) -> FetchRun:
    """Visit each faculty homepage once and record any lab it names."""
    settings = get_settings()
    at = _now()
    fetch_run = FetchRun(source=SOURCE, started_at=at, status="running")
    session.add(fetch_run)
    session.flush()

    owns_client = client is None
    client = client or httpx.Client(
        timeout=15.0,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )

    stats = {
        "pages_tried": 0,
        "pages_ok": 0,
        "fetch_failed": 0,
        "labs_found": 0,
        "memberships_added": 0,
        "faculty_with_lab": 0,
        "lab_links_dead": 0,
    }
    # Several faculty link the same group; check each address once per run.
    resolved: dict[str, str | None] = {}

    def checked(mention: LabMention) -> LabMention | None:
        if mention.url is None:
            return mention
        if mention.url not in resolved:
            resolved[mention.url] = resolve_lab_url(client, mention.url)
            if resolved[mention.url] is None:
                log.info("lab link dead for %s: %s", mention.name, mention.url)
                stats["lab_links_dead"] += 1
        url = resolved[mention.url]
        return None if url is None else replace(mention, url=url)

    try:
        rows = session.execute(
            select(Faculty, FacultyVersion.homepage_url)
            .join(
                FacultyVersion,
                (FacultyVersion.faculty_id == Faculty.id)
                & (FacultyVersion.valid_to.is_(None)),
            )
            .where(Faculty.is_current, FacultyVersion.homepage_url.isnot(None))
        ).all()

        seen_pairs: set[tuple[int, int]] = set()

        for faculty, homepage in rows:
            stats["pages_tried"] += 1
            try:
                response = client.get(homepage)
                response.raise_for_status()
            except Exception as exc:
                # One unreachable personal site must not fail the run.
                log.info("lab fetch failed for %s (%s): %s", faculty.full_name, homepage, exc)
                stats["fetch_failed"] += 1
                time.sleep(settings.request_delay_seconds)
                continue

            stats["pages_ok"] += 1
            mentions = [
                live
                for m in extract_labs(response.text, str(response.url))
                if m.confidence >= MIN_CONFIDENCE and (live := checked(m)) is not None
            ][:MAX_LABS_PER_FACULTY]

            if mentions:
                stats["faculty_with_lab"] += 1
            for mention in mentions:
                lab = upsert_lab(session, mention, fetch_run)
                if record_membership(session, faculty, lab, mention, fetch_run, at):
                    stats["memberships_added"] += 1
                seen_pairs.add((faculty.id, lab.id))

            time.sleep(settings.request_delay_seconds)

        session.flush()
        stats["labs_found"] = session.scalar(
            select(func.count()).select_from(Lab).where(Lab.last_seen_run == fetch_run.id)
        )

        # Close memberships whose page no longer names the lab. The row stays,
        # so "this group stopped being mentioned" remains visible in history.
        closed = 0
        for row in session.scalars(
            select(FacultyLab).where(FacultyLab.valid_to.is_(None))
        ):
            if (row.faculty_id, row.lab_id) not in seen_pairs:
                row.valid_to = at
                closed += 1
        stats["memberships_closed"] = closed

        # A group nobody is attached to any more drops out of the listing. The
        # row is kept, like every other observation, so it can come back.
        session.flush()
        seen_labs = {lab_id for _, lab_id in seen_pairs}
        retired = 0
        for lab in session.scalars(select(Lab).where(Lab.is_current)):
            if lab.id not in seen_labs:
                lab.is_current = False
                retired += 1
        stats["labs_retired"] = retired

        fetch_run.status = "partial" if stats["fetch_failed"] else "success"
        fetch_run.stats = stats
    except Exception as exc:
        fetch_run.status = "failed"
        fetch_run.error = str(exc)
        fetch_run.stats = stats
        raise
    finally:
        fetch_run.finished_at = _now()
        if owns_client:
            client.close()

    return fetch_run
