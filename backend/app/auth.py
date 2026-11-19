import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import Request, Response
from sqlmodel import Session, select

from .config import settings
from .models import AuthSession

SESSION_COOKIE_NAME = "node_session"
PBKDF2_ITERATIONS = 600_000


# Which routes each guard-token scope unlocks. A route belongs to exactly one
# scope. Anything not listed here requires a real login session.
GUARD_SCOPES: Dict[str, Dict[str, object]] = {
    "extension": {
        "label": "Browser extension",
        "used_by": "Node MV3 extension (new tab, quick capture, cookie sync). Paste into the extension's Settings.",
        "routes": {
            "/api/v1/ext/bootstrap",
            "/api/v1/ext/info",
            "/api/v1/ext/package.zip",
            "/api/v1/ext/quick-capture",
            "/api/v1/ext/cookies",
            "/api/v1/ext/session/import",
            "/api/v1/newtab/config",
        },
    },
    "focus_guard": {
        "label": "Focus guard",
        "used_by": "Focus guard extension and userscript. Embedded automatically when you download them.",
        "routes": {
            "/api/v1/focus-guard/heartbeat",
            "/api/v1/focus-guard/respond",
            "/api/v1/focus-guard/justify",
            "/api/v1/focus-guard/status",
        },
    },
    "worker": {
        "label": "Workers",
        "used_by": "Remote Ollama workers and scripts that push model lists or notification pings.",
        "routes": {
            "/api/v1/models/ping",
            "/api/v1/ping",
        },
    },
}

GUARD_ROUTE_SCOPES: Dict[str, str] = {
    route: scope for scope, meta in GUARD_SCOPES.items() for route in meta["routes"]  # type: ignore[union-attr]
}


