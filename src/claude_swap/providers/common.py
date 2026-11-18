"""Shared quota records, private storage and subprocess settings."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_swap.fsutil import atomic_write_text, read_text_with_retry

KST = timezone(timedelta(hours=9))
WINDOWS_NO_WINDOW = 0x08000000


class ProviderError(Exception):
    """An operator-facing message that contains no raw upstream output."""


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    remaining_pct: float | None = None
    resets_at: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class ProviderAccountQuota:
    provider: str
    account: str
    number: str | None = None
    active: bool = False
    switchable: bool = False
    plan: str | None = None
    windows: tuple[QuotaWindow, ...] = ()
    status: str = "ok"
    error: str | None = None


def home() -> Path:
    return Path.home() / ".cswap"


def object_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def objects(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def read_json(path: Path) -> dict | None:
    try:
        raw = json.loads(read_text_with_retry(path))
    except FileNotFoundError:
        return None
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")
    return raw


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, json.dumps(value, ensure_ascii=True) + "\n", follow_symlinks=False)


def jwt_claims(value: object) -> dict:
    if not isinstance(value, str):
        return {}
    try:
        part = value.split(".")[1]
        return object_dict(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))))
    except (IndexError, ValueError, binascii.Error):
        return {}


def display_text(value: object, *secrets: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for secret in secrets:
        if secret:
            value = value.replace(secret, "<key>")
    value = re.sub(r"(?i)\b(?:bearer\s+\S+|(?:sk-|key-|eyJ|ya29\.)[\w.-]{12,})", "<key>", value)
    return "".join(c for c in value if c.isprintable()).strip()[:100] or None


def email_label(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", value):
        return value.lower() if len(value) <= 120 else None
    return None


def timestamp(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        else:
            epoch = float(value)
            moment = datetime.fromtimestamp(epoch / 1000 if epoch >= 1e11 else epoch, timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def window(name: str, value: object, reset: object = None, *, offset: float = 0,
           scale: float = 1, disabled: bool = False) -> QuotaWindow:
    remaining = None
    if value is not None and not isinstance(value, bool):
        try:
            pct = offset + scale * float(value)
            if math.isfinite(pct):
                remaining = min(100.0, max(0.0, pct))
        except (OverflowError, TypeError, ValueError):
            pass
    return QuotaWindow(name, remaining, timestamp(reset), disabled)


def reset_clock(value: str | None, now: datetime) -> str:
    if not value:
        return "-"
    moment = datetime.fromisoformat(value).astimezone(KST)
    return moment.strftime("%H:%M" if moment.date() == now.astimezone(KST).date() else "%m-%d %H:%M")


def quota_cells(item: QuotaWindow, now: datetime) -> tuple[str, str]:
    if item.disabled:
        remaining = "disabled"
    elif item.remaining_pct is None:
        remaining = "unavailable"
    else:
        blocks = round(item.remaining_pct / 10)
        remaining = f"{'█' * blocks}{'░' * (10 - blocks)} {item.remaining_pct:5.1f}%"
    return remaining, reset_clock(item.resets_at, now)


def process_options() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", WINDOWS_NO_WINDOW)}
    return {}


def child_environment() -> dict[str, str]:
    # These are enough for native launchers and terminal I/O. Provider URLs,
    # injected keys, and config/profile overrides stay out of scoped runs.
    names = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
        "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
        "COLORTERM", "TERM_PROGRAM", "NO_COLOR", "TZ",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in names}
