FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HUB_API_URL=http://backend:8000/api/v1/ping \
    BROWSER_PROFILE_DIR=/data/browser_profile \
    STATE_FILE_PATH=/data/scraper_state.json \
    APPS_CONFIG_PATH=/app/apps_config.yaml

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /data/browser_profile

COPY apps_config.yaml scraper_template.py auth_bootstrap.py ./

CMD ["python3", "scraper_template.py"]
