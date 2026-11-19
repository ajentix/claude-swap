"""Read coding-plan quotas over fixed HTTPS usage endpoints."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .coding_credentials import local_coding_credential
from .common import ProviderAccountQuota, ProviderError, display_text, object_dict, objects, window

CODING_ENDPOINTS = {
    "kimi-coding": ("https://api.kimi.com/coding/v1/usages", ("KIMI_CODING_API_KEY", "KIMI_CODE_API_KEY")),
    "zai-glm": ("https://api.z.ai/api/monitor/usage/quota/limit", ("ZAI_API_KEY", "ZAI_CODING_PLAN_API_KEY")),
    "opencode-go": ("https://opencode.ai/zen/go/v1/usage", ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY")),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_usage(url: str, headers: dict[str, str], *, timeout: float = 10) -> dict:
    return _request_json(url, headers, timeout=timeout)


def post_reset(url: str, headers: dict[str, str], payload: dict) -> dict:
    return _request_json(url, {**headers, "Content-Type": "application/json"}, json.dumps(payload).encode())


def _request_json(url: str, headers: dict[str, str], body: bytes | None = None, *, timeout: float = 10) -> dict:
    try:
        request = Request(url, data=body, headers={"Accept": "application/json", "User-Agent": "claude-swap", **headers})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ProviderError("Usage response exceeded the size limit")
        data = json.loads(raw)
    except HTTPError as exc:
        code = exc.code
        messages = {401: "authentication required", 403: "access denied or subscription unavailable", 429: "rate limited"}
        message = messages.get(code, "usage request failed")
        with exc:
            if code == 403 and url != CODING_ENDPOINTS["opencode-go"][0]:
                try:  # a Cloudflare challenge page is an edge block, not a lost subscription
                    head = exc.read(2048).decode("utf-8", "replace").lower()
                    if "<html" in head or "cloudflare" in head or "cf-chl" in head:
                        message = "blocked by the provider's edge (Cloudflare); try again later"
                except (OSError, ValueError):
                    pass
            if code == 403 and url == CODING_ENDPOINTS["opencode-go"][0]:
                try:
                    body = object_dict(json.loads(exc.read(4096)))
                    if object_dict(body.get("error")).get("type") == "EntitlementError":
                        message = "OpenCode Go subscription required"
                except (OSError, ValueError):
                    pass
        raise ProviderError(f"HTTP {code}: {message}") from None
    except (URLError, OSError, ValueError) as exc:
        message = "Invalid usage response" if isinstance(exc, ValueError) else "Usage endpoint unavailable"
        raise ProviderError(message) from None
    if not isinstance(data, dict):
        raise ProviderError("Invalid usage response")
    return data


ZAI_SUBSCRIPTIONS_URL = "https://api.z.ai/api/biz/subscription/list"


def zai_plan_name(headers: dict[str, str]) -> str | None:
    """The plan's product name ("GLM Coding Pro"); the quota endpoint only has a level."""
    try:
        data = get_usage(ZAI_SUBSCRIPTIONS_URL, headers)
    except ProviderError:
        return None
    names = [display_text(item.get("productName"), headers.get("Authorization")) for item in objects(data.get("data"))
             if str(item.get("valid", "true")).lower() not in ("false", "0")]
    return next((n for n in names if n), None)


def kimi_usage(data: dict, key: str) -> tuple:
    data = object_dict(data.get("data")) or data
    pools = object_dict(data.get("usages"))
    windows = []
    for label, names in (
        ("5h", ("limit_5h", "5h")), ("7d", ("limit_7d", "7d")),
        ("mo", ("limit_month_total", "monthly", "mo")), ("Code mo", ("limit_month_code",)),
    ):
        entry = next((object_dict(pools[name]) for name in names if name in pools), {})
        if entry:
            windows.append(window(label, entry.get("used_ratio"), entry.get("reset_time"), offset=100, scale=-100))
    membership = object_dict(object_dict(data.get("user")).get("membership"))
    return tuple(windows), display_text(membership.get("level"), key)


