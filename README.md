# LabRadar

Find active research groups in the Columbia CS department — and tell them apart
from the ones that have quietly stopped publishing.

Department directories list everyone forever and say nothing about recency.
Publication databases have the activity data but aren't organized by department.
LabRadar joins the two.

**It reports, it does not judge.** Every activity figure is a dated observation
("23 papers since 2025, most recent 2025, as of 2026-09-22"), never an
`active: true/false` verdict. The recency threshold belongs to the student using
it. Nobody is ever auto-deleted: a professor who leaves the directory is flagged
and kept, because "who left" is one of the questions worth asking.

---

## Quick start

```bash
# Register a free OpenAlex key (~30s) and paste it into .env:
#   https://openalex.org/settings/api
export OPENALEX_API_KEY=...

docker compose up -d          # postgres + migrations + api + weekly scheduler
docker compose run --rm api labradar run-all
open http://localhost:8000
```

Without Docker, you need a reachable Postgres and then:

```bash
pip install -e ".[dev]"
alembic upgrade head
labradar run-all
uvicorn labradar.api:app --reload
```

## Commands

| Command | What it does |
|---|---|
| `labradar fetch-directory` | Scrape the faculty directory and the 11 area pages |
| `labradar fetch-openalex` | Refresh works for every **confirmed** OpenAlex link |
| `labradar fetch-labs` | Discover research groups from faculty homepages |
| `labradar compute-activity` | Deduplicate works, derive signals, rebuild search |
| `labradar detect-changes` | Diff the latest run against the previous one |
| `labradar run-all` | The full chain, as the scheduler runs it |
| `labradar link` | Propose OpenAlex candidates for unmapped faculty |
| `labradar review` | Show pending proposals and the evidence behind them |
| `labradar validate-curation` | Check the mapping file parses and is consistent |
| `labradar export-static DIR` | Write the site as static files (used for GitHub Pages) |

The scheduler calls exactly these commands, so there is no code path that only
the scheduler exercises.

---

## Who the app includes

**Research professors only.** The department directory's title is the ground
truth: anyone it lists as a Lecturer or Teaching Professor is dropped at ingest
and never enters the database. That is a policy, not an inference, so it holds
even for a lecturer who publishes -- see `tests/test_appointments.py`.

| | Count |
|---|---|
| Research professors | **61** |
| Excluded at ingest | 11 (8 lecturers, 3 teaching professors) |

---

## The activity signal

One number with the year it starts from: **papers published since a cutoff
year**, plus the most recent publication year.

The cutoff spans the current calendar year and the one before it — so on
2026-09-22 it is `since 2025`, covering 2025 and 2026. Publication data is
year-granularity and OpenAlex's index lags real publication by months, so
counting only the current year reports working researchers as silent.
`ACTIVITY_LOOKBACK_YEARS` in `activity.py` holds that span.

**The label is generated from the data.** `/api/faculty` returns `since_year`
in the same payload as `works_since`, and the interface renders the label from
it. An earlier version hardcoded the words "last year" against a figure that
actually covered two, so a card read "5 papers last year" when one of those
papers was from the current year. A label and a count taken from one value
cannot drift apart.

Preprints count as publications. They are 30–43% of papers every year, and in
CS they are how work is released; excluding them would badly understate theory
and ML groups. A preprint and its published version already merge into one
paper when both exist.

Two states are kept distinct and must never be conflated:

- **inactive** — linked to a publication record, nothing since the cutoff.
- **unknown** — nobody has linked a publication record yet. Not a finding.

The default view hides inactive professors and those with no website; both have
a toggle, and nobody is deleted. `include_inactive` never applies to unknown
people.

---

## Two data sources, different confidence

**The department directory is the seed layer** and is treated as authoritative
for identity. It is server-rendered HTML, permitted by `robots.txt`, and every
entry embeds a stable internal `personID` — used as the natural key throughout,
because names collide and change.

Faculty-to-area membership comes from the **11 area pages**, each of which lists
its own roster, rather than from parsing the free-text interests line. The
relation is sourced, not guessed. (Area rosters are a superset of the directory:
cross-listed affiliates appear there with no directory entry. They are counted
in the run stats rather than silently dropped.)

**OpenAlex is the activity layer.** Since February 2026 it meters usage in
dollars and a free API key is required — `mailto` no longer buys any headroom.
Measured per-request costs, which drive how the fetch job is written:

