"""Parsers for the Columbia CS department's public pages.

Two pages feed the seed layer:

  * ``/people/faculty/`` -- the directory, giving identity and contact details.
  * ``/areas/<slug>/``   -- eleven area pages, each listing its own members.

Both are server-rendered HTML with stable class names. Every faculty photo URL
embeds an internal ``personID``, which is used as the natural key throughout:
names collide and change, this does not.

Area membership is taken from the area pages rather than inferred from the
free-text interests line, so the faculty-to-area relation is sourced rather
than guessed. The interests text is still captured verbatim to feed search.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

BASE_URL = "https://www.cs.columbia.edu"
FACULTY_URL = f"{BASE_URL}/people/faculty/"

# The department's canonical areas, as linked from /research/areas/.
RESEARCH_AREAS: dict[str, str] = {
    "ai": "Artificial Intelligence",
    "machine": "Machine Learning",
    "vision-robotics": "Vision & Robotics",
    "networking": "Networking",
    "compengin": "Computer Engineering",
    "software": "Software Systems",
    "compio": "Computational Biology",
    "security": "Security & Privacy",
    "speech": "NLP & Speech",
    "theory": "Theory",
    "graphics-ui": "Graphics & User Interfaces",
}

# Columbia separates a research professoriate from a teaching track. The
# distinction matters here because a student looking for a group should not
# have to scroll past instructors who run none.
#
# Title alone is not sufficient evidence, though: Ansaf Salleb-Aouissi is a
# Senior Lecturer in Discipline who runs the PRAISE Lab and publishes. So this
# only labels the appointment; whether to hide someone is decided later,
# together with whether they actually have a lab or papers.
TEACHING_TITLE = re.compile(
    r"\b(?:lecturer|teaching\s+professor|instructor|adjunct|"
    r"professor\s+of\s+practice)\b",
    re.I,
)

RESEARCH_TRACK = "research"
TEACHING_TRACK = "teaching"

_PERSON_ID = re.compile(r"personID=(\d+)")
_INTERESTS_PREFIX = re.compile(r"^\s*interests\s*:\s*", re.IGNORECASE)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def classify_title(title: str | None) -> str:
    """Label an appointment as research or teaching track.

    Returns the track only. A teaching-track appointment is not by itself a
    reason to hide someone -- see the ``teaching_only`` filter, which also
    requires the absence of a lab and of any linked publications.
    """
    if not title:
        return RESEARCH_TRACK
    return TEACHING_TRACK if TEACHING_TITLE.search(title) else RESEARCH_TRACK


def area_url(slug: str) -> str:
    return f"{BASE_URL}/areas/{slug}/"


@dataclass
class FacultyRecord:
    """One directory entry, as scraped.

    Fields are stored exactly as published; normalization happens downstream so
    the raw observation stays auditable.
    """

    person_id: str
    full_name: str
    slug: str
    title: str | None = None
    email: str | None = None
    office: str | None = None
    phone: str | None = None
    homepage_url: str | None = None
    scholar_url: str | None = None
    cv_url: str | None = None
    interests_raw: str | None = None

    @property
    def appointment_track(self) -> str:
        return classify_title(self.title)

    @property
    def content_hash(self) -> str:
        """Fingerprint of the mutable fields, used to skip no-op version writes."""
        payload = "\x1f".join(
            str(v)
            for v in (
                self.full_name,
                self.title,
                self.email,
                self.office,
                self.phone,
                self.homepage_url,
                self.scholar_url,
                self.cv_url,
                self.interests_raw,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def interest_terms(self) -> list[str]:
        """Interests split into terms, deduplicated, original casing preserved.

        The directory separates these inconsistently -- commas mostly, but also
        semicolons and the occasional ' and ' -- so all three are treated as
        separators.
        """
        if not self.interests_raw:
            return []
        parts = re.split(r"[;,]|\band\b", self.interests_raw)
        seen: set[str] = set()
        terms: list[str] = []
        for part in parts:
            term = part.strip(" . ")
            if not term:
                continue
            key = term.lower()
            if key not in seen:
                seen.add(key)
                terms.append(term)
        return terms


def make_slug(name: str) -> str:
    """Turn 'Alman, Josh' into 'josh-alman'.

    The directory lists people surname-first; flipping it produces the natural
    reading order for URLs.
    """
    cleaned = name.strip()
    if "," in cleaned:
        surname, _, given = cleaned.partition(",")
        cleaned = f"{given.strip()} {surname.strip()}"
    return _SLUG_STRIP.sub("-", cleaned.lower()).strip("-")


def display_name(name: str) -> str:
    """'Alman, Josh' -> 'Josh Alman'."""
    cleaned = " ".join(name.split())
    if "," in cleaned:
        surname, _, given = cleaned.partition(",")
        return f"{given.strip()} {surname.strip()}".strip()
    return cleaned


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    value = " ".join(node.text().split())
    return value or None


def _person_id_from(node: Node) -> str | None:
    for img in node.css("img"):
        match = _PERSON_ID.search(img.attributes.get("src") or "")
        if match:
            return match.group(1)
    return None


def _contact_fields(node: Node | None) -> dict[str, str | None]:
    """Pull Office / Phone / Email out of the contact list.

    The markup uses unclosed <li> tags, so the labels are read from the block's
    text rather than by iterating elements.
    """
    fields: dict[str, str | None] = {"office": None, "phone": None, "email": None}
    if node is None:
        return fields

    for link in node.css("a[href^='mailto:']"):
        fields["email"] = (link.attributes.get("href") or "")[len("mailto:") :].strip()
        break

    text = " ".join(node.text().split())
    for key, label, following in (
        ("office", "Office:", ("Phone:", "Email:")),
        ("phone", "Phone:", ("Office:", "Email:")),
    ):
        if label not in text:
            continue
        value = text.split(label, 1)[1]
        for stop in following:
            value = value.split(stop, 1)[0]
        cleaned = value.strip(" : ")
        # An empty label (common for Phone) must stay None, not become the
        # email address that follows it.
        if cleaned and "@" not in cleaned:
            fields[key] = cleaned
    return fields


def _links(node: Node | None) -> dict[str, str | None]:
    """Classify the CV / Scholar / homepage icon links by their href."""
    found: dict[str, str | None] = {"cv_url": None, "scholar_url": None}
    if node is None:
        return found
    for link in node.css("a"):
        href = link.attributes.get("href")
        if not href:
            continue
        lowered = href.lower()
        if "scholar.google" in lowered and not found["scholar_url"]:
            found["scholar_url"] = href
        elif lowered.endswith(".pdf") and "cv" in lowered and not found["cv_url"]:
            found["cv_url"] = href
    return found


def parse_faculty_directory(html: str) -> list[FacultyRecord]:
    """Extract every faculty entry from the directory page.

    Entries without a ``personID`` are skipped: without the natural key there is
    nothing stable to attach history to.
    """
    tree = HTMLParser(html)
    records: list[FacultyRecord] = []
    seen: set[str] = set()

    # Each entry is a .faculty-row holding two columns: the photo (which carries
    # the personID) and the .faculty-details text. Iterating the row is what
    # keeps the key and the details together.
    for row in tree.css(".faculty-row"):
        block = row.css_first(".faculty-details")
        if block is None:
            continue

        person_id = _person_id_from(row)
        if not person_id or person_id in seen:
            continue

        name_node = block.css_first(".faculty-name")
        raw_name = _text(name_node)
        if not raw_name:
            continue
        seen.add(person_id)

        homepage = None
        if name_node is not None:
            link = name_node.css_first("a")
            if link is not None:
                href = link.attributes.get("href")
                if href:
                    homepage = urljoin(BASE_URL, href)

        interests = _text(block.css_first(".faculty-interests"))
        if interests:
            interests = _INTERESTS_PREFIX.sub("", interests).strip()

        contact = _contact_fields(block.css_first(".faculty-contact-info"))
        links = _links(block.css_first(".faculty-links"))

        records.append(
            FacultyRecord(
                person_id=person_id,
                full_name=display_name(raw_name),
                slug=make_slug(raw_name),
                title=_text(block.css_first(".faculty-title")),
                homepage_url=homepage,
                interests_raw=interests or None,
                **contact,
                **links,
            )
        )

    return records


@dataclass
class AreaMembership:
    """The roster of one research area, as person IDs."""

    slug: str
    name: str
    person_ids: list[str] = field(default_factory=list)


def parse_area_page(html: str, slug: str) -> AreaMembership:
    """Extract an area's faculty roster.

    Only the ``.area-faculty-wrap`` block is read, so unrelated photos elsewhere
    on the page cannot leak in as members.
    """
    tree = HTMLParser(html)
    person_ids: list[str] = []
    seen: set[str] = set()

    for wrap in tree.css(".area-faculty-wrap"):
        for bio in wrap.css(".faculty-bio"):
            person_id = _person_id_from(bio)
            if person_id and person_id not in seen:
                seen.add(person_id)
                person_ids.append(person_id)

    return AreaMembership(
        slug=slug, name=RESEARCH_AREAS.get(slug, slug), person_ids=person_ids
    )
