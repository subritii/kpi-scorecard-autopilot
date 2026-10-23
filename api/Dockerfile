# Built from the repo root (see docker-compose.yml) so the image can include
# prompts/ next to the API code: /srv/api/app and /srv/prompts, the same layout
# as the repo, which is what app/config.py's REPO_ROOT expects.
FROM python:3.11-slim

WORKDIR /srv/api

# Install dependencies first so this layer is cached when only app code changes.
COPY api/pyproject.toml .
RUN mkdir app && touch app/__init__.py && pip install --no-cache-dir . && rm -rf app

COPY api/app ./app
COPY prompts /srv/prompts

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
