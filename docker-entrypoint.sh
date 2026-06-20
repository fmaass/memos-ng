#!/bin/sh
set -e

# `command: worker` selects the media worker; default runs the app.
if [ "$1" = "worker" ]; then
    exec python -m app.worker
fi

# App: migrate then serve.
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
