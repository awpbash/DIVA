"""accounts.py — manage login accounts from the command line.

The admin dashboard is the normal way to do this. This exists for the cases a
browser cannot help with: the very first account, a locked-out instance, and
scripted provisioning.

    python -m scripts.accounts list
    python -m scripts.accounts add alice@example.com --name "Alice" --role admin
    python -m scripts.accounts role bob@example.com confidential
    python -m scripts.accounts verifier bob@example.com --on
    python -m scripts.accounts remove bob@example.com

Roles: admin (everything), confidential (chat, explore, knowledge), default (chat).
The verifier flag opens the Review tab regardless of role.
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from api import appdb


def _print_users() -> int:
    users = appdb.list_users()
    if not users:
        print("no accounts")
        return 1
    width = max(len(u["email"]) for u in users)
    for u in sorted(users, key=lambda u: (u["role"], u["email"])):
        flag = " [verifier]" if u.get("verifier") else ""
        print(f"  {u['email']:<{width}}  {u['role']:<12} {u.get('name') or ''}{flag}")
    print(f"\n{len(users)} account(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="python -m scripts.accounts",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show every account")

    p_add = sub.add_parser("add", help="create or update an account")
    p_add.add_argument("email")
    p_add.add_argument("--name", default="")
    p_add.add_argument("--title", default="")
    p_add.add_argument("--role", default="default",
                       choices=["admin", "confidential", "default"])
    p_add.add_argument("--zone", default="")

    p_role = sub.add_parser("role", help="change an account's role")
    p_role.add_argument("email")
    p_role.add_argument("role", choices=["admin", "confidential", "default"])

    p_ver = sub.add_parser("verifier", help="grant or revoke Review access")
    p_ver.add_argument("email")
    grp = p_ver.add_mutually_exclusive_group(required=True)
    grp.add_argument("--on", action="store_true")
    grp.add_argument("--off", action="store_true")

    p_rm = sub.add_parser("remove", help="delete an account")
    p_rm.add_argument("email")

    args = ap.parse_args(argv)
    appdb.init()

    if args.cmd == "list":
        return _print_users()

    if args.cmd == "add":
        if "@" not in args.email:
            print(f"not an email address: {args.email}", file=sys.stderr)
            return 2
        appdb.upsert_user(args.email, args.name, args.title, args.role, args.zone)
        print(f"{args.email} is now {args.role}")
        return 0

    if args.cmd == "role":
        if not appdb.set_role(args.email, args.role):
            print(f"no account for {args.email}", file=sys.stderr)
            return 1
        print(f"{args.email} is now {args.role}")
        return 0

    if args.cmd == "verifier":
        if not appdb.update_account(args.email, verifier=args.on):
            print(f"no account for {args.email}", file=sys.stderr)
            return 1
        print(f"{args.email} verifier: {'on' if args.on else 'off'}")
        return 0

    if args.cmd == "remove":
        if not appdb.delete_user(args.email):
            print(f"no account for {args.email}", file=sys.stderr)
            return 1
        print(f"removed {args.email}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
