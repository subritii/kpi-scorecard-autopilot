FROM python:3.11-slim

WORKDIR /srv

# Install dependencies first so this layer is cached when only app code changes.
COPY pyproject.toml .
RUN mkdir app && touch app/__init__.py && pip install --no-cache-dir . && rm -rf app

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
