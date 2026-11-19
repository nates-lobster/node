"""
Server-only CLI for managing Node Lock Screen credentials over SSH (`max@athlon`).
Credentials can NEVER be modified from any browser/client API.

Usage inside container or host:
  docker exec -it hub-backend python -m app.auth_cli set <username>
  docker exec -it hub-backend python -m app.auth_cli hash
  docker exec -it hub-backend python -m app.auth_cli status
  docker exec -it hub-backend python -m app.auth_cli revoke-sessions
  docker exec -it hub-backend python -m app.auth_cli guard-tokens [list|rotate <scope>|end-legacy]
"""

import argparse
import getpass
import sys
from datetime import datetime, timezone

from sqlmodel import Session, select

from .auth import GUARD_SCOPES, auth_service, hash_password_pbkdf2


def _print_guard_tokens(show_secrets: bool) -> None:
    info = auth_service.describe_guard_tokens()
    for scope in info["scopes"]:
        tok = scope["token"] if show_secrets else scope["token"][:6] + "…"
        print(f"  {scope['id']:<12} {tok}")
        print(f"  {'':<12} {scope['used_by']}")
    if info["legacy_shared"]:
        print(
            "\n  Legacy shared mode is ON: the extension token is still accepted on focus-guard and\n"
            "  worker routes. Re-download the focus guard and update worker scripts, then run\n"
            "  `python -m app.auth_cli guard-tokens end-legacy`."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Node Server-Only Credential Manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="Set username & PBKDF2-SHA256 hashed password in /data/auth_credentials.json")
    p_set.add_argument("username", help="Login username")
    p_set.add_argument("--password", help="Optional password (omit to prompt securely via getpass)")

    p_hash = sub.add_parser("hash", help="Generate a PBKDF2-SHA256 hash string for .env AUTH_PASSWORD_HASH")
    p_hash.add_argument("--password", help="Optional password (omit to prompt securely)")

    sub.add_parser("status", help="Show the active credential source, live sessions and guard-token state")
    sub.add_parser("revoke-sessions", help="Sign out every browser immediately")

    p_guard = sub.add_parser("guard-tokens", help="List, rotate, or finish migrating scoped guard tokens")
    p_guard.add_argument("action", nargs="?", default="list", choices=["list", "rotate", "end-legacy"])
    p_guard.add_argument("scope", nargs="?", choices=sorted(GUARD_SCOPES), help="Scope to rotate")

    args = parser.parse_args()

    if args.cmd == "set":
        pw = args.password
        if not pw:
            pw = getpass.getpass(f"New password for '{args.username}': ")
            pw2 = getpass.getpass("Confirm password: ")
            if pw != pw2:
                print("Error: Passwords do not match.", file=sys.stderr)
                return 1
        out_path = auth_service.write_server_credentials(args.username, pw)
        print(f"Saved PBKDF2-HMAC-SHA256 credentials for '{args.username}' to {out_path}")
        return 0

    if args.cmd == "hash":
        pw = args.password or getpass.getpass("Password to hash: ")
        print(hash_password_pbkdf2(pw))
        return 0

    if args.cmd in ("status", "revoke-sessions"):
        from .database import engine, init_db
        from .models import AuthSession

        init_db()
        with Session(engine) as db:
            if args.cmd == "revoke-sessions":
                n = auth_service.revoke_all_sessions(db)
                print(f"Revoked {n} session(s). Every browser must sign in again.")
                return 0
            now = datetime.now(timezone.utc)
            live = 0
            for row in db.exec(select(AuthSession)).all():
                exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
                live += exp > now
        print(f"Credential source: {auth_service.credential_source()}")
        print(f"Active sessions:   {live}")
        print("Guard tokens:")
        _print_guard_tokens(show_secrets=False)
        return 0

    if args.cmd == "guard-tokens":
        if args.action == "rotate":
            if not args.scope:
                print(f"Error: rotate needs a scope ({', '.join(sorted(GUARD_SCOPES))}).", file=sys.stderr)
                return 1
            auth_service.rotate_guard_token(args.scope)
            print(f"Rotated '{args.scope}'. Update every client that uses it.")
        elif args.action == "end-legacy":
            auth_service.end_guard_legacy()
            print("Legacy shared mode ended: each token now unlocks only its own routes.")
        _print_guard_tokens(show_secrets=True)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
