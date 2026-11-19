"""Decode subscription reset inventory and keep redemption identifiers internal."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .common import ResetCredits, object_dict, objects, timestamp

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
CODEX_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
ZAI_CREDIT_BASE = "https://api.z.ai/api/biz/customer-package-reset"
ZAI_CREDITS_URL = f"{ZAI_CREDIT_BASE}/list?targetType=PERSONAL"


@dataclass(frozen=True)
class ResetOffer:
    credit_id: str = field(repr=False)
    expires_at: str | None = None
    remaining: int = 1
    windows: tuple[str, ...] = ()
    usable: bool = True
    label: str = "Reset credit"
    reset_type: str | None = None
    grant_type: str | None = field(default=None, repr=False)


CLAUDE_WINDOWS = {
    "five_hour": "5h", "seven_day": "7d", "seven_day_fable": "Fable 7d",
    "fable": "Fable", "seven_day_sonnet": "Sonnet 7d",
    "seven_day_overage_included": "Included weekly overage",
    "all": "All usage limits",
}


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


def claude_reset_offers(data: dict, *, now: str | None = None) -> tuple[ResetCredits | None, list[ResetOffer]]:
    block = object_dict(data.get("cedar_ember"))
    eligible = block.get("eligible")
    if eligible is False:
        return ResetCredits(0, redeemable_count=0), []
    if eligible is not True or not isinstance(block.get("grants"), list):
        return None, []
    now = now or _utc_now()
    try:
        cooldown = _bound(block, "cooldown_until")
    except ValueError:
        return None, []
    cooldown = cooldown if cooldown and cooldown > now else None
    held = []
    seen = set()
    for grant in objects(block["grants"]):
        credit_id = grant.get("id")
        remaining = count(grant.get("resets_left"))
        if not isinstance(credit_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", credit_id):
            continue
        if credit_id in seen:
            return None, []
        seen.add(credit_id)
        if not remaining or grant.get("paused") is not False:
            continue
        try:
            starts, expires = _bound(grant, "starts_at"), _bound(grant, "ends_at")
        except ValueError:
            continue
        if (starts and starts > now) or (expires and expires <= now):
            continue
        raw_scopes = grant.get("clears")
        scopes = tuple(dict.fromkeys(
            CLAUDE_WINDOWS.get(item, "Other provider limit")
            for item in raw_scopes if isinstance(item, str) and item
        )) if isinstance(raw_scopes, list) else ()
        usable = bool(
            credit_id == block.get("next_grant_id") and grant.get("usable_now") is True
            and (grant.get("use_requires_limit") is False or block.get("at_limit") is True)
            and not cooldown and not grant.get("blocking") and scopes
        )
        held.append(ResetOffer(credit_id, expires, remaining, scopes, usable, "Claude reset"))
    return ResetCredits(sum(x.remaining for x in held), _next_expiry(held), sum(x.usable for x in held), cooldown), held


def claude_reset_inventory(data: dict, *, now: str | None = None) -> tuple[ResetCredits | None, ResetOffer | None]:
    credits, offers = claude_reset_offers(data, now=now)
    return credits, next((offer for offer in offers if offer.usable), None)


def _next_expiry(offers: list[ResetOffer]) -> str | None:
    return min((offer.expires_at for offer in offers if offer.expires_at), default=None)


def codex_reset_offers(block: dict, *, now: str | None = None) -> tuple[ResetCredits | None, list[ResetOffer]]:
    available = count(block.get("available_count"))
    if available is None:
        return None, []
    now = now or _utc_now()
    offers = []
    seen = set()
    for item in objects(block.get("credits")):
        credit_id = item.get("id")
        if item.get("status") != "available" or item.get("reset_type") != "codex_rate_limits":
            continue
        if not isinstance(credit_id, str) or not credit_id or len(credit_id) > 128 or not credit_id.isprintable():
            continue
        if credit_id in seen:
            continue
        seen.add(credit_id)
        try:
            expires = _bound(item, "expires_at")
        except ValueError:
            continue
        if expires is None or expires > now:
            offers.append(ResetOffer(credit_id, expires, windows=("Codex usage limits",), label="Codex reset"))
    offers.sort(key=lambda offer: (offer.expires_at is None, offer.expires_at or "", offer.credit_id))
    offers = offers[:available]
    return ResetCredits(available, _next_expiry(offers)), offers


def codex_reset_inventory(block: dict, *, now: str | None = None) -> tuple[ResetCredits | None, ResetOffer | None]:
    credits, offers = codex_reset_offers(block, now=now)
    return credits, next(iter(offers), None)


def zai_reset_offers(data: dict, *, now: str | None = None) -> tuple[ResetCredits | None, list[ResetOffer]]:
    block = object_dict(data.get("data"))
    buckets = (("fiveHourResets", "FIVE_HOUR", "5h"), ("weekResets", "WEEK", "7d"))
    if data.get("success") is not True or data.get("code") not in (0, 200):
        return None, []
    if not all(isinstance(block.get(field), list) for field, _, _ in buckets):
        return None, []
    now = now or _utc_now()
    offers = []
    seen = set()
    for field, reset_type, scope in buckets:
        for card in objects(block[field]):
            record, grant = card.get("recordId"), card.get("grantType")
            if card.get("available") is not True or type(record) is not int or record < 1:
                continue
            raw_expiry = card.get("expireTime")
            if not isinstance(raw_expiry, str):
                continue
            try:
                expires = datetime.fromisoformat(raw_expiry)
            except ValueError:
                continue
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone(timedelta(hours=8)))
            expiry = timestamp(expires.isoformat())
            if not expiry or expiry <= now:
                continue
            valid_grant = grant if isinstance(grant, str) and re.fullmatch(r"[A-Z_]{1,40}", grant) else None
            key = (record, reset_type)
            if key in seen:
                return None, []
            seen.add(key)
            offers.append(ResetOffer(str(record), expiry, windows=(scope,), usable=bool(valid_grant),
                                     label="GLM reset pack", reset_type=reset_type, grant_type=valid_grant))
    offers.sort(key=lambda offer: (offer.expires_at, offer.reset_type, offer.credit_id))
    counts = {scope: sum(offer.windows == (scope,) for offer in offers) for _, _, scope in buckets}
    return ResetCredits(len(offers), _next_expiry(offers), sum(offer.usable for offer in offers),
                        by_scope=counts), offers
