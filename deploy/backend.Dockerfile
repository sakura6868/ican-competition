FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home app

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/gongxu ./gongxu
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./

RUN pip install ".[media]"

USER app
EXPOSE 8011
