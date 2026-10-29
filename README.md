# LabRadar

Find the research groups in Columbia's Computer Science department that are
actively publishing.

**Live site: https://sandyli0404.github.io/LabRadar/**

Department directories list every professor but say nothing about who is still
active. Publication databases know that, but aren't organized by department.
LabRadar joins the two and updates weekly.

## What it does

- **Professor cards.** Every research professor in the department, with their
  research areas, website, and lab.
- **Publication activity.** The number of papers published since last year and
  the year of the most recent one, with the date it was observed. Versions of
  one paper (preprint, conference, journal) count once.
- **Recent papers.** The two newest papers per professor, linked to arXiv when
  available, otherwise to the DOI.
- **Research groups.** Labs found on faculty homepages, each with the combined
  output of its members. Dead links and labs at other universities are dropped.
- **Search and filters.** By name, interest or research area, and sort by
  recent output. Professors with no recent papers are hidden by default, never
  deleted.

Lecturers and teaching professors are excluded, following the department
directory's own titles.

## How it works

1. **Directory**: scrape the department faculty directory and its 11 research
   area pages.
2. **Publications**: fetch each professor's papers from
   [OpenAlex](https://openalex.org). The professor-to-profile mapping is
   curated by hand in `curation/openalex_links.yaml`, because OpenAlex often
   splits one person across several profiles or merges two people who share a
   name.
3. **Deduplicate and count**: merge versions of the same paper, then compute
   activity and pick recent papers.
4. **Publish**: a GitHub Actions workflow runs the pipeline every Sunday,
   exports the results as JSON, and deploys the site to GitHub Pages.

## Repository structure

```
LabRadar/
├── src/labradar/
│   ├── sources/              # Data sources
│   │   ├── columbia.py       #   Faculty directory and area page parser
│   │   ├── openalex.py       #   OpenAlex API client (budget-aware)
│   │   └── labs.py           #   Lab extraction from faculty homepages
│   ├── jobs/                 # Pipeline steps, run in order by `labradar run-all`
│   │   ├── fetch_directory.py
│   │   ├── fetch_openalex.py
│   │   ├── fetch_labs.py
│   │   ├── compute_activity.py
│   │   ├── detect_changes.py
│   │   └── suggest_links.py  #   Proposes OpenAlex profiles for curation
│   ├── db/                   # SQLAlchemy models and session
│   ├── dedup.py              # Merges versions of the same paper
│   ├── activity.py           # Activity figures from deduplicated papers
│   ├── changes.py            # Week-over-week change detection
│   ├── matching.py           # Scores candidate OpenAlex profiles
│   ├── curation.py           # Reads and validates the curation file
│   ├── api.py                # FastAPI backend (/api/faculty, /api/labs, ...)
│   ├── export.py             # Writes the static site for GitHub Pages
│   ├── cli.py                # `labradar` command line
│   └── scheduler.py          # Weekly scheduler for the Docker setup
├── frontend/                 # The website: plain HTML, CSS and JavaScript
├── curation/
│   └── openalex_links.yaml   # Hand-curated professor → OpenAlex mapping
├── migrations/               # Alembic database migrations
├── tests/                    # pytest suite, runs offline on captured pages
├── .github/workflows/
│   └── pages.yml             # Weekly build and deploy to GitHub Pages
├── docker-compose.yml        # Postgres + API + scheduler for local use
└── Dockerfile
```

## Run locally

Requires Docker and a free [OpenAlex API key](https://openalex.org/settings/api).

```bash
echo "OPENALEX_API_KEY=your-key" > .env
docker compose up -d --build
docker compose run --rm api labradar run-all
```

Then open http://localhost:8000. To run the tests:

```bash
pip install -e ".[dev]"
pytest
```
