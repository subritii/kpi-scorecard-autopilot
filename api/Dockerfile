# Built from the repo root (see docker-compose.yml) so the image can include
# prompts/ next to the API code: /srv/api/app and /srv/prompts, the same layout
# as the repo, which is what app/config.py's REPO_ROOT expects.
FROM python:3.11-slim

WORKDIR /srv/api

# Exact dependency versions from the lock file, then the app itself without
# re-resolving dependencies. Cached unless the lock file changes.
COPY api/requirements.lock api/pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock \
    && mkdir app && touch app/__init__.py \
    && pip install --no-cache-dir --no-deps . \
    && rm -rf app

COPY api/app ./app
COPY prompts /srv/prompts

# Run as an unprivileged user; the code stays root-owned and read-only to it.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin api
USER api

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
