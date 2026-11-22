"""Reset inventory and explicit, restart-safe redemption."""

import json
from types import SimpleNamespace

import pytest

from claude_swap import session
from claude_swap.providers import cli, codex, common, reset, reset_credits
from claude_swap.providers.accounts import Account
from claude_swap.switcher import ClaudeAccountSwitcher


def claude_usage(**changes):
    return {"cedar_ember": {
        "eligible": True, "at_limit": True, "next_grant_id": "opaque-grant",
        "grants": [
            {"id": "opaque-grant", "resets_left": 1, "paused": False, "usable_now": True,
             "clears": ["seven_day"], "ends_at": "2099-01-02T00:00:00Z"},
            {"id": "later-grant", "resets_left": 2, "paused": False, "usable_now": False,
             "clears": ["seven_day"], "ends_at": "2099-01-03T00:00:00Z"},
            {"id": "invalid-date", "resets_left": 5, "paused": False, "ends_at": "broken"},
        ],
        **changes,
    }}


def codex_details():
    return {"available_count": 2, "credits": [
        {"id": "later-codex", "reset_type": "codex_rate_limits", "status": "available",
         "expires_at": "2099-01-03T00:00:00Z"},
        {"id": "opaque-codex", "reset_type": "codex_rate_limits", "status": "available",
         "expires_at": "2099-01-01T00:00:00Z"},
        {"id": "exhausted", "reset_type": "codex_rate_limits", "status": "redeemed"},
    ]}


def test_claude_inventory_counts_held_credits_separately_from_eligibility():
    credits, offer = reset_credits.claude_reset_inventory(claude_usage(at_limit=False))
    assert credits.available_count == 3
    assert credits.redeemable_count == 0
    assert credits.next_expiry == "2099-01-02T00:00:00+00:00"
    assert offer is None
    credits, offer = reset_credits.claude_reset_inventory(claude_usage())
    assert credits.redeemable_count == 1
    assert offer.credit_id == "opaque-grant"
    assert "opaque-grant" not in repr(offer)
    credits, offer = reset_credits.claude_reset_inventory(claude_usage(cooldown_until="2099-01-01T00:00:00Z"))
    assert credits.cooldown_until and not offer
    assert reset_credits.claude_reset_inventory({}) == (None, None)


def test_claude_distinguishes_fable_from_full_reset():
    raw = claude_usage()
    first, second, _ = raw["cedar_ember"]["grants"]
    first["clears"] = ["seven_day_fable"]
    second.update(clears=["all"], usable_now=True)
    credits, offers = reset_credits.claude_reset_offers(raw)
    assert credits.available_count == 3
    assert [(offer.windows, offer.usable) for offer in offers] == [
        (("Fable 7d",), True), (("All usage limits",), False),
    ]


def test_zai_redeems_and_retries_the_exact_window_and_pack(monkeypatch, tmp_path):
    (tmp_path / "providers.toml").write_text('[zai-glm.personal]\napi_key = "synthetic-zai-key"\n')
    raw = {"success": True, "code": 200, "data": {
        "targetType": "PERSONAL", "customerId": 123,
        "fiveHourResets": [{"recordId": 456, "grantType": "GLOBAL", "available": True,
                           "expireTime": "2099-12-01 12:30:00"}],
        "weekResets": [{"recordId": 789, "grantType": "DIRECT", "available": True,
                       "expireTime": "2099-12-01 11:30:00"}],
    }}
    posts = []

    def get(url, headers):
        assert url == reset_credits.ZAI_CREDITS_URL
        assert headers["Authorization"] == "Bearer synthetic-zai-key"
        return raw

    def post(url, headers, payload):
        assert url == reset_credits.ZAI_CREDIT_BASE + "/use"
        assert headers["Authorization"] == "Bearer synthetic-zai-key"
        posts.append(payload)
        if len(posts) == 1:
            raise common.ProviderError("timeout")
        return {"success": True, "code": 200}

    monkeypatch.setattr(reset, "get_usage", get)
    monkeypatch.setattr(reset, "post_reset", post)
    options = reset.reset_options("zai-glm", "personal", root=tmp_path)
    assert options.credits.by_scope == {"5h": 1, "7d": 1}
    assert options.offers[0].expires_at == "2099-12-01T03:30:00+00:00"
    assert not posts
    assert reset.redeem_reset(options.choose(2), root=tmp_path) == "unknown"
    pending = reset.reset_options("zai-glm", "personal", root=tmp_path)
    assert pending.pending_state == "unknown"
    assert pending.pending.offer.windows == ("5h",)
    assert reset.redeem_reset(pending.pending, root=tmp_path) == "reset"
    assert posts[0] == posts[1]
    assert {k: v for k, v in posts[0].items() if k != "requestId"} == {
        "recordId": 456, "targetType": "PERSONAL", "resetType": "FIVE_HOUR", "grantType": "GLOBAL",
    }