| Request | Cost |
|---|---|
| `GET /authors/{id}` | **$0.00** — free and uncapped |
| `GET /authors?filter=` | $0.0001 |
| `GET /authors?search=` | $0.0010 |

A full refresh costs roughly a cent against a $1/day allowance. Search is
confined to the curation tool, where a human is choosing between candidates.

> DBLP was the original plan and is not usable: every dblp.org host, including
> the bulk XML dumps, now sits behind Anubis proof-of-work bot protection and
> returns a challenge page instead of data.

---

## The two things that are easy to get wrong

### 1. One researcher, many OpenAlex profiles

OpenAlex splits people across duplicate author IDs. Filtering to
Columbia-affiliated authors only:

| Faculty | Candidate profiles at Columbia |
|---|---|
| Kathleen McKeown | **13** — one real record of 466 works, plus a dozen single-paper fragments |
| Josh Alman | **7** |
| Steven Feiner | **2** |

Every one of those fragments reports a current last-publication year. A "take the
freshest match" heuristic would therefore rank someone who has stopped publishing
as the most active person in the department.

So the mapping is **curated by hand** in
[`curation/openalex_links.yaml`](curation/openalex_links.yaml), which lives in
git so every identity decision has an author, a diff and a review. It is
many-to-many, and it records three states:

- `confirmed` — counted
- `candidate` — proposed, **not** counted; a human still has to decide
- `rejected` — never counted, and the suggester stops re-proposing it

Rejections are load-bearing. Without them the same wrong fragment comes back
every week.

### 2. `counts_by_year` counts records, not papers

OpenAlex stores the arXiv preprint, the conference version and the journal
article as three separate records, each with **its own DOI and often its own
year**. Measured over Josh Alman's real corpus: 65 records, 46 distinct papers,
16 titles appearing more than once — one appearing three times, spread across
2020, 2021 and 2024.

