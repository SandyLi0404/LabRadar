"""Finding the lab or group a faculty member runs, from their own page.

This is the soft-signal layer, and it is deliberately the least trusted part of
the project. Unlike the directory and the area pages, personal sites follow no
convention: some name their lab in a heading, some only in a link, and many
never mention one. Absence of a lab here means "not found", never "has no lab".

Every extraction therefore carries how it was found, the page it came from and a
confidence, so the interface can present it as something we noticed rather than
something we know.

Publication lists are deliberately *not* scraped from these pages. Surveying
them showed why: a quarter delegate to Google Scholar (excluded by the project's
data-source rules), a quarter have no publication link at all, and the rest use
bespoke, undated formats with no DOIs to deduplicate against. Lab output is
instead aggregated from its members' OpenAlex records, which are dated,
deduplicable and comparable across groups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

# "Lab" and "Group" indicate something a researcher runs. "Center" and
# "Institute" more often name a building or a funding body, so they are
# accepted only weakly.
STRONG_SUFFIX = re.compile(r"\b(Lab|Labs|Laboratory)\b|\bGroup\b", re.I)
WEAK_SUFFIX = re.compile(r"\b(Center|Centre|Institute|Consortium)\b", re.I)

# Phrases that look like labs but name an employer or a department.
INSTITUTIONAL = re.compile(
    r"""
    \b(?:
        university | college | school \s+ of | department | faculty \s+ of
      | institute \s+ of \s+ technology | medical \s+ center | cancer \s+ center
      | data \s+ science \s+ institute | engineering \s+ school
      | national \s+ laboratory | graduate \s+ school
    )\b
    """,
    re.I | re.X,
)

# A lab name is a few capitalised words ending in the right noun.
LAB_NAME = re.compile(
    r"^[A-Z0-9][\w'&.\-]*(?:\s+(?:[A-Z0-9][\w'&.\-]*|for|of|and|the|in|on))"
    r"{0,6}\s+(?:Lab|Labs|Laboratory|Group|Center|Centre|Institute)$"
)

# The institution whose groups this project covers. A lab hosted at any other
# university is someone else's: faculty link to a former employer's lab, or a
# colleague's, and their directory homepage can itself still sit at the old
# institution.
HOME_INSTITUTION = "columbia.edu"
ACADEMIC_HOST = re.compile(r"(?:\.edu|\.(?:ac|edu)\.[a-z]{2})$")

MAX_NAME_WORDS = 6
MIN_NAME_WORDS = 2

# Words that carry no identity. A name built only from these ("Research Group",
# "The Lab") names nothing findable, so it is not worth recording.
GENERIC_WORDS = frozenset(
    """
    the our my their a research lab labs laboratory group team center centre
    institute home homepage main people projects
    """.split()
)

# Words that start a navigation label rather than a name. Links like "Papers
# Published By Members of the NLP Group" or "Friends of the Group" end in the
# right noun and are not labs.
LEADING_NOISE = frozenset(
    """
    papers publications members friends alumni news join contact positions
    openings people photos resources links talks courses teaching projects
    welcome about more other all related former current former recent
    """.split()
)

# Confidence by how the name was found. An anchor is best: it names the lab and
# gives its address, which is corroboration a bare heading cannot offer.
CONFIDENCE = {
    "anchor_strong": 0.75,
    "heading_strong": 0.55,
    "anchor_weak": 0.40,
    "heading_weak": 0.30,
    "title_strong": 0.35,
}


@dataclass(frozen=True)
class LabMention:
    """A lab named on a faculty page, with how it was found."""

    name: str
    url: str | None
    method: str
    confidence: float
    source_url: str

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")


def _clean(text: str | None) -> str:
    """Collapse the whitespace that markup leaves inside link text."""
    return " ".join((text or "").split())


def _plausible_name(name: str) -> bool:
    words = name.split()
    if not (MIN_NAME_WORDS <= len(words) <= MAX_NAME_WORDS):
        return False
    if INSTITUTIONAL.search(name):
        return False
    # Needs at least one distinguishing word beyond the generic scaffolding.
    if not any(w.lower().strip(".,'") not in GENERIC_WORDS for w in words):
        return False
    if words[0].lower().strip(".,'") in LEADING_NOISE:
        return False
    return bool(LAB_NAME.match(name))


def _strength(name: str) -> str | None:
    if STRONG_SUFFIX.search(name):
        return "strong"
    if WEAK_SUFFIX.search(name):
        return "weak"
    return None


def _registrable(url: str) -> str | None:
    """The last two labels of a host, e.g. 'columbia.edu'.

    Deliberately coarser than an exact hostname: a lab commonly sits on a
    sibling subdomain of the faculty page (``dna.cs.columbia.edu`` beside
    ``www.cs.columbia.edu``), and treating those as unrelated would discard
    exactly the links worth trusting.
    """
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except ValueError:
        return None
    labels = [l for l in host.split(".") if l]
    return ".".join(labels[-2:]) if len(labels) >= 2 else None


def _same_site(a: str, b: str) -> bool:
    left, right = _registrable(a), _registrable(b)
    return bool(left and right and left == right)


def at_other_institution(url: str) -> bool:
    """True for a university host that is not the home institution.

    Only academic hosts count. Labs also live on GitHub Pages or a personal
    domain, and those say nothing about which institution the group belongs to.
    """
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except ValueError:
        return False
    if host == HOME_INSTITUTION or host.endswith("." + HOME_INSTITUTION):
        return False
    return bool(ACADEMIC_HOST.search(host))


def extract_labs(html: str, source_url: str) -> list[LabMention]:
    """Find labs named on one faculty page, best evidence first.

    Returns every distinct candidate rather than guessing a single answer:
    faculty do belong to more than one group, and the caller has the context to
    decide how many to keep.
    """
    tree = HTMLParser(html)
    found: dict[str, LabMention] = {}

    def offer(name: str, url: str | None, method: str) -> None:
        name = _clean(name)
        if not _plausible_name(name):
            return
        strength = _strength(name)
        if strength is None:
            return
        # "Center" and "Institute" usually name an employer or a partner
        # organisation. Accept one only when it is hosted on the faculty
        # member's own site, which is decent evidence that it is theirs.
        if strength == "weak" and not (url and _same_site(url, source_url)):
            return
        # Where the group lives: its own address, or failing that the page
        # that names it.
        if at_other_institution(url or source_url):
            return
        key = name.lower()
        confidence = CONFIDENCE.get(f"{method}_{strength}")
        if confidence is None:
            return
        existing = found.get(key)
        if existing is None or confidence > existing.confidence:
            found[key] = LabMention(
                name=name,
                url=url,
                method=f"{method}_{strength}",
                confidence=confidence,
                source_url=source_url,
            )

    for anchor in tree.css("a"):
        href = anchor.attributes.get("href")
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        offer(anchor.text(), urljoin(source_url, href), "anchor")

    for heading in tree.css("h1, h2, h3"):
        offer(heading.text(), None, "heading")

    title = tree.css_first("title")
    if title is not None:
        # Page titles often read "Name | The X Lab"; test each part.
        for part in re.split(r"[|–—:]", _clean(title.text())):
            offer(part, None, "title")

    mentions = sorted(found.values(), key=lambda m: m.confidence, reverse=True)

    # A lab hosted on the same domain as the faculty page is very likely theirs
    # rather than one they merely link to.
    return [
        (
            m
            if not (m.url and _same_site(m.url, source_url))
            else LabMention(
                m.name,
                m.url,
                m.method,
                min(0.9, m.confidence + 0.1),
                m.source_url,
            )
        )
        for m in mentions
    ]
