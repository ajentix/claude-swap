"""Project Claude's roster and usage store into the provider quota view."""

from __future__ import annotations

from claude_swap.exceptions import LockError

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from claude_swap import oauth, subscription
from claude_swap.switcher import ClaudeAccountSwitcher

from .api import get_usage
from .common import ProviderAccountQuota, ProviderError, ResetCredits, display_text, object_dict, objects, window
from .reset_credits import CLAUDE_USAGE_URL, claude_reset_inventory


def claude_headers(credentials: str, *, cli_identity: bool = False) -> dict[str, str]:
    """Usage-request headers. ``cli_identity`` sends the Claude Code CLI's
    User-Agent, which the reset-credit inventory requires (see claude_resets)."""
    blob = oauth.extract_oauth_data(credentials) or {}
    token = blob.get("accessToken")
    if not isinstance(token, str) or not token or oauth.is_setup_token(blob):
        raise ProviderError("Claude subscription OAuth login required")
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": oauth.OAUTH_BETA_HEADER}
    if cli_identity:
        from .claude_resets import user_agent

        headers["User-Agent"] = user_agent()
    return headers


def _with_inventory(row: ProviderAccountQuota, email: str, credentials: str, refresh: bool) -> ProviderAccountQuota:
    """The reset credits the CLI identity sees, in place of the 'not offered'
    cswap's own usage read gets; read anew on --refresh once the last is old."""
    from . import claude_resets

    if refresh and claude_resets.due(email):
        try:
            claude_resets.refresh(email, credentials)
        except (ProviderError, OSError, ValueError, LockError):
            pass
    credits = claude_resets.cached(email)
    return replace(row, reset_credits=credits) if credits is not None else row


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


def _claude_row(row: ProviderAccountQuota, credentials: str, previous: dict,
                previous_at: float | None = None) -> ProviderAccountQuota:
    blob = oauth.extract_oauth_data(credentials) or {}
    token = blob.get("accessToken")
    secrets = tuple(s for s in (token, blob.get("refreshToken")) if isinstance(s, str) and s)
    if not isinstance(token, str) or not token or oauth.is_setup_token(blob):
        return replace(row, status="unavailable", error="Claude subscription OAuth login required")
    try:
        raw = get_usage(CLAUDE_USAGE_URL, claude_headers(credentials))
        credits, _ = claude_reset_inventory(raw)
        windows = _windows(raw, stored=False, secrets=secrets)
        if credits is None and not any(item.remaining_pct is not None for item in windows):
            raise ProviderError("Claude returned no quota windows")
        return replace(row, windows=windows, reset_credits=credits)
    except ProviderError as exc:
        stale = ()
        if previous.get("source") != oauth.USAGE_SOURCE_HEADERS:
            stale = _windows(previous, stored=True, secrets=secrets)
        return replace(row, windows=stale, status="stale" if stale else "error", error=str(exc),
                       fetched_at=previous_at if stale else None, retry_at=exc.retry_after, http_status=exc.status_code)


def _usage_store_row(row: ProviderAccountQuota, creds: str, entry, now: float, *,
                     refresh: bool = False) -> ProviderAccountQuota:
    previous = entry.last_good or {}
    previous_at = entry.fetched_at
    usable = previous.get("source") != oauth.USAGE_SOURCE_HEADERS
    waiting = entry.backoff_until is not None and entry.backoff_until > now
    pending = entry.claim_until is not None and entry.claim_until > now
    # Read a native snapshot before sending a second request for the same account.
    # Header probes remain native-only. Their backoff still applies here.
    native_fresh = (usable and previous_at is not None
                    and 0 <= now - previous_at < 60 and not refresh and not entry.last_error)
    if waiting or pending or native_fresh:
        blob = oauth.extract_oauth_data(creds) or {}
        secrets = tuple(s for s in (blob.get("accessToken"), blob.get("refreshToken")) if isinstance(s, str) and s)
        windows = _windows(previous, stored=True, secrets=secrets) if usable else ()
        credit_data = previous.get("reset_credits")
        credits = ResetCredits(**credit_data) if isinstance(credit_data, dict) else None
        if credits and credits.next_expiry and datetime.fromisoformat(credits.next_expiry).timestamp() <= now:
            credits = None
        status = "ok" if native_fresh else "stale" if windows else "backoff" if waiting else "pending"
        error = ("Claude usage is in backoff" if waiting else "Claude usage refresh is in progress" if pending else None)
        return replace(row, windows=windows, reset_credits=credits, status=status, error=error,
                       fetched_at=previous_at if windows else None, retry_at=entry.backoff_until if waiting else None)
    return _claude_row(row, creds, previous, previous_at)


def claude_jobs(switcher: ClaudeAccountSwitcher, *, refresh: bool = False) -> list[Callable[[], ProviderAccountQuota]]:
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
        entry = entries[number]
        jobs.append(lambda row=row, creds=credentials, entry=entry, email=email: _with_inventory(
            _usage_store_row(row, creds, entry, switcher._usage_store.clock(), refresh=refresh), email, creds, refresh))
    return jobs
