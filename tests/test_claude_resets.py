"""Claude reset credits read with the CLI identity, cached, and shown over cswap's own reading."""

from __future__ import annotations

import time

import pytest

from claude_swap import oauth
from claude_swap.providers import claude, claude_resets, quota
from claude_swap.providers.common import ProviderAccountQuota, ProviderError, ResetCredits

CREDS = '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-test", "refreshToken": "r"}}'
GRANT = {"id": "g1", "resets_left": 1, "resets_total": 1, "starts_at": "2026-11-01T00:00:00+00:00",
         "ends_at": "2099-12-10T22:00:00+00:00", "usable_now": True, "clears": ["seven_day"], "type": "weekly",
         "paused": False, "use_requires_limit": False, "blocking": []}


@pytest.fixture(autouse=True)
def version(monkeypatch):
    claude_resets.cli_version.cache_clear()
    monkeypatch.setattr(claude_resets, "cli_version", lambda: "2.1.328")


def test_inventory_uses_the_cli_identity_and_usage_polling_does_not():
    assert claude.claude_headers(CREDS, cli_identity=True)["User-Agent"] == "claude-cli/2.1.328 (external, cli)"
    assert "User-Agent" not in claude.claude_headers(CREDS)  # ordinary reads keep cswap's own agent


def test_refresh_reads_and_keeps_the_inventory(tmp_path, monkeypatch):
    from claude_swap.providers import api

    seen = {}

    def get(url, headers, **_):
        seen.update(headers)
        return {"cedar_ember": {"eligible": True, "grants": [GRANT]}}

    monkeypatch.setattr(api, "get_usage", get)
    credits = claude_resets.refresh("a@example.com", CREDS, tmp_path)
    assert seen["User-Agent"].startswith("claude-cli/")
    assert credits.available_count == 1 and credits.next_expiry.startswith("2099-12-10")
    assert claude_resets.cached("a@example.com", tmp_path) == credits
    assert not claude_resets.due("a@example.com", tmp_path)
    assert claude_resets.due("a@example.com", tmp_path, now=time.time() + claude_resets.TTL_S + 1)


def test_a_failed_read_waits_before_the_next_try(tmp_path, monkeypatch):
    from claude_swap.providers import api

    def get(url, headers, **_):
        raise ProviderError("HTTP 429: rate limited")

    monkeypatch.setattr(api, "get_usage", get)
    assert claude_resets.refresh_due([("a@example.com", CREDS)], tmp_path) == 0
    assert not claude_resets.due("a@example.com", tmp_path)
    assert claude_resets.due("a@example.com", tmp_path, now=time.time() + claude_resets.RETRY_S + 1)
    assert claude_resets.cached("a@example.com", tmp_path) is None


def test_quota_rows_show_the_inventory_even_from_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_resets, "home", lambda: tmp_path)
    claude_resets._save("a@example.com", None, at=time.time(),
                        credits={"available_count": 4, "next_expiry": "2099-12-10T22:00:00+00:00"})
    row = ProviderAccountQuota("claude", "work (a@example.com)", "1", reset_credits=ResetCredits(0, eligible=False))
    shown = quota._with_claude_inventory(row)
    assert shown.reset_credits.available_count == 4
    other = ProviderAccountQuota("codex", "a@example.com", "1")
    assert quota._with_claude_inventory(other) is other
