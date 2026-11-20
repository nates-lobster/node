# Deployment & Git

Two things this doc answers:

1. **How you (the human) make the first commit and push.**
2. **How code updates work later without ever touching your data.**

---

## 1. The model: code in git, data on the server

```
repo (git)                         server (not in git)
├── backend/                       data/
├── dashboard/                       ├── hub.db            ← your whole life
├── extension/                       ├── uploads/
├── docs/                            ├── browser_profile/  ← logged-in sessions
├── docker-compose.yml               ├── apps_config.yaml  ← installed apps
├── .env.example                     ├── auth_credentials.json
└── AGENT.md                         ├── guard_token.secret / guard_tokens.json
                                     ├── os_settings.json
                                     └── ...
```

`data/` is gitignored **and** mounted from the host in `docker-compose.yml`, so
it survives `git pull`, `docker compose build`, and `docker compose down -v`.
Updating = replace the code, keep the folder.

**Rule:** if a change requires touching `data/` by hand, it isn't done — put a
migration in `database.py` instead.

---

## 2. First commit (do this once)

All commands run in the project folder.

### 2.1 Check what will be committed

```bash
git init
git add -A
git status --short            # eyeball the list
git status --short | grep -E '^\s*A\s+data/' && echo "STOP: data/ is staged"
```

If nothing under `data/` appears, you're good. Expect ~84 files, ~4 MB.

### 2.2 Set your identity (once per machine)

```bash
git config --global user.name  "Max"
git config --global user.email "you@example.com"
```

### 2.3 Make the commit

```bash
git commit -m "chore: Node v0.11.0 — initial commit

Self-hosted personal WebOS: tiling desktop shell, tag-first mail client,
multi-LMS grades, server-side AI secretary with a d1-2B decision gate,
learned-API bridge (api-anything), and an MV3 new-tab extension.

Data lives in data/ and is deliberately excluded from git."
```

### 2.4 Create the remote and push

Create an **empty** repo on GitHub (no README, no .gitignore — you have both):

```bash
git branch -M main
git remote add origin git@github.com:<you>/node.git
git push -u origin main
```

HTTPS alternative:
```bash
git remote add origin https://github.com/<you>/node.git
git push -u origin main
```

> **Private repo recommended.** Even though `data/` is excluded, the repo reveals
> your architecture and, potentially, usernames in commit metadata.

### 2.5 Sanity checks after the push

```bash
git ls-files | grep -c '^data/'        # must print 0
git ls-files | wc -l                   # ~84
git log --oneline -1
```

---

## 3. Updating the server later (the pull workflow)

The server keeps its `.env` and `data/`. Only code changes.

```bash
cd /path/to/node
git pull --ff-only          # fails loudly instead of making a merge mess
docker compose up -d --build
docker compose logs -f hub-backend
```

That's it. Migrations run automatically on boot (`database.init_db()` is
idempotent).

### 3.1 If a release says it needs manual steps

Check `CHANGELOG.md` under that version. Typical examples:

- a new env var → copy the line from `.env.example` into your `.env`, restart
- Node upgrade (api-anything needs ≥ 22.13)
- a one-time backfill → the changelog gives the command

### 3.2 Rolling back

```bash
git log --oneline -10
git checkout <good-sha>
docker compose up -d --build
```

Your data is untouched by a rollback. If a migration added columns, the old code
simply ignores them.

---

## 4. Running without Docker (plain systemd)

Useful for the sandbox or a quick test.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
PYTHONPATH=backend uvicorn app.main:app --host 0.0.0.0 --port 8080
```

`/etc/systemd/system/node.service`:

```ini
[Unit]
Description=Node WebOS
After=network.target

