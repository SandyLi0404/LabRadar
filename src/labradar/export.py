"""Write the site as static files, for hosts that only serve files.

GitHub Pages has no server and no database, so the API's answers are written
out ahead of time instead. The export calls the same route functions the live
API does, with every filter switched off, and the frontend applies the filters
in the browser. One code path decides what a professor's record says; the
static site only decides which records to show.

Layout written to the output directory::

    index.html          the page, marked data-source="static"
    static/             the frontend's CSS and JS, unchanged
    data/faculty.json   every professor, departed and inactive included
    data/labs.json      every current group
    data/areas.json     the research areas with their counts
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from labradar import api

# Paging only exists to keep live responses small; the export wants everyone.
PAGE_SIZE = 200
MAX_LABS = 500


def _dump(models: Any) -> Any:
    if isinstance(models, list):
        return [m.model_dump(mode="json") for m in models]
    return models.model_dump(mode="json")


def all_faculty(session: Session) -> list[dict]:
    people: list[dict] = []
    page = 1
    while True:
        result = api.list_faculty(
            db=session,
            q=None,
            area=None,
            include_inactive=True,
            include_no_website=True,
            include_unmapped=True,
            include_departed=True,
            sort="name",
            page=page,
            page_size=PAGE_SIZE,
        )
        people.extend(_dump(result.results))
        if len(people) >= result.total or not result.results:
            return people
        page += 1


def all_labs(session: Session) -> list[dict]:
    return _dump(
        api.list_labs(
            db=session,
            include_inactive=True,
            include_unmapped=True,
            sort="works_since",
            limit=MAX_LABS,
        )
    )


def all_areas(session: Session) -> list[dict]:
    return _dump(api.list_areas(db=session))


def write_site(session: Session, out: Path, frontend: Path) -> dict[str, int]:
    """Write the whole site into ``out``, replacing what was there."""
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(frontend, out / "static")

    page = (frontend / "index.html").read_text(encoding="utf-8")
    marked = page.replace('<html lang="en">', '<html lang="en" data-source="static">', 1)
    if marked == page:
        raise RuntimeError("index.html has no <html lang=\"en\"> tag to mark as static")
    (out / "index.html").write_text(marked, encoding="utf-8")
    (out / "static" / "index.html").unlink()

    data = {
        "faculty": all_faculty(session),
        "labs": all_labs(session),
        "areas": all_areas(session),
    }
    (out / "data").mkdir()
    for name, rows in data.items():
        (out / "data" / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
    return {name: len(rows) for name, rows in data.items()}
