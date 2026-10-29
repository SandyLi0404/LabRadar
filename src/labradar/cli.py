"""Command line entrypoints.

Every job is runnable by hand. The scheduler calls exactly these commands, so
there is never a code path that only the scheduler exercises.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from labradar import curation as curation_mod
from labradar.config import get_settings
from labradar.db.session import session_scope
from labradar.jobs import (
    compute_activity,
    detect_changes,
    fetch_directory,
    fetch_labs,
    fetch_openalex,
)
from labradar.sources.openalex import OpenAlexClient, OpenAlexError

log = logging.getLogger("labradar")


def _report(run) -> None:
    print(f"  run #{run.id} [{run.source}] -> {run.status}")
    for key, value in (run.stats or {}).items():
        print(f"      {key}: {value}")
    if run.error:
        print(f"      error: {run.error}")


def cmd_fetch_directory(args) -> int:
    with session_scope() as session:
        _report(fetch_directory.run(session))
    return 0


def cmd_fetch_openalex(args) -> int:
    links = curation_mod.load(args.curation)
    if not links.mapped_count:
        print(
            "No confirmed links in the curation file. Run `labradar link --suggest` "
            "first, or activity will stay unmapped for everyone.",
            file=sys.stderr,
        )
    with session_scope() as session:
        _report(fetch_openalex.run(session, links))
    return 0


def cmd_fetch_labs(args) -> int:
    with session_scope() as session:
        _report(fetch_labs.run(session))
    return 0


def cmd_compute_activity(args) -> int:
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    with session_scope() as session:
        _report(compute_activity.run(session, as_of=as_of))
    return 0


def cmd_detect_changes(args) -> int:
    with session_scope() as session:
        _report(detect_changes.run(session))
    return 0


def cmd_run_all(args) -> int:
    """The scheduled chain, in order."""
    links = curation_mod.load(args.curation)
    with session_scope() as session:
        _report(fetch_directory.run(session))
    with session_scope() as session:
        _report(fetch_openalex.run(session, links))
    with session_scope() as session:
        _report(fetch_labs.run(session))
    with session_scope() as session:
        _report(compute_activity.run(session))
    with session_scope() as session:
        _report(detect_changes.run(session))
    return 0


def cmd_link(args) -> int:
    """Propose OpenAlex links for unmapped faculty.

    Searching costs about $0.001 per name, so this is a deliberate command
    rather than part of the scheduled chain. Proposals are written to the
    curation file with their evidence; review the diff before trusting them.
    """
    from sqlalchemy import select

    from labradar.db.models import Faculty, FacultyArea, FacultyVersion, ResearchArea
    from labradar.jobs.suggest_links import FacultyInput, suggest

    settings = get_settings()
    curation = curation_mod.load(args.curation)

    try:
        client = OpenAlexClient(
            settings.openalex_api_key, reserve_usd=settings.openalex_reserve_usd
        )
    except OpenAlexError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    with session_scope() as session, client:
        rows = session.execute(
            select(Faculty.person_id, Faculty.full_name, FacultyVersion.interests_raw)
            .outerjoin(
                FacultyVersion,
                (FacultyVersion.faculty_id == Faculty.id)
                & (FacultyVersion.valid_to.is_(None)),
            )
            .where(Faculty.is_current)
            .order_by(Faculty.full_name)
        ).all()

        areas: dict[str, list[str]] = {}
        for person_id, name in session.execute(
            select(Faculty.person_id, ResearchArea.name)
            .join(FacultyArea, FacultyArea.faculty_id == Faculty.id)
            .join(ResearchArea, ResearchArea.id == FacultyArea.research_area_id)
            .where(FacultyArea.valid_to.is_(None))
        ).all():
            areas.setdefault(person_id, []).append(name)

        people = [
            FacultyInput(
                person_id=r.person_id,
                full_name=r.full_name,
                interests_raw=r.interests_raw,
                area_names=areas.get(r.person_id, []),
            )
            for r in rows
        ]
        if args.person_id:
            people = [p for p in people if p.person_id == args.person_id]

        report = suggest(
            people,
            curation,
            client,
            include_curated=args.all,
            limit=args.limit,
        )

    for person_id, name, decision in report.confirmed:
        print(f"  CONFIRM  {name:24} {decision.chosen.openalex_id}  "
              f"conf={decision.confidence}  ({decision.reason})")
    for person_id, name, decision in report.candidates:
        print(f"  review   {name:24} {decision.chosen.openalex_id}  "
              f"({decision.reason})")
    for person_id, name, decision in report.unmatched:
        print(f"  no match {name:24} ({decision.reason})")

    print()
    print(report.summary())
    if report.stopped_early:
        print("Stopped early: OpenAlex budget reserve reached.", file=sys.stderr)

    if args.dry_run:
        print("Dry run; curation file not written.")
        return 0

    curation_mod.dump(curation, args.curation)
    print(f"Wrote {args.curation}. Review the diff, then run `labradar run-all`.")
    return 0


def cmd_review(args) -> int:
    """Show every pending proposal with the evidence behind it.

    Reviewing is a judgement about identity, so this prints what a person needs
    to make it -- the profile's size, recency, subject areas and a link to open
    -- and then gets out of the way. Nothing here edits the mapping.
    """
    curation = curation_mod.load(args.curation)
    pending = [
        (entry, link)
        for entry in curation
        for link in entry.links
        if link.status == curation_mod.CANDIDATE
    ]
    if not pending:
        print("Nothing pending. Every faculty member is either linked or has no candidate.")
        return 0

    print(f"{len(pending)} proposal(s) awaiting a decision.")
    print()
    for entry, link in sorted(pending, key=lambda x: x[0].name or ""):
        evidence = link.evidence or {}
        print(f"{entry.name}   (person_id: {entry.person_id})")
        print(f"   proposed : {link.openalex_id}   https://openalex.org/{link.openalex_id}")
        print(
            f"   evidence : {evidence.get('works_count', '?')} works, "
            f"last {evidence.get('last_year', '?')}, "
            f"subject match {evidence.get('topic_score', '?')}"
        )
        if evidence.get("topics"):
            print(f"   topics   : {', '.join(evidence['topics'])}")
        print(f"   held for : {link.note or '(no reason recorded)'}")
        print(f"   to accept: set status: confirmed  and  is_primary: true")
        print("   to reject: set status: rejected   and add a note")
        print()

    print(f"Edit {args.curation}, then run:")
    print("   labradar validate-curation && labradar run-all")
    return 0


def cmd_validate_curation(args) -> int:
    try:
        links = curation_mod.load(args.curation)
    except curation_mod.CurationError as exc:
        print(f"invalid curation file: {exc}", file=sys.stderr)
        return 1
    total_links = sum(len(e.links) for e in links)
    print(
        f"OK: {len(links.entries)} people, {links.mapped_count} mapped, "
        f"{total_links} links, {len(links.unmapped_person_ids)} unmapped"
    )
    return 0


def cmd_export_static(args) -> int:
    from labradar.api import FRONTEND_DIR
    from labradar.export import write_site

    if FRONTEND_DIR is None:
        print("No frontend directory found; set FRONTEND_DIR.", file=sys.stderr)
        return 1
    with session_scope() as session:
        counts = write_site(session, args.out, FRONTEND_DIR)
    print(f"Wrote {args.out}: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="labradar", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--curation",
        type=Path,
        default=curation_mod.DEFAULT_PATH,
        help="path to the curated OpenAlex mapping",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch-directory", help="scrape the faculty directory and area pages")
    sub.add_parser("fetch-openalex", help="refresh confirmed OpenAlex profiles")
    sub.add_parser("fetch-labs", help="discover research groups from faculty pages")

    activity = sub.add_parser("compute-activity", help="derive activity signals")
    activity.add_argument("--as-of", help="ISO date to compute windows against")

    sub.add_parser("detect-changes", help="diff the latest runs")
    sub.add_parser("run-all", help="the full scheduled chain")
    sub.add_parser("validate-curation", help="check the mapping file parses")
    sub.add_parser("review", help="show pending link proposals and their evidence")

    export = sub.add_parser("export-static", help="write the site as static files")
    export.add_argument("out", type=Path, help="directory to write (replaced)")

    link = sub.add_parser("link", help="suggest OpenAlex candidates for curation")
    link.add_argument("--person-id", help="only suggest for this person")
    link.add_argument("--limit", type=int, default=None, help="max people to search")
    link.add_argument(
        "--all", action="store_true", help="re-search already-curated faculty"
    )
    link.add_argument(
        "--dry-run", action="store_true", help="show proposals without writing"
    )

    return parser


HANDLERS = {
    "fetch-directory": cmd_fetch_directory,
    "fetch-openalex": cmd_fetch_openalex,
    "fetch-labs": cmd_fetch_labs,
    "compute-activity": cmd_compute_activity,
    "detect-changes": cmd_detect_changes,
    "run-all": cmd_run_all,
    "link": cmd_link,
    "validate-curation": cmd_validate_curation,
    "review": cmd_review,
    "export-static": cmd_export_static,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # httpx logs the full request URL at INFO, which would print the OpenAlex
    # api_key into the terminal and any captured job output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return HANDLERS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
