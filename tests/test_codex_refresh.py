"""Refreshing saved Codex logins: when, what is written, and what is never touched."""

from __future__ import annotations

import base64
import io
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from claude_swap.providers import codex
from claude_swap.providers.accounts import Account
from claude_swap.providers.common import ProviderError, read_json, write_json


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def document(refresh="rt-1", access_exp=None, last_refresh=None):
    exp = access_exp if access_exp is not None else time.time() + 86400
    return {"auth_mode": "chatgpt", "last_refresh": last_refresh or datetime.now(timezone.utc).isoformat(),
            "tokens": {"account_id": "ws", "id_token": jwt(email="b@example.com", sub="u-b"),
                       "access_token": jwt(exp=exp), "refresh_token": refresh}}


@pytest.fixture
def saved(tmp_path):
    store = codex.CodexAccounts(tmp_path)
    path = tmp_path / "codex" / "b.json"
    write_json(path, document())
    account = Account("2", "b@example.com", store.parse(read_json(path)), False, path)
    return store, account, path


class TestDue:
    def test_recent_login_is_not_due(self):
        assert not codex._refresh_due(document(), time.time())

    def test_old_refresh_or_expiring_access_is_due(self):
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        assert codex._refresh_due(document(last_refresh=old), time.time())
        assert codex._refresh_due(document(access_exp=time.time() + 60), time.time())


class TestRefreshSaved:
    def test_writes_rotated_tokens_and_keeps_missing_ones(self, saved, monkeypatch):
        store, account, path = saved
        monkeypatch.setattr(codex, "_post_refresh", lambda rt: {"access_token": jwt(exp=time.time() + 9e5),
                                                                "refresh_token": "rt-2"})
        before = read_json(path)
        fresh = codex.refresh_saved(account, store)
        after = read_json(path)
        assert after["tokens"]["refresh_token"] == "rt-2"
        assert after["tokens"]["id_token"] == before["tokens"]["id_token"]  # absent in the response: kept
        assert after["last_refresh"].endswith("Z")
        assert fresh.login.data["tokens"]["refresh_token"] == "rt-2"

    def test_never_the_live_login(self, saved, monkeypatch):
        store, account, _ = saved
        monkeypatch.setattr(codex, "_post_refresh", lambda rt: pytest.fail("refreshed the live login"))
        live = Account("1", account.label, account.login, True, account.path)
        assert codex.refresh_saved(live, store) is live

    def test_file_changed_meanwhile_is_not_overwritten(self, saved, monkeypatch):
        store, account, path = saved

        def rotate_elsewhere(rt):
            write_json(path, document(refresh="rt-other"))
            return {"access_token": jwt(exp=1), "refresh_token": "rt-2"}

        monkeypatch.setattr(codex, "_post_refresh", rotate_elsewhere)
        with pytest.raises(ProviderError, match="changed"):
            codex.refresh_saved(account, store)
        assert read_json(path)["tokens"]["refresh_token"] == "rt-other"

    @pytest.mark.parametrize("status,body", [
        (400, {"error": {"code": "refresh_token_reused"}}),
        (400, {"error": "invalid_grant"}),
        (401, {}),
    ])
    def test_spent_refresh_token_means_sign_in_again(self, monkeypatch, status, body):
        def opener(*_a, **_k):
            class O:
                def open(self, request, timeout):
                    assert json.loads(request.data) == {"grant_type": "refresh_token",
                                                        "client_id": codex.CLIENT_ID, "refresh_token": "rt"}
                    raise HTTPError(request.full_url, status, "x", {}, io.BytesIO(json.dumps(body).encode()))
            return O()

        monkeypatch.setattr(codex, "build_opener", opener)
        with pytest.raises(ProviderError, match="(?i)sign in"):
            codex._post_refresh("rt")


def test_quota_retries_once_after_a_401(saved, monkeypatch):
    store, account, _ = saved
    calls = []

    def get(url, headers, *, timeout=None):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            raise ProviderError("HTTP 401: authentication required")
        return {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": 18000}}}

    monkeypatch.setattr(codex, "get_usage", get)
    monkeypatch.setattr(codex, "_post_refresh", lambda rt: {"access_token": jwt(exp=time.time() + 9e5, n=2),
                                                            "refresh_token": "rt-2"})
    row = codex.codex_quota(account, store)
    assert row.status == "ok" and [w.name for w in row.windows] == ["5h"]
    assert len(calls) == 2 and calls[0] != calls[1]
