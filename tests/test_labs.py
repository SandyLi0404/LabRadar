"""Lab-extraction tests.

Faculty pages follow no convention, so the extractor's job is as much about
what it refuses as what it finds. Every rejection case below is a real string
that appeared on a Columbia faculty page and was wrongly accepted at some point
during development.
"""

from __future__ import annotations

import pytest

from labradar.sources.labs import LabMention, extract_labs

FACULTY_PAGE = "http://www.cs.columbia.edu/~someone/"


def page(body: str) -> str:
    return f"<html><head><title>A Person</title></head><body>{body}</body></html>"


def link(text: str, href: str) -> str:
    return f'<a href="{href}">{text}</a>'


def first_lab(html: str, source_url: str):
    """The most credible mention on a page, or None."""
    mentions = extract_labs(html, source_url)
    return mentions[0] if mentions else None


class TestFindingLabs:
    def test_finds_a_lab_named_in_a_link(self):
        html = page(link("ARiSE Lab", "https://www.rayb.info/ariselab"))
        labs = extract_labs(html, FACULTY_PAGE)
        assert [l.name for l in labs] == ["ARiSE Lab"]
        assert labs[0].url == "https://www.rayb.info/ariselab"

    def test_collapses_whitespace_inside_link_text(self):
        """Markup wraps link text across lines; the name must not keep it."""
        html = page(link("DNA\n        Lab", "http://dna.cs.columbia.edu"))
        assert first_lab(html, FACULTY_PAGE).name == "DNA Lab"

    def test_relative_links_are_resolved(self):
        html = page(link("PRAISE Lab", "praise/index.html"))
        assert first_lab(html, FACULTY_PAGE).url == (
            "http://www.cs.columbia.edu/~someone/praise/index.html"
        )

    def test_finds_a_group_in_a_heading(self):
        html = page("<h2>Computer Graphics Group</h2>")
        lab = first_lab(html, FACULTY_PAGE)
        assert lab.name == "Computer Graphics Group"
        assert lab.url is None

    def test_a_lab_on_the_same_institution_scores_higher(self):
        """Corroboration: their own site hosting it means it is likely theirs."""
        onsite = first_lab(
            page(link("Mobile X Lab", "http://mobilex.cs.columbia.edu")), FACULTY_PAGE
        )
        offsite = first_lab(
            page(link("Mobile X Lab", "http://example.org/lab")), FACULTY_PAGE
        )
        assert onsite.confidence > offsite.confidence

    def test_sibling_subdomains_count_as_the_same_site(self):
        """dna.cs.columbia.edu beside www.cs.columbia.edu is the same place."""
        lab = first_lab(page(link("DNA Lab", "http://dna.cs.columbia.edu")), FACULTY_PAGE)
        assert lab.confidence > 0.75

    def test_several_groups_are_all_returned(self):
        html = page(
            link("Computer Graphics Group", "http://www.cs.columbia.edu/cg")
            + link("Columbia Vision and Graphics Center", "http://www1.cs.columbia.edu/cvgc/")
        )
        assert len(extract_labs(html, FACULTY_PAGE)) == 2

    def test_results_are_ordered_by_confidence(self):
        html = page(
            "<h2>Some Heading Lab</h2>" + link("Linked Lab", "http://x.cs.columbia.edu")
        )
        labs = extract_labs(html, FACULTY_PAGE)
        assert labs == sorted(labs, key=lambda l: l.confidence, reverse=True)


class TestRejections:
    """Strings that end in the right noun but name no research group."""

    @pytest.mark.parametrize(
        "text,why",
        [
            ("Papers Published By Members of the NLP Group", "navigation label"),
            ("Friends of the Group", "navigation label"),
            ("Research Group", "no distinguishing word"),
            ("The Lab", "no distinguishing word"),
            ("Stevens Institute of Technology", "an employer"),
            ("Columbia University", "an employer"),
            ("Herbert Irving Comprehensive Cancer Center", "an institution"),
            ("Department of Computer Science", "a department"),
        ],
    )
    def test_rejected(self, text, why):
        assert extract_labs(page(link(text, "/x")), FACULTY_PAGE) == [], why

    def test_offsite_centers_are_rejected(self):
        """'Center' usually names a partner organisation, not their group."""
        html = page(link("New York Genome Center", "http://www.nygenome.org/"))
        assert extract_labs(html, FACULTY_PAGE) == []

    def test_onsite_centers_are_kept_but_rated_lower(self):
        html = page(
            link("Columbia Vision and Graphics Center", "http://www1.cs.columbia.edu/cvgc/")
        )
        lab = first_lab(html, FACULTY_PAGE)
        assert lab is not None
        assert lab.confidence < 0.75

    def test_a_page_with_no_group_yields_nothing(self):
        """Absence means not found, never 'has no lab'."""
        assert extract_labs(page("<p>Welcome to my homepage.</p>"), FACULTY_PAGE) == []

    def test_mailto_and_anchors_are_skipped(self):
        html = page(link("Contact Lab", "mailto:x@y.z") + link("Jump Lab", "#lab"))
        assert extract_labs(html, FACULTY_PAGE) == []

    def test_overlong_names_are_rejected(self):
        html = page(link("A Very Long String Of Many Words Indeed Lab", "/x"))
        assert extract_labs(html, FACULTY_PAGE) == []


class TestSlug:
    @pytest.mark.parametrize(
        "name,slug",
        [
            ("ARiSE Lab", "arise-lab"),
            ("Columbia Core AI Lab", "columbia-core-ai-lab"),
            ("Software Systems Lab", "software-systems-lab"),
        ],
    )
    def test_slugs_are_url_safe(self, name, slug):
        assert LabMention(name, None, "anchor_strong", 0.75, FACULTY_PAGE).slug == slug


class TestUrlCanonicalisation:
    """One group written two ways must not become two groups."""

    def test_lab_and_laboratory_share_a_url(self):
        from labradar.jobs.fetch_labs import _canonical_url

        assert _canonical_url("http://systems.cs.columbia.edu/") == _canonical_url(
            "http://systems.cs.columbia.edu"
        )

    def test_www_prefix_is_ignored(self):
        from labradar.jobs.fetch_labs import _canonical_url

        assert _canonical_url("https://www.example.edu/lab") == _canonical_url(
            "https://example.edu/lab"
        )

    def test_missing_url_has_no_canonical_form(self):
        from labradar.jobs.fetch_labs import _canonical_url

        assert _canonical_url(None) is None