def zai_usage(data: dict, key: str) -> tuple:
    quota = object_dict(data.get("data"))
    units = {3: "5h", 5: "mo", 6: "7d"}
    windows = []
    for entry in objects(quota.get("limits")):
        unit = entry.get("unit")
        if not isinstance(unit, int) or unit not in units:
            continue
        count = entry.get("number")
        label = f"{count}h" if unit == 3 and type(count) is int and count > 0 else units[unit]
        if entry.get("type") == "TIME_LIMIT":
            label = f"Tools {label}"
        windows.append(window(label, entry.get("percentage"), entry.get("nextResetTime"), offset=100, scale=-1))
    return tuple(windows), display_text(quota.get("level"), key)


def opencode_usage(data: dict, key: str, session: str) -> tuple:
    pools = object_dict(data.get("usage"))
    windows = tuple(
        window(label, 100 if entry.get("status") == "rate-limited" else entry.get("percent"),
               entry.get("resetsAt"), offset=100, scale=-1)
        for name, label in (("rolling", "5h"), ("weekly", "7d"), ("monthly", "mo"))
        if (entry := object_dict(pools.get(name)))
    )
    return windows, display_text(data.get("plan"), key, session)


def _credential(settings: dict, field: str) -> str:
    env = settings.get(f"{field}_env")
    value = os.environ.get(env) if isinstance(env, str) else settings.get(field)
    return value.strip() if isinstance(value, str) else ""


def coding_plan_jobs(root: Path, provider: str) -> list[Callable[[], ProviderAccountQuota]]:
    url, env_names = CODING_ENDPOINTS[provider]
    try:
        with (root / "providers.toml").open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError):
        raise ProviderError("Cannot read providers.toml") from None
    section = object_dict(config.get(provider))
    credentials = {label: value for label, value in section.items() if isinstance(value, dict)}
    if any(name in section for name in ("api_key", "api_key_env", "session", "session_env")):
        credentials["default"] = section
    if not credentials:
        env = next((name for name in env_names if os.environ.get(name, "").strip()), None)
        if env:
            credentials["default"] = {"api_key_env": env}
        else:
            key, label, local_url = local_coding_credential(provider, Path.home())
            if key:
                credentials[label] = {"api_key": key}
                url = local_url or url
    if not credentials:
        return [lambda: ProviderAccountQuota(
            provider, "-", status="unconfigured", error=f"Set {env_names[0]} or configure a local login",
        )]
    return [
        lambda label=label, settings=settings: _coding_plan_row(provider, label, url, settings)
        for label, settings in sorted(credentials.items())
    ]


def _coding_plan_row(provider: str, label: str, url: str, settings: dict) -> ProviderAccountQuota:
    key = _credential(settings, "api_key")
    session = _credential(settings, "session")
    if provider == "opencode-go":
        session = session or os.environ.get("OPENCODE_GO_SESSION", "").strip()
    row = ProviderAccountQuota(provider, display_text(label, key, session) or "account")
    if not key:
        return replace(row, status="error", error="API key missing")
    headers = {"Authorization": key if provider == "zai-glm" else f"Bearer {key}"}
    if provider == "opencode-go" and session:
        headers["x-opencode-session"] = session
    try:
        data = get_usage(url, headers)
        if provider == "kimi-coding":
            windows, plan = kimi_usage(data, key)
        elif provider == "zai-glm":
            windows, plan = zai_usage(data, key)
            plan = zai_plan_name(headers) or plan
        else:
            windows, plan = opencode_usage(data, key, session)
        if not any(item.remaining_pct is not None for item in windows):
            raise ProviderError("Usage API returned no quota windows")
        return replace(row, windows=windows, plan=plan)
    except ProviderError as exc:
        return replace(row, status="error", error=str(exc))
