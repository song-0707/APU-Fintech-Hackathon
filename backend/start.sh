#!/bin/sh
# Render's persistent disks attach to a single service — the web process and
# the Celery worker both need to read/write the same SQLite file and the
# same storage/chroma directories, so for this deployment they run together
# in one container instead of as separate Render services. If this ever
# needs to scale past one dyno, split the worker back out and move
# STORAGE_PATH/DATABASE_URL to something network-shared (S3-compatible
# storage + Postgres) first — a second container would otherwise silently
# diverge from this one's disk.
set -e

celery -A app.core.celery_app worker --loglevel=info &

# Render assigns the port to bind via $PORT; it is not always 8000.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
