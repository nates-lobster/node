#!/usr/bin/env bash
set -euo pipefail

cd /home/user

if ! python3 -c "import fastapi, uvicorn, sqlmodel, httpx, pydantic_settings, yaml, playwright, websockets" >/dev/null 2>&1; then
  echo "[start_preview] Installing backend Python dependencies..."
  pip install --quiet -r /home/user/backend/requirements.txt
fi

if ! command -v brave-browser >/dev/null 2>&1; then
  if [ "$(uname -m)" = "x86_64" ] && command -v sudo >/dev/null 2>&1; then
    echo "[start_preview] Installing Brave Browser (Zero-Google streaming engine)..."
    (
      sudo curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg &&
      echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg arch=amd64] https://brave-browser-apt-release.s3.brave.com/ stable main" | sudo tee /etc/apt/sources.list.d/brave-browser-release.list >/dev/null &&
      sudo apt-get update -qq &&
      sudo apt-get install -y -qq brave-browser
    ) >/dev/null 2>&1 || true
  fi
fi

if ! command -v brave-browser >/dev/null 2>&1 && [ ! -d "/home/user/.cache/ms-playwright" ]; then
  echo "[start_preview] Ensuring Playwright runtime dependencies..."
  python3 -m playwright install chromium --with-deps >/dev/null 2>&1 || true
fi

export PYTHONPATH=/home/user/backend
exec python3 -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --proxy-headers \
  --forwarded-allow-ips="*"
