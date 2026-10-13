FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml alembic.ini docker-entrypoint.sh ./
COPY app ./app
COPY migrations ./migrations

RUN pip install --no-cache-dir . && chmod +x docker-entrypoint.sh

RUN useradd -m bider && chown -R bider:bider /app
USER bider

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
