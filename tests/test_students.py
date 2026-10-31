"""The PhD student file: it loads, and it holds names rather than contact details."""

from pathlib import Path

from labradar import students


def test_curated_file_loads_with_a_source_for_every_list():
    lists = students.load()
    assert lists
    for entry in lists.values():
        assert entry.names
        assert entry.source_url.startswith("http")
        assert entry.checked


def test_curated_file_holds_no_email_addresses():
    assert "@" not in students.DEFAULT_PATH.read_text(encoding="utf-8")


def test_label_defaults_to_phd_students_and_can_be_overridden(tmp_path: Path):
    path = tmp_path / "students.yaml"
    path.write_text(
        "checked: 2026-10-30\n"
        "professors:\n"
        '  "1": {source: "https://a.example", students: [Ann Lee]}\n'
        '  "2": {source: "https://b.example", label: Lab members, students: [Bo Li]}\n',
        encoding="utf-8",
    )
    lists = students.load(path)
    assert lists["1"].label == "PhD students"
    assert lists["2"].label == "Lab members"
    assert lists["2"].checked == "2026-10-30"


def test_missing_file_means_no_lists(tmp_path: Path):
    assert students.load(tmp_path / "absent.yaml") == {}