def test_codex_counts_without_details_and_picks_earliest_expiration():
    credits, offer = reset_credits.codex_reset_inventory({"available_count": 2})
    assert credits.available_count == 2 and offer is None
    credits, offer = reset_credits.codex_reset_inventory(codex_details())
    assert credits.available_count == 2
    assert offer.credit_id == "opaque-codex"
    assert credits.next_expiry == "2099-01-01T00:00:00+00:00"


@pytest.fixture
def codex_target(monkeypatch):
    target = reset.ResetTarget("codex", "1", "personal", "identity", "workspace", {"Authorization": "Bearer test-key"})
    monkeypatch.setattr(reset, "_target", lambda *_: target)

    def get(url, headers):
        assert url == reset.CODEX_CREDITS_URL
        assert headers == {**target.headers, **codex.CREDIT_HEADERS}
        return codex_details()

    monkeypatch.setattr(reset, "get_usage", get)
    return target


def test_lost_response_reuses_exact_request_and_credit(monkeypatch, tmp_path, codex_target):
    posts = []

    def post(url, headers, payload):
        assert url == reset.CODEX_CREDITS_URL + "/consume"
        assert headers == {**codex_target.headers, **codex.CREDIT_HEADERS}
        posts.append(payload)
        if len(posts) == 1:
            raise common.ProviderError("Usage endpoint unavailable")
        return {"code": "already_redeemed"}

    monkeypatch.setattr(reset, "post_reset", post)
    intent = reset.prepare_reset("codex", "1", root=tmp_path)
    assert posts == []
    assert reset.redeem_reset(intent, root=tmp_path) == "unknown"
    with pytest.raises(common.ProviderError, match="pending"):
        reset.prepare_reset("codex", "1", root=tmp_path)
    retry = reset.prepare_reset("codex", "1", retry=True, root=tmp_path)
    assert reset.redeem_reset(retry, root=tmp_path) == "already_redeemed"
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0]["credit_id"] == "opaque-codex"
    journal = reset._journal_path(tmp_path, codex_target).read_text()
    assert "test-key" not in journal and "opaque-codex" not in journal
    with pytest.raises(common.ProviderError, match="No pending"):
        reset.prepare_reset("codex", "1", retry=True, root=tmp_path)


def test_concurrent_confirmations_do_not_spend_two_credits(monkeypatch, tmp_path, codex_target):
    calls = []

    def post(*args):
        calls.append(args)
        return {"code": "reset"}

    monkeypatch.setattr(reset, "post_reset", post)
    first = reset.prepare_reset("codex", "1", root=tmp_path)
    second = reset.prepare_reset("codex", "1", root=tmp_path)
    assert reset.redeem_reset(first, root=tmp_path) == "reset"
    with pytest.raises(common.ProviderError, match="Another reset"):
        reset.redeem_reset(second, root=tmp_path)
    assert len(calls) == 1


def test_changed_identity_refuses_before_post(monkeypatch, tmp_path, codex_target):
    intent = reset.prepare_reset("codex", "1", root=tmp_path)
    wrong = reset.ResetTarget("codex", "1", "personal", "changed", codex_target.scope, codex_target.headers)
    monkeypatch.setattr(reset, "_target", lambda *_: wrong)
    monkeypatch.setattr(reset, "post_reset", lambda *_: pytest.fail("wrong account"))
    with pytest.raises(common.ProviderError, match="selected account changed"):
        reset.redeem_reset(intent, root=tmp_path)


