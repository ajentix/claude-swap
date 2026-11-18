"""CLI entry points for provider quota and account operations."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from claude_swap.exceptions import LockError

from .common import KST, ProviderAccountQuota, ProviderError, home, quota_cells, reset_credit_cells
from .quota import PROVIDERS, SWITCHERS, collect_quota, invalidate_quota, quota_payload
from .reset import OUTCOMES, RESET_PROVIDERS, prepare_reset, redeem_reset


def _print_rows(rows: list[ProviderAccountQuota], json_output: bool) -> None:
    if json_output:
        print(json.dumps(quota_payload(rows), ensure_ascii=True))
        return
    if not rows:
        print("No provider accounts configured.")
        return
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    table = Table(box=None, padding=(0, 1))
    for name in ("Provider", "Account", "Plan", "Window", "Remaining", "Reset (KST)", "Status"):
        table.add_column(name, overflow="fold", no_wrap=name in ("Provider", "Remaining", "Reset (KST)"))
    now = datetime.now(KST)
    for row in rows:
        label = " ".join(filter(None, ("*" if row.active else None, row.number, row.account)))
        status = f"{row.status}: {row.error}" if row.error else row.status
        for index, item in enumerate(row.windows or (() if row.reset_credits else (None,))):
            remaining, reset = quota_cells(item, now) if item else ("-", "-")
            table.add_row(*map(Text, (
                row.provider if index == 0 else "", label if index == 0 else "",
                (row.plan or "-") if index == 0 else "", item.name if item else "-",
                remaining, reset, status if index == 0 else "",
            )))
        if row.reset_credits:
            remaining, expiry, note = reset_credit_cells(row.reset_credits, now)
            table.add_row(*map(Text, (
                "" if row.windows else row.provider, "" if row.windows else label, "",
                "Reset credits", remaining, expiry, note,
            )))
    Console().print(table)


def quota_command(argv: list[str], prog: str) -> None:
    parser = argparse.ArgumentParser(prog=f"{prog} quota", description="Show remaining subscription quotas")
    parser.add_argument("--provider", action="append", choices=PROVIDERS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    _print_rows(collect_quota(args.provider or PROVIDERS, refresh=args.refresh), args.json)


def reset_command(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=f"{prog} reset", description="Use one subscription reset credit")
    parser.add_argument("provider", choices=RESET_PROVIDERS)
    parser.add_argument("account", help="Account number, email or name from cswap quota")
    parser.add_argument("--retry", action="store_true", help="Retry the same pending reset request")
    parser.add_argument("--yes", action="store_true", help="Confirm the reset without a prompt")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.yes and not sys.stdin.isatty():
        parser.error("Use --yes to explicitly confirm in non-interactive sessions")
    try:
        intent = prepare_reset(args.provider, args.account, retry=args.retry)
        if not args.yes:
            print(f"{intent.prompt()} [y/N] ", end="", file=sys.stderr, flush=True)
            if input().strip().casefold() not in ("y", "yes"):
                print(json.dumps({"status": "cancelled"}) if args.json else "Reset cancelled")
                return 1
        outcome = redeem_reset(intent)
        message = OUTCOMES[outcome]
    except (EOFError, KeyboardInterrupt):
        outcome, message = "interrupted", "Interrupted. Check quota before trying again"
    except (ProviderError, LockError, OSError, ValueError) as exc:
        outcome = "error"
        message = str(exc) if isinstance(exc, ProviderError) else "Could not access the local reset state"
    if args.json:
        print(json.dumps({"provider": args.provider, "status": outcome, "message": message}))
    else:
        print(message, file=sys.stdout if outcome == "reset" else sys.stderr)
    return 0 if outcome in ("reset", "already_used", "already_redeemed") else 1


def provider_command(provider: str, argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=f"{prog} {provider}")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Back up the active login")
    if provider == "codex":
        add.add_argument("name", nargs="?")
    listing = commands.add_parser("list", help="Show accounts and quotas")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--refresh", action="store_true")
    switch = commands.add_parser("switch", help="Install a saved login")
    switch.add_argument("account")
    switch.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="Launch the CLI in a temporary account profile")
    run.add_argument("account")
    if "--" in argv:
        split = argv.index("--")
        tail, argv = argv[split + 1:], argv[:split]
    else:
        tail = []
    args = parser.parse_args(argv)
    if tail and args.command != "run":
        parser.error("Only run takes arguments after --")
    store = SWITCHERS[provider](home())
    try:
        if args.command == "list":
            _print_rows(collect_quota((provider,), refresh=args.refresh), args.json)
            return 0
        if args.command == "run":
            try:
                return store.run(args.account, tail)
            finally:
                invalidate_quota(provider)
        if args.command == "add":
            label = store.add(getattr(args, "name", None))
            print(f"Saved {provider} account {label}")
        else:
            label = store.switch(args.account)
            if args.json:
                print(json.dumps({"provider": provider, "account": label, "status": "ok"}))
            else:
                print(f"Active {provider} account {label}")
        invalidate_quota(provider)
    except (ProviderError, LockError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ProviderError) else "Could not access the local login store"
        if getattr(args, "json", False):
            print(json.dumps({"provider": provider, "status": "error", "error": message}))
        else:
            print(message, file=sys.stderr)
        return 1
    return 0
