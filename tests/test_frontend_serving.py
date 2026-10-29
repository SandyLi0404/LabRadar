"""Frontend discovery and serving.

These exist because of a real break: the path was resolved relative to the
package, which is correct for an editable install in the repo but lands in the
Python prefix once the package is installed into site-packages. The container
came up healthy, /docs worked, and only "/" 404ed -- a failure mode that gives
almost no signal about its own cause.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin

import pytest
from fastapi.testclient import TestClient

from labradar import api
from labradar.api import _resolve_frontend_dir, app

REPO_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


class TestDiscovery:
    def test_finds_the_frontend_in_this_checkout(self):
        assert _resolve_frontend_dir() == REPO_FRONTEND

    def test_an_explicit_setting_wins(self, tmp_path, monkeypatch):
        override = tmp_path / "custom-frontend"
        override.mkdir()

        settings = api.get_settings()
        monkeypatch.setattr(settings, "frontend_dir", override, raising=False)
        assert _resolve_frontend_dir() == override

    def test_a_nonexistent_override_falls_through(self, tmp_path, monkeypatch):
        """A stale FRONTEND_DIR should not disable the UI outright."""
        settings = api.get_settings()
        monkeypatch.setattr(settings, "frontend_dir", tmp_path / "gone", raising=False)
        assert _resolve_frontend_dir() == REPO_FRONTEND

    def test_missing_frontend_warns_loudly(self, tmp_path, monkeypatch, caplog):
        """The original bug was silent; a missing UI must say so."""
        monkeypatch.setattr(
            api, "__file__", str(tmp_path / "a" / "b" / "c" / "api.py")
        )
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(
            api.Path, "is_dir", lambda self: False, raising=False
        )

        with caplog.at_level(logging.WARNING):
            assert _resolve_frontend_dir() is None
        assert "frontend directory not found" in caplog.text
        assert "FRONTEND_DIR" in caplog.text


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


class TestServing:
    def test_root_serves_the_page(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "<title>LabRadar" in response.text

    def test_root_is_registered_as_a_route(self):
        """Guards the mount itself, not just the file on disk."""
        assert "/" in {r.path for r in app.routes if hasattr(r, "methods")}

    @pytest.mark.parametrize("path", ["/static/style.css", "/static/app.js"])
    def test_static_assets_are_mounted(self, client, path):
        assert client.get(path).status_code == 200

    def test_the_page_references_the_assets_it_serves(self, client):
        """A 200 on each file is not enough if the HTML points elsewhere."""
        html = client.get("/").text
        refs = re.findall(r'(?:href|src)="([^"]+\.(?:css|js))"', html)
        # Relative, so the same page also works from a Pages project subpath.
        assert {urljoin("/", r) for r in refs} == {"/static/style.css", "/static/app.js"}
        for ref in refs:
            assert client.get(urljoin("/", ref)).status_code == 200
