#!/bin/sh
# Entrypoint: apply migrations (retrying while postgres is still coming up),
# then run the app. Mirrors the Repraiser model: schema applies itself on start.
set -e

attempts=0
max_attempts=20
until alembic upgrade head; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge "$max_attempts" ]; then
        echo "migrations failed after $attempts attempts, giving up" >&2
        exit 1
    fi
    echo "migrations not ready (attempt $attempts/$max_attempts), retrying in 3s..."
    sleep 3
done

exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
