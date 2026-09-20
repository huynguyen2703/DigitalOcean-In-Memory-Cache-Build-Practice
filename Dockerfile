FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /code

COPY requirements.txt /code/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /code/

# Run as a non-root user (CIS Docker Benchmark / OWASP container hardening) — the app only
# needs write access to its own working directory for the SQLite file.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /code
USER appuser

EXPOSE 8080

# NOTE: exactly one worker/process, always. CacheService keeps its LRU cache and lock
# in-process (in-memory) — running multiple workers (or instance_count > 1 in app.yaml)
# would silently split traffic across independent, inconsistent caches.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8080"]
