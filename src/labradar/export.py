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

import hashlib
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


ASSETS = ("static/app.js", "static/style.css")


def write_site(session: Session, out: Path, frontend: Path) -> dict[str, int]:
    """Write the whole site into ``out``, replacing what was there.

    Pages lets browsers cache every file for ten minutes, each on its own
    clock, so after a deploy a browser could pair the new page with last
    week's script or data -- and render a card the old code cannot fill in.
    Every asset and data URL therefore carries a build hash, so a page only
    ever loads the files it was built with.
    """
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(frontend, out / "static")
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

    build = hashlib.sha256()
    for path in sorted(out.rglob("*")):
        if path.is_file():
            build.update(path.read_bytes())
    build_id = build.hexdigest()[:12]

    page = (frontend / "index.html").read_text(encoding="utf-8")
    marked = page.replace(
        '<html lang="en">', f'<html lang="en" data-source="static" data-build="{build_id}">', 1
    )
    if marked == page:
        raise RuntimeError("index.html has no <html lang=\"en\"> tag to mark as static")
    for asset in ASSETS:
        if f'"{asset}"' not in marked:
            raise RuntimeError(f"index.html no longer references {asset}")
        marked = marked.replace(f'"{asset}"', f'"{asset}?v={build_id}"')
    (out / "index.html").write_text(marked, encoding="utf-8")
    return {name: len(rows) for name, rows in data.items()}
