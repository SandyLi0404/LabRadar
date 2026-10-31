"""Current PhD students, read from the hand-checked file in curation/.

Faculty pages mix current students with alumni and postdocs, and many are years
out of date, so a scraper would present former students as current. The file is
checked by hand instead, and every list keeps a link to the page it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_PATH = Path("curation/phd_students.yaml")
DEFAULT_LABEL = "PhD students"


@dataclass(frozen=True)
class StudentList:
    label: str
    names: tuple[str, ...]
    source_url: str
    checked: str


def load(path: Path = DEFAULT_PATH) -> dict[str, StudentList]:
    """Student lists by person ID; empty if the file is absent."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    checked = str(data["checked"])
    return {
        str(person_id): StudentList(
            label=entry.get("label", DEFAULT_LABEL),
            names=tuple(entry["students"]),
            source_url=entry["source"],
            checked=checked,
        )
        for person_id, entry in data["professors"].items()
    }


@lru_cache(maxsize=1)
def cached() -> dict[str, StudentList]:
    return load()
