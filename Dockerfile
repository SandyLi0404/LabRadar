# Single image for all three services -- api, scheduler and one-off CLI jobs.
# They differ only by command, so there is no service-specific code path that
# the others never exercise.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so source edits don't invalidate the install layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY frontend ./frontend
COPY curation ./curation

# Run as a non-root user; nothing here needs write access to the image.
RUN useradd --create-home --uid 10001 labradar && chown -R labradar:labradar /app
USER labradar

EXPOSE 8000

CMD ["uvicorn", "labradar.api:app", "--host", "0.0.0.0", "--port", "8000"]
