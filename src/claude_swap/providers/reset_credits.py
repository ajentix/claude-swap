"""Decode subscription reset inventory and keep redemption identifiers internal."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .common import ResetCredits, object_dict, objects, timestamp

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
CODEX_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"


@dataclass(frozen=True)
class ResetOffer:
    credit_id: str = field(repr=False)
    expires_at: str | None = None
    remaining: int = 1


def count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _bound(data: dict, key: str) -> str | None:
    raw = data.get(key)
    bound = timestamp(raw)
    if raw is not None and bound is None:
        raise ValueError("invalid time bound")
    return bound


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def claude_reset_inventory(data: dict, *, now: str | None = None) -> tuple[ResetCredits | None, ResetOffer | None]:
    block = object_dict(data.get("cedar_ember"))
    eligible = block.get("eligible")
    if eligible is False:
        return ResetCredits(0, redeemable_count=0), None
    if eligible is not True or not isinstance(block.get("grants"), list):
        return None, None
    now = now or _utc_now()
    try:
        cooldown = _bound(block, "cooldown_until")
    except ValueError:
        return None, None
    cooldown = cooldown if cooldown and cooldown > now else None
    held = []
    offer = None
    seen = set()
    for grant in objects(block["grants"]):
        credit_id = grant.get("id")
        remaining = count(grant.get("resets_left"))
        if not isinstance(credit_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", credit_id):
            continue
        if credit_id in seen:
            return None, None
        seen.add(credit_id)
        if not remaining or grant.get("paused") is not False:
            continue
        try:
            starts, expires = _bound(grant, "starts_at"), _bound(grant, "ends_at")
        except ValueError:
            continue
        if (starts and starts > now) or (expires and expires <= now):
            continue
        item = ResetOffer(credit_id, expires, remaining)
        held.append(item)
        if (
            credit_id == block.get("next_grant_id") and grant.get("usable_now") is True
            and (grant.get("use_requires_limit") is False or block.get("at_limit") is True)
            and not cooldown and not grant.get("blocking") and _has_windows(grant.get("clears"))
        ):
            offer = item
    return ResetCredits(sum(x.remaining for x in held), _next_expiry(held), 1 if offer else 0, cooldown), offer


def _has_windows(value: object) -> bool:
    return isinstance(value, list) and any(isinstance(item, str) and item for item in value)


def _next_expiry(offers: list[ResetOffer]) -> str | None:
    return min((offer.expires_at for offer in offers if offer.expires_at), default=None)


def codex_reset_inventory(block: dict, *, now: str | None = None) -> tuple[ResetCredits | None, ResetOffer | None]:
    available = count(block.get("available_count"))
    if available is None:
        return None, None
    now = now or _utc_now()
    offers = []
    for item in objects(block.get("credits")):
        credit_id = item.get("id")
        if item.get("status") != "available" or item.get("reset_type") != "codex_rate_limits":
            continue
        if not isinstance(credit_id, str) or not credit_id or len(credit_id) > 128 or not credit_id.isprintable():
            continue
        try:
            expires = _bound(item, "expires_at")
        except ValueError:
            continue
        if expires is None or expires > now:
            offers.append(ResetOffer(credit_id, expires))
    expiry = _next_expiry(offers) if available else None
    selected = min(offers, key=lambda offer: (offer.expires_at is None, offer.expires_at or "")) if available and offers else None
    return ResetCredits(available, expiry), selected
