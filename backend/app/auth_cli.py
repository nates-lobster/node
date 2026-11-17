"""
Server-only CLI for managing Node Lock Screen credentials over SSH (`max@athlon`).
Credentials can NEVER be modified from any browser/client API.

Usage inside container or host:
  docker exec -it hub-backend python -m app.auth_cli set <username>
  docker exec -it hub-backend python -m app.auth_cli hash
"""

import argparse
import getpass
import sys

from .auth import auth_service, hash_password_pbkdf2


def main() -> int:
    parser = argparse.ArgumentParser(description="Node Server-Only Credential Manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="Set username & PBKDF2-SHA256 hashed password in /data/auth_credentials.json")
    p_set.add_argument("username", help="Login username")
    p_set.add_argument("--password", help="Optional password (omit to prompt securely via getpass)")

    p_hash = sub.add_parser("hash", help="Generate a PBKDF2-SHA256 hash string for .env AUTH_PASSWORD_HASH")
    p_hash.add_argument("--password", help="Optional password (omit to prompt securely)")

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
