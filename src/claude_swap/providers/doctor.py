"""Account and subscription diagnostics without exposing credentials."""

from __future__ import annotations

import shutil
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from claude_swap import oauth

from .api import CODING_ENDPOINTS, _credential, coding_plan_config
from .common import ProviderError, display_text, home, jwt_claims, object_dict, timestamp
from .quota import PROVIDERS, SWITCHERS, collect_quota


@dataclass(frozen=True)
class Check:
    provider: str
    account: str
    source: str
    status: str
    number: str | None = None
    active: bool = False
    expires_at: str | None = None
    quota_status: str | None = None
    action: str | None = None
    detail: str | None = None


_SIGN_IN = {
    "claude": "claude /login, then cswap add",
    "agy": "agy, then cswap agy add",
    "codex": "codex login, then cswap codex add",
    **{name: f"Configure {settings[1][0]} or sign in with the provider"
       for name, settings in CODING_ENDPOINTS.items()},
}


def _local_status(expires: str | None) -> str:
    if expires and datetime.fromisoformat(expires).timestamp() <= time.time():
        return "expired"
    return "configured"


def _claude_checks() -> list[Check]:
    from claude_swap.switcher import ClaudeAccountSwitcher

    rows = []
    for number, email, _, _, active, credentials, _ in ClaudeAccountSwitcher()._build_accounts_info():
        blob = oauth.extract_oauth_data(credentials) or {}
        expires = timestamp(blob.get("expiresAt"))
        token = blob.get("accessToken")
        token = token if isinstance(token, str) else None
        refresh = blob.get("refreshToken")
        status = _local_status(expires) if token else "unavailable"
        if oauth.is_setup_token(blob):
            status = "quota-unavailable"
        label = display_text(email, credentials, token, refresh if isinstance(refresh, str) else None) or "account"
        rows.append(Check("claude", label, "Claude Code login" if active else "cswap saved login",
                          status, str(number), active, expires,
                          action=_SIGN_IN["claude"] if status != "configured" else None))
    return rows


def _native_checks(provider: str, root: Path) -> list[Check]:
    store = SWITCHERS[provider](root)
    accounts, error = store.roster()
    rows = []
    for account in accounts:
        data = account.login.data
        if provider == "codex":
            access = object_dict(data.get("tokens")).get("access_token")
            expires = timestamp(jwt_claims(access).get("exp"))
            status = _local_status(expires) if access else "quota-unavailable"
            active_source = "Codex auth.json"
        else:
            expires = timestamp(object_dict(data.get("token")).get("expiry"))
            status = _local_status(expires)
            active_source = "Antigravity native login"
        legacy = account.path and account.path.parent != store.directory
        source = active_source if account.active else "Legacy stash" if legacy else "cswap saved login"
        rows.append(Check(provider, account.label, source, status, account.number,
                          account.active, expires, action=_SIGN_IN[provider] if status != "configured" else None))
    if error:
        rows.append(Check(provider, "-", "local login store", "error", action=_SIGN_IN[provider], detail=error))
    if not shutil.which(provider):
        rows.append(Check(provider, "-", "PATH", "cli-missing", action=f"Install the {provider} CLI"))
    return rows


def _coding_checks(provider: str, root: Path) -> list[Check]:
    _, settings, source = coding_plan_config(root, provider)
    rows = []
    for label, fields in sorted(settings.items()):
        key = _credential(fields, "api_key")
        session = _credential(fields, "session")
        rows.append(Check(provider, display_text(label, key, session) or "account", source or "-",
                          "configured" if key else "missing-key",
                          action=None if key else _SIGN_IN[provider]))
    return rows


def diagnose(providers=PROVIDERS, *, live: bool = False, root: Path | None = None) -> list[Check]:
    root = home() if root is None else root
    providers = tuple(dict.fromkeys(providers))
    results = []
    for provider in providers:
        try:
            if provider == "claude":
                rows = _claude_checks()
            elif provider in SWITCHERS:
                rows = _native_checks(provider, root)
            else:
                rows = _coding_checks(provider, root)
        except (ProviderError, OSError, ValueError):
            rows = [Check(provider, "-", "local login store", "error", action=_SIGN_IN[provider])]
        results.extend(rows or [Check(provider, "-", "-", "unconfigured", action=_SIGN_IN[provider])])
    if not live:
        return results
    readings = collect_quota(providers, refresh=True, refresh_login=False, root=root)
    by_account = {(r.provider, r.number or r.account): r for r in readings}
    checked = []
    for row in results:
        reading = by_account.get((row.provider, row.number or row.account))
        if reading is None or row.status in {"cli-missing", "error"}:
            checked.append(row)
            continue
        action = row.action
        if reading.http_status in {401, 403}:
            action = _SIGN_IN[row.provider] if reading.http_status == 401 else "Verify subscription access for this login"
        checked.append(replace(row, quota_status=reading.status, detail=reading.error, action=action))
    return checked


def doctor_command(argv: list[str], prog: str) -> int:
    import argparse
    import json

    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    parser = argparse.ArgumentParser(prog=f"{prog} doctor")
    parser.add_argument("--provider", action="append", choices=PROVIDERS)
    parser.add_argument("--live", action="store_true", help="Check subscription access using usage endpoints")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = diagnose(args.provider or PROVIDERS, live=args.live)
    if args.json:
        print(json.dumps({"schemaVersion": 1, "accounts": [asdict(row) for row in rows]}))
    else:
        table = Table(box=None)
        for column in ("Provider", "Account", "Source", "Login", "Expires (UTC)", "Quota", "Action"):
            table.add_column(column, overflow="fold")
        for row in rows:
            account = " ".join(filter(None, ("*" if row.active else None, row.number, row.account)))
            table.add_row(*map(Text, (row.provider, account, row.source, row.status, row.expires_at or "-",
                                      row.detail or row.quota_status or "-", row.action or "-")))
        Console().print(table)
    return 0
