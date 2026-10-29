"""Lab-extraction tests.

Faculty pages follow no convention, so the extractor's job is as much about
what it refuses as what it finds. Every rejection case below is a real string
that appeared on a Columbia faculty page and was wrongly accepted at some point
during development.
"""

from __future__ import annotations

import httpx
import pytest

from labradar.jobs.fetch_labs import resolve_lab_url
from labradar.sources.labs import LabMention, at_other_institution, extract_labs

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


class TestOtherInstitutions:
    """Both cases are real: each put a non-Columbia group on a Columbia card."""

    def test_a_lab_at_another_university_is_rejected(self):
        """A Columbia page linking a Dartmouth colleague's lab."""
        html = page(link("Augmented Health Lab", "https://www.ah-lab.cs.dartmouth.edu/"))
        assert extract_labs(html, FACULTY_PAGE) == []

    def test_a_page_hosted_at_another_university_yields_its_groups_nothing(self):
        """A directory homepage still at the previous employer, in the UK."""
        vlad = "http://www.clrc.rhul.ac.uk/people/vlad/"
        html = page(link("Computer Learning Research Centre", "http://clrc.rhul.ac.uk/"))
        assert extract_labs(html, vlad) == []
        assert extract_labs(page("<h2>Computer Learning Group</h2>"), vlad) == []

    def test_a_columbia_lab_linked_from_an_old_page_is_kept(self):
        html = page(link("Mobile X Lab", "http://mobilex.cs.columbia.edu"))
        assert first_lab(html, "http://www.mit.edu/~someone/").name == "Mobile X Lab"

    @pytest.mark.parametrize(
        "url", ["https://wan-research-group.github.io/", "https://www.rayb.info/ariselab"]
    )
    def test_non_academic_hosts_say_nothing_about_institution(self, url):
        assert first_lab(page(link("ARiSE Lab", url)), FACULTY_PAGE) is not None

    @pytest.mark.parametrize(
        "url,other",
        [
            ("https://cs.columbia.edu/x", False),
            ("http://dna.cs.columbia.edu", False),
            ("https://www.mit.edu/lab", True),
            ("https://cml.rhul.ac.uk/", True),
            ("https://cs.tsinghua.edu.cn/", True),
            ("https://notcolumbia.edu/", True),
            ("https://example.org/", False),
        ],
    )
    def test_institution_check(self, url, other):
        assert at_other_institution(url) is other


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


class TestLinkResolution:
    """Each case is one of the dead links the first live run put on a card."""

    @staticmethod
    def client(routes):
        """Answer from ``routes`` (url -> status, or a redirect target); else no DNS."""

        def handler(request):
            url = str(request.url)
            if url not in routes:
                raise httpx.ConnectError("getaddrinfo failed", request=request)
            answer = routes[url]
            if isinstance(answer, str):
                return httpx.Response(301, headers={"location": answer})
            return httpx.Response(answer)

        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    def test_a_working_link_is_kept_as_is(self):
        client = self.client({"https://cave.cs.columbia.edu/": 200})
        assert resolve_lab_url(client, "https://cave.cs.columbia.edu/") == (
            "https://cave.cs.columbia.edu/"
        )

    def test_a_redirect_is_followed_to_where_the_lab_lives(self):
        client = self.client(
            {"http://sld.cs.columbia.edu/": "https://sld.cs.columbia.edu/",
             "https://sld.cs.columbia.edu/": 200}
        )
        assert resolve_lab_url(client, "http://sld.cs.columbia.edu/") == (
            "https://sld.cs.columbia.edu/"
        )

    def test_a_www_host_that_does_not_resolve_falls_back_to_the_bare_host(self):
        client = self.client({"http://psl.cs.columbia.edu/": 200})
        assert resolve_lab_url(client, "http://www.psl.cs.columbia.edu/") == (
            "http://psl.cs.columbia.edu/"
        )

    def test_http_falls_back_to_https(self):
        client = self.client({"https://lab.cs.columbia.edu/": 200})
        assert resolve_lab_url(client, "http://lab.cs.columbia.edu/") == (
            "https://lab.cs.columbia.edu/"
        )

    def test_a_redirect_into_a_404_is_dead(self):
        client = self.client(
            {"http://www1.cs.columbia.edu/cvgc/": "https://vigr.cs.columbia.edu/",
             "https://vigr.cs.columbia.edu/": 404}
        )
        assert resolve_lab_url(client, "http://www1.cs.columbia.edu/cvgc/") is None

    def test_an_unreachable_host_is_dead(self):
        assert resolve_lab_url(self.client({}), "http://dna.cs.columbia.edu") is None

    @pytest.mark.parametrize("status", [401, 403, 429])
    def test_a_site_that_blocks_scripts_still_opens_in_a_browser(self, status):
        client = self.client({"https://lab.example.org/": status})
        assert resolve_lab_url(client, "https://lab.example.org/") is not None

    def test_server_errors_are_dead(self):
        client = self.client({"https://lab.example.org/": 503})
        assert resolve_lab_url(client, "https://lab.example.org/") is None
