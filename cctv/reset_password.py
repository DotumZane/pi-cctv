"""Reset a local Pi CCTV dashboard user's password."""

from __future__ import annotations

import argparse
import getpass

from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset a Pi CCTV dashboard user's password")
    parser.add_argument("--db", default="data/cctv.db", help="SQLite database path (default: data/cctv.db)")
    parser.add_argument("--username", required=True, help="Existing dashboard username")
    args = parser.parse_args()

    password = getpass.getpass("New password (8+ characters): ")
    confirmation = getpass.getpass("Confirm new password: ")
    if password != confirmation:
        parser.error("passwords do not match")
    if len(password) < 8:
        parser.error("password must be at least 8 characters")

    store = Store(args.db)
    try:
        store.set_password(args.username, password)
    except ValueError as error:
        parser.error(str(error))
    finally:
        store.close()
    print(f"Password reset for {args.username}. Existing sessions were signed out.")


if __name__ == "__main__":
    main()
