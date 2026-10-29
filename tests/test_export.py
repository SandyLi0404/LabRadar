"""Static export for GitHub Pages.

The export must hold every record, because the static page filters in the
browser and can only show what the snapshot contains. A professor dropped here
would vanish from the site with no error anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from labradar import export

REPO_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class FakeModel(dict):
    def model_dump(self, mode):
        return dict(self)


@pytest.fixture
def fake_api(monkeypatch):
    """Three pages of faculty, with every call's filters recorded."""
    people = [FakeModel(full_name=f"Person {i}") for i in range(450)]
    calls = []

    def list_faculty(**kwargs):
        calls.append(kwargs)
        start = (kwargs["page"] - 1) * kwargs["page_size"]
        return SimpleNamespace(
            total=len(people), results=people[start : start + kwargs["page_size"]]
        )

    monkeypatch.setattr(export.api, "list_faculty", list_faculty)
    monkeypatch.setattr(export.api, "list_labs", lambda **kw: [FakeModel(name="A Lab")])
    monkeypatch.setattr(export.api, "list_areas", lambda **kw: [FakeModel(slug="theory")])
    return calls


class TestEveryRecordIsExported:
    def test_pages_through_all_faculty(self, fake_api):
        assert len(export.all_faculty(session=None)) == 450
        assert [c["page"] for c in fake_api] == [1, 2, 3]

    def test_no_filter_is_applied(self, fake_api):
        export.all_faculty(session=None)
        call = fake_api[0]
        assert call["include_inactive"] and call["include_no_website"]
        assert call["include_departed"] and call["include_unmapped"]
        assert call["q"] is None and call["area"] is None


class TestSiteLayout:
    @pytest.fixture
    def site(self, fake_api, tmp_path):
        out = tmp_path / "site"
        counts = export.write_site(None, out, REPO_FRONTEND)
        return out, counts

    def test_writes_the_data_files(self, site):
        out, counts = site
        assert counts == {"faculty": 450, "labs": 1, "areas": 1}
        faculty = json.loads((out / "data" / "faculty.json").read_text(encoding="utf-8"))
        assert len(faculty) == 450

    def test_the_page_is_marked_static(self, site):
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert 'data-source="static"' in html

    def test_assets_sit_where_the_page_references_them(self, site):
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        for asset in ("static/app.js", "static/style.css"):
            assert f'"{asset}"' in html
            assert (out / asset).is_file()
        assert not (out / "static" / "index.html").exists()

    def test_replaces_a_previous_export(self, fake_api, tmp_path):
        out = tmp_path / "site"
        (out / "stale").mkdir(parents=True)
        export.write_site(None, out, REPO_FRONTEND)
        assert not (out / "stale").exists()
