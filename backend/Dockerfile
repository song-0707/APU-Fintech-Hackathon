FROM python:3.11-slim

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/storage \
    && chown -R appuser:appuser /code /data

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser start.sh ./start.sh
RUN chmod +x ./start.sh

USER appuser

EXPOSE 8000

# docker-compose's fastapi-backend service (no `command:` override — see
# docker-compose.yml) relies on this default and runs alongside its own
# separate celery-worker service, so this stays plain uvicorn. Render's web
# service overrides the start command to ./start.sh instead, which also
# launches the worker in the same container — see start.sh for why.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