| | Raw records | Deduplicated |
|---|---|---|
| Whole corpus | 65 | **47** |
| Since 2025 (the app's cutoff) | 14 | **13** |

Both rows are pinned in `tests/test_dedup.py`. The keying-scheme guards use the
whole corpus: inside one short window most versions share a year, so a broken
scheme can land on the right count by luck.

Two obvious keying schemes both fail on this data: **DOI-first merges nothing**
(16/16 duplicate groups have differing DOIs) and **`title||year` fails on 12 of
16** (versions straddle year boundaries). So clustering keys on the normalized
title with a year tolerance, and DOI is used only as a fast path for identical
records — never as a splitter. `tests/test_dedup.py` pins all of this, including
guards that fail loudly if either rejected scheme is ever reinstated.

The interface shows the correction rather than applying it silently: "17 papers
· from 20 records".

---

## Architecture

```
cs.columbia.edu ──┐
  directory       │  seed layer (identity + areas, authoritative)
  11 area pages   │
                  ▼
api.openalex.org ─┤  activity layer (works paged, deduped at paper level)
                  ▼
   jobs/  fetch_directory → fetch_openalex → compute_activity → detect_changes
                  │        (all writing under one fetch_run id)
                  ▼
   PostgreSQL — append-mostly; history IS the storage model, "current" is a view
                  ▼
   FastAPI /api/* + a no-build HTML/JS frontend
```

Stack: Python 3.12+ · FastAPI · SQLAlchemy 2.0 + Alembic · Postgres 17 ·
httpx · selectolax · APScheduler. The frontend is vanilla HTML/CSS/JS with no
build step.

### Why history is the storage model

Scraped facts are immutable observations tied to a `fetch_run`. Current state is
`WHERE valid_to IS NULL`. Nothing is updated in place; nothing is deleted.

That choice pays for itself twice: change detection becomes a plain diff of run
N against N−1 with no separate snapshot store, and "when did this change?" stays
answerable forever.

| Table | Holds |
|---|---|
| `fetch_run` | One row per job execution; every observation points back to one |
| `faculty` / `faculty_version` | Identity, and a new version row only when the content hash moves |
| `faculty_area` | The many-to-many, historied, tagged with its `source` |
| `faculty_openalex_link` | The curated mapping, mirrored from the YAML file |
| `openalex_work` / `openalex_author_work` | Work records and authorship, with run bounds |
| `faculty_work_cluster` | The materialized dedup result — one row per real paper |
| `faculty_activity` | Derived signals, with both deduped and raw counts |
| `change_event` | What moved between two runs |
| `faculty_search` | Weighted `tsvector`, rebuilt per run |

Two details worth knowing:

- **Stale works stop counting.** `compute_activity` counts only works whose
  authorship was seen in the latest OpenAlex run, so a work that gets reassigned
  stops contributing — while the row is retained so history still sees it.
- **`faculty_search` is a table, not a generated column.** The document spans
  three tables, and Postgres generated columns can only depend on their own row.
  Rebuilding ~70 rows per run is simpler than triggers across all three.

### Change detection is deliberately quiet

An over-sensitive rule emits an event for most of the department every week,
which means nobody reads the feed and a real departure goes unnoticed. An
`activity_shift` fires only when:

- the last publication year moves, **or**
- the last-year count moves by at least `ACTIVITY_SHIFT_MIN_DELTA` papers **and**
  at least `ACTIVITY_SHIFT_MIN_PCT` of its previous value, **or**
- someone crosses "nothing since the cutoff year" in either direction — always
  reported, because that is the transition students actually care about.

A prolific researcher drifting 40 → 42 is noise. 2 → 0 is not.

### Unknown is not the same as inactive

Someone with no curated OpenAlex profile has `linked_author_count = 0`. The API
returns `is_unmapped: true` and the UI says *"Publication record not yet linked
— unknown"*, never *inactive*. Recency filters keep unmapped people by default;
excluding them is an explicit choice (`include_unmapped=false`).

---

## Curating the mapping

```bash
labradar link --suggest --limit 10     # ~$0.01 of budget
labradar validate-curation
```

`--suggest` searches Columbia-scoped candidates and prints each with its work
count, most recent year and topics, plus a ready-to-paste YAML block. It writes
nothing: a human pastes the decision into the file so it goes through review.

43 of the 72 directory entries have a confirmed link, including McKeown with
her rejected fragments. The rest are unmapped and show as *unknown* until
curated.

---

## Tests

```bash
pytest          # 264 tests, no network access
```

The suite runs against captured copies of the real pages and real OpenAlex
responses, so it fails if the department changes its markup or if the dedup rule
drifts from the measurements that justified it.

| File | Covers |
|---|---|
| `test_dedup.py` | The dedup rule, pinned to measured numbers, with guards against both rejected keying schemes |
| `test_columbia.py` | Directory and area-page parsing against real HTML |
| `test_openalex.py` | Key requirement, budget accounting, cursor paging |
| `test_changes.py` | Threshold behaviour — including that ±1 paper jitter emits nothing |
| `test_curation_file.py` | That every curated `personID` is real and names match the directory |

That last one exists for a reason: during development two person IDs were taken
from an area roster instead of being looked up, which would have credited a
466-work publication record to the wrong researcher. A wrong ID is invisible —
it produces a plausible number attached to the wrong person.

---

## Status

Implemented: directory and area ingest, curated OpenAlex linking, paper-level
dedup, activity signals, search and recency filtering, change detection, the
weekly scheduler, and research-group discovery from faculty homepages
(`fetch-labs`, stored separately with a confidence and source URL).

Not yet built: soft signals from lab pages such as "recruiting students". When
added, those render as low-confidence with a source URL and date.

The public site is published to GitHub Pages; see below. Docker Compose
remains the way to run the full app with its live API.

---

## Publishing to GitHub Pages

Pages serves files only, so the public site is a weekly snapshot rather than a
live API. [`.github/workflows/pages.yml`](.github/workflows/pages.yml) runs the
full pipeline against a throwaway Postgres, then `labradar export-static`
writes every record to `data/*.json` beside the frontend. The page sees it is
marked `data-source="static"` and applies the API's filter rules in the
browser -- same filters, same ordering, checked against the API side by side.

It runs on Sundays at 03:00 UTC, on every push to `main` that touches the app,
and by hand from the Actions tab. Each run costs about a cent of OpenAlex
allowance.

Because change detection diffs a run against the previous one, the database
survives between runs: it is dumped to the `data` branch (one force-pushed
commit, about 1 MB) and restored at the start of the next run.

One-time setup:

1. Add the OpenAlex key as an Actions secret named `OPENALEX_API_KEY`.
2. Settings → Pages → Source: **GitHub Actions**.
3. Run the workflow once from the Actions tab.

GitHub disables scheduled workflows in a repository with no commits for 60
days; a commit or a manual run re-enables them.