def hash_password_pbkdf2(password: str, salt_hex: Optional[str] = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password_pbkdf2(password: str, stored_hash: str) -> bool:
    try:
        parts = stored_hash.strip().split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected_dk = bytes.fromhex(parts[3])
        computed_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(computed_dk, expected_dk)
    except Exception:
        return False


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ServerAuthService:
    """
    Server-side-only OS Lock Screen Authentication.
    - Credentials can ONLY be modified on the server (via `/data/auth_credentials.json` CLI
      or server `.env` variables `AUTH_USERNAME` / `AUTH_PASSWORD` / `AUTH_PASSWORD_HASH`).
    - Zero credentials or tokens are ever stored in client localStorage/JS; sessions use
      opaque 256-bit `HttpOnly` cookies whose SHA-256 digest is verified server-side.
    """

    def __init__(self) -> None:
        self._failed_attempts: Dict[str, List[float]] = {}
        self._cached_env_pw_tuple: Optional[Tuple[str, str]] = None

    @property
    def credentials_path(self) -> Path:
        return Path(settings.auth_credentials_path)

    @property
    def guard_token_path(self) -> Path:
        return Path(settings.auth_guard_token_path)

    def _get_effective_credentials(self) -> Tuple[str, str]:
        """Return `(username, pbkdf2_hash)` strictly from server config / server file."""
        # 1. Server-managed hashed credentials file written via `python -m app.auth_cli`
        cred_file = self.credentials_path
        if cred_file.is_file():
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8"))
                u = str(data.get("username") or "").strip()
                ph = str(data.get("password_hash") or "").strip()
                if u and ph.startswith("pbkdf2_sha256$"):
                    return u, ph
            except Exception:
                pass

        # 2. Explicit pre-hashed env var (`AUTH_PASSWORD_HASH`)
        env_user = (os.environ.get("NODE_AUTH_USERNAME") or os.environ.get("AUTH_USERNAME") or settings.auth_username).strip()
        env_hash = (os.environ.get("NODE_AUTH_PASSWORD_HASH") or os.environ.get("AUTH_PASSWORD_HASH") or settings.auth_password_hash).strip()
        if env_user and env_hash.startswith("pbkdf2_sha256$"):
            return env_user, env_hash

        # 3. Server `.env` password -> hashed once in server memory (or persisted if no file exists)
        raw_pw = os.environ.get("NODE_AUTH_PASSWORD") or os.environ.get("AUTH_PASSWORD") or settings.auth_password
        if self._cached_env_pw_tuple and self._cached_env_pw_tuple[0] == f"{env_user}:{raw_pw}":
            return env_user, self._cached_env_pw_tuple[1]

        computed_hash = hash_password_pbkdf2(raw_pw)
        self._cached_env_pw_tuple = (f"{env_user}:{raw_pw}", computed_hash)

        # Persist hashed credentials file on disk (0600) if none exists yet and not overridden by env
        if not cred_file.exists() and "AUTH_PASSWORD" not in os.environ and "NODE_AUTH_PASSWORD" not in os.environ:
            try:
                cred_file.parent.mkdir(parents=True, exist_ok=True)
                cred_file.write_text(
                    json.dumps(
                        {
                            "username": env_user,
                            "password_hash": computed_hash,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.chmod(cred_file, 0o600)
            except Exception:
                pass

        return env_user, computed_hash

    def write_server_credentials(self, username: str, password: str) -> Path:
        """Used strictly by server-side CLI (`python -m app.auth_cli`). Never exposed over HTTP."""
        clean_user = username.strip()
        if not clean_user or len(password) < 4:
            raise ValueError("Username must be non-empty and password at least 4 characters.")
        ph = hash_password_pbkdf2(password)
        p = self.credentials_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "username": clean_user,
                    "password_hash": ph,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return p

    def extract_client_key(self, request: Request) -> str:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check_rate_limit(self, client_key: str) -> Optional[int]:
        """Return seconds remaining if locked out due to brute-force attempts, else None."""
        now = time.monotonic()
        window = 300.0
        attempts = [t for t in self._failed_attempts.get(client_key, []) if (now - t) < window]
        self._failed_attempts[client_key] = attempts
        if len(attempts) >= 8:
            oldest = attempts[0]
            return max(1, int(window - (now - oldest)))
        return None

    def record_failure(self, client_key: str) -> None:
        now = time.monotonic()
        arr = self._failed_attempts.setdefault(client_key, [])
        arr.append(now)

    def clear_failures(self, client_key: str) -> None:
        self._failed_attempts.pop(client_key, None)

    def verify_credentials(self, username: str, password: str) -> Optional[str]:
        expected_user, expected_hash = self._get_effective_credentials()
        user_ok = hmac.compare_digest(
            username.strip().lower().encode("utf-8"),
            expected_user.strip().lower().encode("utf-8"),
        )
        pw_ok = verify_password_pbkdf2(password, expected_hash)
        if user_ok and pw_ok:
            return expected_user
        return None

    def extract_request_token(self, request: Request, allow_query_token: bool = False) -> Optional[str]:
        hdr_tok = request.headers.get("x-node-session")
        if hdr_tok and len(hdr_tok.strip()) >= 16:
            return hdr_tok.strip()
        cookie_tok = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_tok and len(cookie_tok.strip()) >= 16:
            return cookie_tok.strip()
        if allow_query_token:
            q_tok = request.query_params.get("_st")
            if q_tok and len(q_tok.strip()) >= 16:
                return q_tok.strip()
        return None

    def create_session(self, username: str, db: Session, request: Request) -> str:
        raw_token = secrets.token_urlsafe(32)
        token_hash = _sha256_hex(raw_token)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=max(1, int(settings.auth_session_days)))

        # Prune expired sessions opportunistically
        expired_rows = db.exec(select(AuthSession).where(AuthSession.expires_at < now)).all()
        for r in expired_rows:
            db.delete(r)

        ua = (request.headers.get("user-agent") or "")[:240]
        row = AuthSession(
            token_hash=token_hash,
            username=username,
            created_at=now,
            expires_at=expires,
            last_seen_at=now,
            client_info=ua,
        )
        db.add(row)
        db.commit()
        return raw_token

    def verify_session_token(self, raw_token: Optional[str], db: Session) -> Optional[AuthSession]:
        if not raw_token or len(raw_token) < 16:
            return None
        token_hash = _sha256_hex(raw_token)
        row = db.get(AuthSession, token_hash)
        if not row:
            return None
        now = datetime.now(timezone.utc)
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            db.delete(row)
            db.commit()
            return None
        return row

    def revoke_session(self, raw_token: Optional[str], db: Session) -> None:
        if not raw_token:
            return
        token_hash = _sha256_hex(raw_token)
        row = db.get(AuthSession, token_hash)
        if row:
            db.delete(row)
            db.commit()

    def attach_session_cookie(self, response: Response, raw_token: str, request: Request) -> None:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").lower()
        is_https = "https" in proto
        max_age = max(1, int(settings.auth_session_days)) * 86400
        if is_https:
            # Support both standalone HTTPS domains (Cloudflare Tunnel) and cross-site HTTPS preview iframes (CHIPS)
            cookie_val = (
                f"{SESSION_COOKIE_NAME}={raw_token}; Path=/; Max-Age={max_age}; "
                "HttpOnly; Secure; SameSite=None; Partitioned"
            )
            response.headers.append("Set-Cookie", cookie_val)
        else:
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=raw_token,
                max_age=max_age,
                httponly=True,
                secure=False,
                samesite="lax",
                path="/",
            )

    def clear_session_cookie(self, response: Response, request: Request) -> None:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").lower()
        is_https = "https" in proto
        if is_https:
            response.headers.append(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=None; Partitioned",
            )
        else:
            response.delete_cookie(
                key=SESSION_COOKIE_NAME,
                path="/",
                httponly=True,
                secure=False,
                samesite="lax",
            )

    # ------------------------------------------------------------------
    # Scoped guard tokens
    #
    # Clients that cannot hold a session cookie (the MV3 extension, the focus
    # guard extension/userscript, remote model workers) authenticate with an
    # `X-Node-Guard-Token` header instead. Each client family has its OWN token
    # that unlocks only its own routes, so a token leaked from one place (e.g.
    # a userscript embedded in YouTube pages) cannot read the inbox or push
    # cookies into the bridge store.
    #
    # The `extension` token is the original single token and keeps its file
    # (`settings.auth_guard_token_path`) so an already-configured extension
    # keeps working. Installs that pre-date scoping start in "legacy shared"
    # mode: the extension token is also accepted on the focus-guard and worker
    # routes until the user ends it (Settings -> System -> Device tokens, or
    # `auth_cli guard-tokens end-legacy`). Fresh installs never enter it.
    # ------------------------------------------------------------------

    @property
    def guard_tokens_path(self) -> Path:
        explicit = str(getattr(settings, "auth_guard_tokens_path", "") or "").strip()
        if explicit:
            return Path(explicit)
        return self.guard_token_path.with_name("guard_tokens.json")

    @staticmethod
    def _write_secret_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(content, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)

    def _read_extension_token(self) -> Optional[str]:
        p = self.guard_token_path
        if p.is_file():
            tok = p.read_text(encoding="utf-8").strip()
            if len(tok) >= 24:
                return tok
        return None

    def get_or_create_guard_token(self) -> str:
        """The `extension` scope token (the original single guard token)."""
        tok = self._read_extension_token()
        if tok:
            return tok
        tok = secrets.token_urlsafe(32)
        self._write_secret_file(self.guard_token_path, tok)
        return tok

    def _load_guard_store(self) -> Dict[str, object]:
        p = self.guard_tokens_path
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_guard_store(self, data: Dict[str, object]) -> None:
        self._write_secret_file(self.guard_tokens_path, json.dumps(data, indent=2))

    def ensure_guard_tokens(self) -> Dict[str, object]:
        """Create any missing scoped tokens. Idempotent; called on boot and lazily."""
        store = self._load_guard_store()
        changed = False
        if not store:
            # First boot with scoping: an existing extension token means this
            # install already handed that one token to every client.
            store = {
                "version": 1,
                "legacy_shared": self._read_extension_token() is not None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            changed = True
        for scope in GUARD_SCOPES:
            if scope == "extension":
                continue
            tok = str(store.get(scope) or "")
            if len(tok) < 24:
                store[scope] = secrets.token_urlsafe(32)
                changed = True
        if changed:
            self._save_guard_store(store)
        self.get_or_create_guard_token()
        return store

    def get_guard_token(self, scope: str) -> str:
        if scope not in GUARD_SCOPES:
            raise ValueError(f"Unknown guard token scope: {scope}")
        if scope == "extension":
            return self.get_or_create_guard_token()
        return str(self.ensure_guard_tokens()[scope])

    def guard_legacy_shared(self) -> bool:
        return bool(self.ensure_guard_tokens().get("legacy_shared"))

    def end_guard_legacy(self) -> None:
        store = self.ensure_guard_tokens()
        if store.get("legacy_shared"):
            store["legacy_shared"] = False
            store["legacy_ended_at"] = datetime.now(timezone.utc).isoformat()
            self._save_guard_store(store)

    def rotate_guard_token(self, scope: str) -> str:
        if scope not in GUARD_SCOPES:
            raise ValueError(f"Unknown guard token scope: {scope}")
        tok = secrets.token_urlsafe(32)
        if scope == "extension":
            self._write_secret_file(self.guard_token_path, tok)
            # The shared legacy value no longer exists, so legacy mode is over.
            self.end_guard_legacy()
            return tok
        store = self.ensure_guard_tokens()
        store[scope] = tok
        self._save_guard_store(store)
        return tok

    def describe_guard_tokens(self) -> Dict[str, object]:
        store = self.ensure_guard_tokens()
        return {
            "legacy_shared": bool(store.get("legacy_shared")),
            "scopes": [
                {
                    "id": scope,
                    "label": meta["label"],
                    "used_by": meta["used_by"],
                    "routes": sorted(meta["routes"]),
                    "token": self.get_guard_token(scope),
                }
                for scope, meta in GUARD_SCOPES.items()
            ],
        }

    @staticmethod
    def guard_scope_for_path(path: str) -> Optional[str]:
        return GUARD_ROUTE_SCOPES.get(path)

    def verify_guard_token(self, candidate: Optional[str], scope: str = "extension") -> bool:
        """True only if `candidate` is the token for `scope` (or the legacy shared token)."""
        if not candidate or scope not in GUARD_SCOPES:
            return False
        got = candidate.strip().encode("utf-8")
        if hmac.compare_digest(got, self.get_guard_token(scope).encode("utf-8")):
            return True
        if scope != "extension" and self.guard_legacy_shared():
            return hmac.compare_digest(got, self.get_or_create_guard_token().encode("utf-8"))
        return False

    def revoke_all_sessions(self, db: Session) -> int:
        """Server-side only (auth_cli). Deletes every browser session."""
        rows = db.exec(select(AuthSession)).all()
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)

    def credential_source(self) -> str:
        cred_file = self.credentials_path
        if cred_file.is_file():
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8"))
                if str(data.get("password_hash") or "").startswith("pbkdf2_sha256$"):
                    return f"credentials file ({cred_file})"
            except Exception:
                pass
        env_hash = (os.environ.get("NODE_AUTH_PASSWORD_HASH") or os.environ.get("AUTH_PASSWORD_HASH") or settings.auth_password_hash).strip()
        if env_hash.startswith("pbkdf2_sha256$"):
            return "NODE_AUTH_PASSWORD_HASH environment variable"
        if os.environ.get("NODE_AUTH_PASSWORD") or os.environ.get("AUTH_PASSWORD"):
            return "NODE_AUTH_PASSWORD environment variable"
        return "built-in default (admin / changeme) - change this before exposing the server"


auth_service = ServerAuthService()
