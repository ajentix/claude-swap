"""CLI entry points for provider quota and account operations."""

from __future__ import annotations

import argparse
import json
import math
import time
import sys
from datetime import datetime

from claude_swap.exceptions import LockError

from .common import KST, ProviderAccountQuota, ProviderError, home, quota_cells, quota_status, reset_credit_cells
from .quota import PROVIDERS, SWITCHERS, collect_quota, invalidate_quota, quota_payload
from .reset import OUTCOMES, RESET_PROVIDERS, prepare_reset, redeem_reset, reset_options


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
        status = quota_status(row, now)
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
                "Reset credits", remaining, expiry, note if row.windows else " / ".join(filter(None, (status, note))),
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
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("account", help="Account number, email or name from cswap quota")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--retry", action="store_true", help="Retry the same pending reset request")
    mode.add_argument("--list", action="store_true", help="List credits, their scope, and pending requests")
    mode.add_argument("--credit", type=int, help="Use the numbered credit from --list")
    parser.add_argument("--yes", action="store_true", help="Confirm the reset without a prompt")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.credit is not None and args.credit < 1:
        parser.error("--credit must be positive")
    if args.provider not in RESET_PROVIDERS:
        message = "This provider has scheduled quota renewal. Reset credit redemption is unavailable in cswap"
        print(json.dumps({"provider": args.provider, "status": "unsupported", "message": message})
              if args.json else message)
        return 1
    if not args.list and not args.yes and not sys.stdin.isatty():
        parser.error("Use --yes to explicitly confirm in non-interactive sessions")
    try:
        if args.list:
            options = reset_options(args.provider, args.account)
            from dataclasses import asdict

            credits = [{"number": i, "label": offer.label, "windows": offer.windows,
                        "expires_at": offer.expires_at, "remaining": offer.remaining,
                        "usable": offer.usable}
                       for i, offer in enumerate(options.offers, 1)]
            if args.json:
                print(json.dumps({"provider": args.provider, "account": options.target.label,
                                  "inventory": asdict(options.credits) if options.credits else None,
                                  "pending": options.pending_state,
                                  "can_retry": bool(options.pending and options.pending_state == "unknown"),
                                  "credits": credits, "message": options.warning}))
            else:
                if options.pending_state:
                    print(f"Pending reset ({options.pending_state}). {options.warning or 'Use --retry when ready.'}")
                elif options.credits is None:
                    print("Reset credit availability was not reported")
                else:
                    print(f"{options.credits.available_count} reset credit(s) held")
                for item in credits:
                    windows = ", ".join(item["windows"]) or "Unspecified"
                    print(f'{item["number"]}  {windows}  {item["expires_at"] or "-"}  '
                          f'{item["remaining"]} left  {"usable" if item["usable"] else "held"}')
            return 0
        intent = prepare_reset(args.provider, args.account, retry=args.retry, credit=args.credit)
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
    if provider == "codex":
        policy = commands.add_parser("policy", help="Show or edit Codex auto-switch policy")
        policy.add_argument("account", nargs="?")
        policy.add_argument("--priority", type=int, help="Higher values take precedence")
        hold = policy.add_mutually_exclusive_group()
        hold.add_argument("--hold", action="store_const", const=True, dest="held")
        hold.add_argument("--release", action="store_const", const=False, dest="held")
        policy.add_argument("--json", action="store_true")
        auto = commands.add_parser("auto", help="Switch to another saved login before a limit")
        auto.add_argument("--threshold", type=float, default=90, help="5-hour line, %% used (default 90)")
        auto.add_argument("--weekly-threshold", type=float, help="weekly line (default: --threshold)")
        auto.add_argument("--interval", type=float, default=60, help="seconds between checks (min 60)")
        auto.add_argument("--once", action="store_true", help="one check; exit 0 switched, 1 error, 2 nothing, 3 blocked")
        auto.add_argument("--dry-run", action="store_true")
        auto.add_argument("--json", action="store_true")
    if "--" in argv:
        split = argv.index("--")
        tail, argv = argv[split + 1:], argv[:split]
    else:
        tail = []
    args = parser.parse_args(argv)
    if tail and args.command != "run":
        parser.error("Only run takes arguments after --")
    if args.command == "auto":
        for threshold in (args.threshold, args.weekly_threshold):
            if threshold is not None and (not math.isfinite(threshold) or not 0 <= threshold <= 100):
                parser.error("Thresholds must be between 0 and 100 percent")
        if not math.isfinite(args.interval) or args.interval <= 0:
            parser.error("Interval must be a finite positive number")
        return _codex_auto(args)
    store = SWITCHERS[provider](home())
    try:
        if args.command == "policy":
            if args.account is None and (args.priority is not None or args.held is not None):
                parser.error("Select an account when editing policy")
            return _codex_policy(args, store)
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


def _codex_auto(args) -> int:
    from dataclasses import asdict

    from .codex_auto import Decision, tick

    while True:
        try:
            decision = tick(threshold=args.threshold, weekly_threshold=args.weekly_threshold, dry_run=args.dry_run)
        except (ProviderError, LockError, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ProviderError) else "Could not access the local login store"
            decision = Decision("error", message)
        if args.json:
            data = {"provider": "codex", "kind": decision.kind, "reason": decision.reason,
                    "from": decision.source, "to": decision.target}
            if args.dry_run:
                data["accounts"] = [asdict(row) for row in decision.assessments]
            print(json.dumps(data), flush=True)
        else:
            stamp = time.strftime("%H:%M:%S")
            arrow = f" -> {decision.target}" if decision.kind == "switch" else ""
            print(f"{stamp}  {decision.kind}{arrow}: {decision.reason}", flush=True)
            if args.dry_run:
                for row in decision.assessments:
                    room = "-" if row.room is None else f"{row.room:.1f}"
                    print(f"  {row.account}: {row.result} / priority {row.priority} / {room} points before line")
        if args.once:
            return decision.exit_code
        time.sleep(max(60.0, args.interval))


def _codex_policy(args, store) -> int:
    from .codex_policy import Policy, load_policies, set_policy

    root = store.directory.parent
    policies = load_policies(root)
    if args.account is None:
        accounts, _ = store.roster()
    else:
        account, _ = store._resolve(args.account)
        accounts = [account]
        if args.priority is not None or args.held is not None:
            identity = account.login.identity
            policies[identity] = set_policy(root, identity, priority=args.priority, held=args.held)
    rows = [{"account": account.label, "number": account.number, "active": account.active,
             **vars(policies.get(account.login.identity, Policy()))} for account in accounts]
    if args.json:
        print(json.dumps({"provider": "codex", "accounts": rows}))
    for row in rows if not args.json else ():
        print(f"{row['number']}  {row['account']}  priority {row['priority']}  "
              f"{'held' if row['held'] else 'eligible'}")
    return 0