[Service]
WorkingDirectory=/opt/node
EnvironmentFile=/opt/node/.env
Environment=PYTHONPATH=/opt/node/backend
ExecStart=/opt/node/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now node
```

---

## 5. Credentials

**Never in the browser, never in git.** Three sources, in priority order:

1. `data/auth_credentials.json` — PBKDF2 hash, written by the CLI:
   ```bash
   docker exec -it hub-backend python -m app.auth_cli set max
   docker exec -it hub-backend python -m app.auth_cli status
   docker exec -it hub-backend python -m app.auth_cli revoke-sessions
   ```
2. `NODE_AUTH_PASSWORD_HASH` in `.env` (pre-hashed).
3. `NODE_AUTH_USERNAME` / `NODE_AUTH_PASSWORD` in `.env` (hashed in memory on
   first boot; persisted if no credential file exists).

Defaults are `admin` / `changeme`. **Change them before exposing the server.**

### 5.0 Device (guard) tokens

Clients that can't sign in use scoped tokens instead, one per client family
(`extension`, `focus_guard`, `worker`), each limited to its own routes. Manage
them in **Settings → System → Device tokens**, or over SSH:

```bash
docker exec -it hub-backend python -m app.auth_cli guard-tokens            # list (full tokens)
docker exec -it hub-backend python -m app.auth_cli guard-tokens rotate worker
docker exec -it hub-backend python -m app.auth_cli guard-tokens end-legacy
```

If a token leaks, rotate just that scope; the other clients keep working.

### 5.1 The sandbox instance

The workspace you were handed runs with:
- user `admin`, password `changeme`
- auth enabled, DB at `data/hub.db`
- no `.env` file (defaults in code)

That is throwaway. A real deployment must set its own.

---

## 6. Environment variables that matter

| Var | Purpose |
|---|---|
| `BIND_IP`, `DASHBOARD_PORT` | front door |
| `NODE_AUTH_ENABLED`, `NODE_AUTH_USERNAME`, `NODE_AUTH_PASSWORD` | lock screen |
| `AUTH_GUARD_TOKEN_PATH` | where the `extension` guard token is stored (the other scopes go in `guard_tokens.json` beside it) |
| `TZ` | authoritative local time (calendar, cron, weekdays) |
| `DATABASE_URL` | override the SQLite path |
| `OLLAMA_BASE_URL` | the Ollama server serving both models |
| `CHAT_MODEL` | chat model (empty/`auto` = discover; `OLLAMA_MODEL` still honoured) |
| `DECISION_MODEL` | decision model tag (`auto` = the `d1` tag Ollama has) |
| `DECISION_BASE_URL`, `DECISION_API_KEY` | only for a separate System One server (default: Ollama's `/v1/systemone`) |
| `DECISION_TIMEOUT_SECONDS` | default 12; covers cold-loading d1 |
| `API_ANYTHING_TOKEN` | shared secret for the api-anything bridge |
| `SEED_DEMO_DATA` | **must stay `false`** |
| `TAILSCALE_SOCK_DIR` | optional tailscaled LocalAPI mount |

Full annotated list: `.env.example`.

---

## 7. Backups (do this)

The entire product is one file plus a folder.

```bash
# nightly
tar czf ~/node-backup-$(date +%F).tgz \
  --exclude='browser_profile' \
  /opt/node/data/hub.db* /opt/node/data/*.json /opt/node/data/uploads
```

Include `browser_profile/` only if you are willing to store logged-in session
material. `hub.db` alone restores all mail, grades, tasks and settings.

---

## 8. Giving another agent control

The agent works on the same repo. To let it operate safely:

1. Add `AGENT.md` (already at the root) to its context or working directory — it
   is the entry point and assumes zero prior knowledge.
2. It works on a branch, opens a PR into `main`, and — by your decision
   (2026-11) — merges it itself once the checks it can run are green.
3. You pull on the server (`git pull --ff-only && docker compose up -d --build`)
   after reading `CHANGELOG.md` for manual steps. The agent never pushes directly
   to `main`, never force-pushes, and never touches `data/`.
4. If it needs a secret, you put it in `.env` on the server — not in the repo, not
   in chat.

**What the agent can verify without you:** `pytest` (49 tests; the 5 that need a
real browser skip in sandboxes without one), import checks, `node --check`, and —
where a browser can be installed — a real headless browser against a running server. Expect it to
run those before claiming anything works.
