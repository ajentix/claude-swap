"""Project Claude's roster and usage store into the provider quota view."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from claude_swap import oauth, subscription
from claude_swap.switcher import ClaudeAccountSwitcher

from .api import get_usage
from .common import ProviderAccountQuota, ProviderError, display_text, object_dict, objects, window


def _windows(data: dict, *, stored: bool, secrets: tuple[str, ...]) -> tuple:
    windows = []
    for name, key in (("5h", "five_hour"), ("7d", "seven_day")):
        entry = object_dict(data.get(key))
        if entry:
            windows.append(window(name, entry.get("pct" if stored else "utilization"), entry.get("resets_at"), offset=100, scale=-1))
    for entry in objects(data.get("scoped" if stored else "limits")):
        model = object_dict(object_dict(entry.get("scope")).get("model"))
        name = display_text(entry.get("name") if stored else model.get("display_name"), *secrets)
        if name:
            windows.append(window(f"{name} 7d", entry.get("pct" if stored else "percent"), entry.get("resets_at"), offset=100, scale=-1))
    return tuple(windows)


def _claude_row(row: ProviderAccountQuota, credentials: str, previous: dict) -> ProviderAccountQuota:
    blob = oauth.extract_oauth_data(credentials) or {}
    token = blob.get("accessToken")
    secrets = tuple(s for s in (token, blob.get("refreshToken")) if isinstance(s, str) and s)
    if not isinstance(token, str) or not token or oauth.is_setup_token(blob):
        return replace(row, status="unavailable", error="Claude subscription OAuth login required")
    try:
        raw = get_usage("https://api.anthropic.com/api/oauth/usage", {
            "Authorization": f"Bearer {token}", "anthropic-beta": oauth.OAUTH_BETA_HEADER,
        })
        windows = _windows(raw, stored=False, secrets=secrets)
        if not any(item.remaining_pct is not None for item in windows):
            raise ProviderError("Claude returned no quota windows")
        return replace(row, windows=windows)
    except ProviderError as exc:
        stale = ()
        if previous.get("source") != oauth.USAGE_SOURCE_HEADERS:
            stale = _windows(previous, stored=True, secrets=secrets)
        return replace(row, windows=stale, status="stale" if stale else "error", error=str(exc))


def claude_jobs(switcher: ClaudeAccountSwitcher) -> list[Callable[[], ProviderAccountQuota]]:
    accounts = switcher._build_accounts_info()
    plans = switcher._stored_subscription_plans(accounts)
    entries = switcher._usage_store.entries({str(n): (email, org) for n, email, _, org, *_ in accounts})
    jobs = []
    for number, email, _, _, active, credentials, alias in accounts:
        blob = oauth.extract_oauth_data(credentials) or {}
        secrets = tuple(s for s in (blob.get("accessToken"), blob.get("refreshToken"), credentials) if isinstance(s, str) and s)
        number = str(number)
        label = f"{alias} ({email})" if alias else email
        plan = object_dict(object_dict(plans.get(number)).get("plan"))
        row = ProviderAccountQuota(
            "claude", display_text(label, *secrets) or "account", number, active,
            switcher._account_is_switchable(number), display_text(subscription.plan_label(plan), *secrets),
        )
        jobs.append(lambda row=row, creds=credentials, prior=entries[number].last_good or {}: _claude_row(row, creds, prior))
    return jobs