def test_claude_verified_profile_and_single_post(monkeypatch, tmp_path):
    org = "cc38cbb0-f934-4ab0-afd9-a73221993c76"
    creds = json.dumps({"claudeAiOauth": {"accessToken": "test-access-token"}})
    switcher = SimpleNamespace(
        _resolve_account_identifier=lambda _: "2",
        _build_accounts_info=lambda: [(2, "user@example.com", "", org, True, creds, "personal")],
    )
    calls = []

    def get(url, headers):
        assert headers["Authorization"] == "Bearer test-access-token"
        if url == reset.CLAUDE_PROFILE_URL:
            return {"account": {"email": "user@example.com"}, "organization": {"uuid": org}}
        assert url == reset.CLAUDE_USAGE_URL
        return claude_usage()

    def post(url, headers, payload):
        assert url == reset.CLAUDE_RESET_URL.format(organization=org)
        assert payload["program"] == "cedar_ember" and payload["grant_id"] == "opaque-grant"
        calls.append(payload)
        return {"result": "reset"}

    monkeypatch.setattr(reset, "get_usage", get)
    monkeypatch.setattr(reset, "post_reset", post)
    intent = reset.prepare_reset("claude", "2", root=tmp_path, switcher=switcher)
    assert not calls
    assert reset.redeem_reset(intent, root=tmp_path, switcher=switcher) == "reset"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "is_active, profile_expiry, drifted, stale, expected",
    [
        (False, 2000, False, False, "session-access"),
        (False, 500, False, False, "slot-access"),
        (False, 2000, True, False, "slot-access"),
        (False, 2000, False, True, "slot-access"),
        (True, 2000, False, False, "slot-access"),
    ],
)
def test_claude_reset_inventory_uses_newer_matching_session_credential(
    monkeypatch, tmp_path, is_active, profile_expiry, drifted, stale, expected,
):
    org = "cc38cbb0-f934-4ab0-afd9-a73221993c76"
    email = "user@example.com"
    slot = json.dumps({"claudeAiOauth": {"accessToken": "slot-access", "expiresAt": 1000}})
    profile = json.dumps({"claudeAiOauth": {"accessToken": "session-access", "expiresAt": profile_expiry}})
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    creds_path = session_dir / ".credentials.json"
    creds_path.write_text(profile, encoding="utf-8")
    config = {"oauthAccount": {"emailAddress": "other@example.com" if drifted else email, "organizationUuid": org}}
    (session_dir / ".claude.json").write_text(json.dumps(config), encoding="utf-8")
    if stale:
        session.stale_marker_for(session_dir).touch()

    switcher = ClaudeAccountSwitcher()
    monkeypatch.setattr(switcher, "_resolve_account_identifier", lambda _: "2")
    monkeypatch.setattr(switcher, "_build_accounts_info", lambda: [(2, email, "", org, is_active, slot, "")])
    monkeypatch.setattr(switcher, "_session_dir", lambda *_: session_dir)
    monkeypatch.setattr(switcher, "_read_account_credentials_ex", lambda *_: (slot, False))

    def get(url, headers):
        assert headers["Authorization"] == f"Bearer {expected}"
        if url == reset.CLAUDE_PROFILE_URL:
            return {"account": {"email": email}, "organization": {"uuid": org}}
        assert url == reset.CLAUDE_USAGE_URL
        return claude_usage()

    monkeypatch.setattr(reset, "get_usage", get)
    monkeypatch.setattr(reset, "post_reset", lambda *_: pytest.fail("inventory must not redeem a credit"))
    options = reset.reset_options("claude", "2", root=tmp_path, switcher=switcher)
    assert options.credits.available_count == 3
    assert creds_path.read_text(encoding="utf-8") == profile


def test_confirmation_cancels_before_redeeming(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "prepare_reset", lambda *_args, **_kwargs: SimpleNamespace(prompt=lambda: "Use one credit?"))
    monkeypatch.setattr("builtins.input", lambda: "n")
    monkeypatch.setattr(cli, "redeem_reset", lambda _: pytest.fail("cancelled"))
    assert cli.reset_command(["claude", "2", "--json"], "cswap") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"


def test_codex_quota_keeps_reported_count_when_details_fail(monkeypatch, tmp_path):
    store = codex.CodexAccounts(tmp_path)
    login = store.parse({"tokens": {"account_id": "workspace", "access_token": "codex-token"}})
    account = Account("1", login.label, login)

    def get(url, headers, *, timeout=None):
        if url == codex.USAGE_URL:
            return {"rate_limit_reset_credits": {"available_count": 2}}
        assert url == reset.CODEX_CREDITS_URL and timeout == 10
        assert headers["OpenAI-Beta"] == "codex-1" and headers["originator"] == "Codex Desktop"
        raise common.ProviderError("HTTP 429: rate limited", status_code=429, retry_after=123.0)

    monkeypatch.setattr(codex, "get_usage", get)
    row = codex.codex_quota(account)
    assert row.status == "ok" and row.reset_credits.available_count == 2
    assert row.retry_at == 123.0
