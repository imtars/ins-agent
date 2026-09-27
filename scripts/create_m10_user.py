"""Provision an M10 account locally, without storing passwords in source or shell history."""

import argparse
import asyncio
from getpass import getpass
import os

from packages.persistence.auth import AuthStore, ROLES


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("role", choices=sorted(ROLES))
    args = parser.parse_args()
    database = os.environ["M10_DATABASE_URL"]
    secret = os.environ["M10_JWT_SECRET"]
    store = AuthStore(database, secret)
    await store.setup()
    password = getpass("Password: ")
    if password != getpass("Repeat password: "):
        raise ValueError("passwords differ")
    await store.create_user(args.username, password, args.role)
    print(f"created {args.username} ({args.role})")


if __name__ == "__main__":
    asyncio.run(main())
