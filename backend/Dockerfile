FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATABASE_URL=sqlite:////data/hub.db \
    OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=qwen3.5:2b

WORKDIR /app

# Install Brave Browser (amd64 Debian) for zero-Google server-side app streaming + Shields,
# plus Playwright system libs as clean fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg iproute2 libatomic1 \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
         && echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg arch=amd64] https://brave-browser-apt-release.s3.brave.com/ stable main" > /etc/apt/sources.list.d/brave-browser-release.list \
         && apt-get update \
         && apt-get install -y --no-install-recommends brave-browser; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /ms-playwright

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
