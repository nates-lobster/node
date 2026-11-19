#!/usr/bin/env bash
# Sandbox convenience launcher (not the production entrypoint — see docs/DEPLOYMENT.md).
# Installs backend deps if missing, tries to provide a browser, then runs uvicorn on :8080.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PYTHON="python3"
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
fi

if ! "$PYTHON" -c "import fastapi, uvicorn, sqlmodel, httpx, pydantic_settings, yaml, playwright, websockets" >/dev/null 2>&1; then
  echo "[start_preview] Installing backend Python dependencies..."
  "$PYTHON" -m pip install --quiet -r "$REPO_ROOT/backend/requirements.txt"
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

PW_CACHE="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
if ! command -v brave-browser >/dev/null 2>&1 && [ ! -d "$PW_CACHE" ]; then
  echo "[start_preview] Ensuring Playwright runtime dependencies..."
  "$PYTHON" -m playwright install chromium --with-deps >/dev/null 2>&1 || true
fi

export PYTHONPATH="$REPO_ROOT/backend"
exec "$PYTHON" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --proxy-headers \
  --forwarded-allow-ips="*"
