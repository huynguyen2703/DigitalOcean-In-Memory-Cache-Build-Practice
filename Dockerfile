FROM python:3.13-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# Install dependencies first for layer caching
COPY requirements.txt /code/
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (Ensure .dockerignore exists in repo root)
COPY . /code/

# 1. Create a non-root user with an explicit UID/GID (>10000)
# 2. Create a dedicated writable /data directory for SQLite persistence
# 3. Keep /code owned by root so runtime process cannot mutate source files
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup --create-home --shell /usr/sbin/nologin appuser && \
    mkdir -p /data && chown -R appuser:appgroup /data

USER appuser

EXPOSE 8080

# Included --proxy-headers for reverse proxies (DigitalOcean App Platform / Nginx)
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]