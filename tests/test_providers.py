"""Provider payloads, process isolation, native credential adapters and cache."""

from __future__ import annotations

import base64
import ctypes as ct
import io
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from claude_swap import cli as main_cli
from claude_swap.providers import agy, agy_keyring, api, claude, codex, coding_credentials, common, quota


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def codex_document(email):
    return {"auth_mode": "chatgpt", "tokens": {
        "account_id": "workspace", "id_token": jwt(email=email, sub=email),
        "access_token": f"access-{email}", "refresh_token": f"refresh-{email}",
    }}


def agy_document(email):
    return {"auth_method": "consumer", "id_token_email": email, "id_token": jwt(email=email),
            "token": {"access_token": "test-access", "refresh_token": "test-refresh",
                      "expiry": "2026-11-18T21:30:00Z"}}


def test_agy_reads_legacy_stash_and_switches_after_backing_up(monkeypatch, tmp_path):
    store = agy.AgyAccounts(tmp_path / ".cswap")
    active = store.parse(agy_document("first@example.com"))
    target = store.parse(agy_document("second@example.com"))
    common.write_json(Path.home() / ".nova/agy/legacy.json", target.data)
    monkeypatch.setattr(store, "read_active", lambda: active)

    def write_live(updated):
        assert common.read_json(store.directory / "first@example.com.json") == active.data
        assert updated == target

    monkeypatch.setattr(store, "write_active", write_live)
    assert [(row.label, row.active) for row in store.roster()[0]] == [
        ("second@example.com", False), ("first@example.com", True),
    ]
    assert store.switch("1") == "second@example.com"


def test_agy_rejects_conflicting_oauth_identity(tmp_path):
    data = agy_document("first@example.com")
    data["id_token_email"] = "second@example.com"
    with pytest.raises(common.ProviderError, match="conflicting email"):
        agy.AgyAccounts(tmp_path).parse(data)


def test_windows_credential_round_trip_preserves_metadata(monkeypatch):
    original_blob = b'{"old": true}'
    buffer = (ct.c_ubyte * len(original_blob)).from_buffer_copy(original_blob)
    entry = agy_keyring.Credential(Type=1, TargetName=agy_keyring.TARGET, Persist=3,
                                   CredentialBlobSize=len(buffer), CredentialBlob=buffer,
                                   UserName="existing-user")
    writes, frees = [], []

    def read(target, kind, flags, out):
        assert (target, kind, flags) == (agy_keyring.TARGET, 1, 0)
        ct.cast(out, ct.POINTER(ct.POINTER(agy_keyring.Credential)))[0] = ct.pointer(entry)
        return 1

    def write(ptr, flags):
        credential = ct.cast(ptr, ct.POINTER(agy_keyring.Credential)).contents
        writes.append((credential.UserName, credential.Persist, credential.TargetName,
                       ct.string_at(credential.CredentialBlob, credential.CredentialBlobSize)))
        return 1

    monkeypatch.setattr(agy_keyring, "_windows_api", lambda: SimpleNamespace(
        CredReadW=read, CredWriteW=write, CredFree=lambda entry: frees.append(bool(entry)),
    ))
    assert agy_keyring.windows_read() == original_blob
    agy_keyring.windows_write(b'{"updated": true}')
    assert writes == [("existing-user", 3, agy_keyring.TARGET, b'{"updated": true}')]
    assert frees == [True, True]


def test_agy_quota_is_scoped_and_uses_no_window_on_windows(monkeypatch, tmp_path):
    store = agy.AgyAccounts(tmp_path)
    login = store.parse(agy_document("first@example.com"))
    account = agy.Account("1", login.label, login, True)
    monkeypatch.setattr(agy.agy_keyring, "available_ram", lambda: agy.MIN_RAM)
    monkeypatch.setattr(agy.shutil, "which", lambda _: "agy")
    monkeypatch.setenv("OPENAI_API_KEY", "must-stay-out-of-child")
    staged_home = []

    def usage(executable, profile, env):
        assert profile != Path.home()
        assert env["HOME"] == env["USERPROFILE"] == str(profile)
        assert env["SSH_TTY"] == "cswap"
        assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        assert "OPENAI_API_KEY" not in env
        assert common.read_json(profile / store.relative_auth) == login.data
        staged_home.append(profile)
        return {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
            {"name": "Gemini", "buckets": [{"window": "weekly", "remaining_fraction": .77,
                                           "reset_time": "2026-11-18T21:30:00Z"}]},
            {"name": "Claude and GPT", "buckets": [{"window": "5h", "disabled": True}]},
        ]}}}

    monkeypatch.setattr(agy, "_scoped_usage", usage)
    monkeypatch.setattr(store, "write_active", lambda _: pytest.fail("unexpected live write"))
    result = agy.agy_quota(store, account)
    assert result.status == "ok" and result.active
    assert [(item.name, item.remaining_pct, item.disabled) for item in result.windows] == [
        ("Gemini 7d", 77, False), ("Claude and GPT 5h", None, True),
    ]
    assert not staged_home[0].exists()
    monkeypatch.setattr(agy.agy_keyring, "available_ram", lambda: agy.MIN_RAM - 1)
    assert agy.agy_quota(store, account).status == "skipped"
    monkeypatch.setattr(common.sys, "platform", "win32")
    assert common.process_options() == {"creationflags": 0x08000000}


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX executable script")
def test_agy_authentication_message_kills_the_child(tmp_path):
    executable = tmp_path / "fake-agy"
    executable.write_text(f"#!{sys.executable}\nimport sys, time\n"
                          "sys.stderr.write('Authentication required\\n'); sys.stderr.flush()\n"
                          "time.sleep(25)\n", encoding="utf-8")
    executable.chmod(0o700)
    before = time.monotonic()
    with pytest.raises(common.ProviderError, match="Sign in again"):
        agy._scoped_usage(str(executable), tmp_path, common.child_environment())
    assert time.monotonic() - before < 5


def test_windows_auth_abort_uses_taskkill_tree_without_a_window(monkeypatch):
    calls = []
    monkeypatch.setattr(agy.subprocess, "run", lambda argv, **opts: calls.append((argv, opts)))
    monkeypatch.setattr(agy.sys, "platform", "win32")
    agy._kill_tree(SimpleNamespace(pid=27, poll=lambda: 1))
    [(args, options)] = calls
    assert args == ["taskkill", "/T", "/F", "/PID", "27"]
    assert options["creationflags"] == 0x08000000


def test_codex_add_switch_and_scoped_run(monkeypatch, tmp_path):
    store = codex.CodexAccounts(tmp_path)
    first, second = codex_document("first@example.com"), codex_document("second@example.com")
    common.write_json(store.active_path, first)
    assert store.add("personal") == "personal"
    common.write_json(store.active_path, second)
    assert store.switch("personal") == "personal"
    assert common.read_json(store.active_path) == first
    assert common.read_json(store.directory / "second@example.com.json") == second

    def run(command, *, env, **options):
        assert command == ["codex", "-c", 'cli_auth_credentials_store="file"', "--version"]
        profile = Path(env["CODEX_HOME"])
        assert common.read_json(profile / "auth.json") == second
        assert "OPENAI_API_KEY" not in env
        if sys.platform == "win32":
            assert options["creationflags"] == 0x08000000
        return CompletedProcess(command, 0)

    monkeypatch.setattr(codex.CodexAccounts, "command", "codex")
    monkeypatch.setattr("claude_swap.providers.accounts.shutil.which", lambda _: "codex")
    monkeypatch.setattr("claude_swap.providers.accounts.subprocess.run", run)
    assert store.run("second@example.com", ["--version"]) == 0
    assert common.read_json(store.active_path) == first


def test_codex_usage_http_and_redaction(monkeypatch, tmp_path):
    store = codex.CodexAccounts(tmp_path)
    login = store.parse(codex_document("first@example.com"))

    def get(url, headers):
        assert url == "https://chatgpt.com/backend-api/wham/usage"
        assert headers["ChatGPT-Account-Id"] == "workspace"
        return {"plan_type": login.secrets[0], "rate_limit": {
            "primary_window": {"used_percent": 34, "limit_window_seconds": 604800,
                               "reset_at": 1795060800},
        }}

    monkeypatch.setattr(codex, "get_usage", get)
    result = codex.codex_quota(codex.Account("1", login.label, login))
    assert result.plan == "<key>"
    assert [(item.name, item.remaining_pct) for item in result.windows] == [("7d", 66)]


@pytest.mark.parametrize("provider,response,windows", [
    ("kimi-coding", {"usages": {"limit_5h": {"used_ratio": .2}, "7d": {"used_ratio": .5}}},
     [("5h", 80), ("7d", 50)]),
    ("zai-glm", {"data": {"limits": [{"unit": 6, "number": 1, "percentage": 50},
                                      {"unit": 5, "percentage": 60}]}}, [("7d", 50), ("mo", 40)]),
    ("opencode-go", {"usage": {"rolling": {"percent": 45}, "weekly": {"percent": 63},
                               "monthly": {"percent": 25}}}, [("5h", 55), ("7d", 37), ("mo", 75)]),
])
def test_coding_plan_quota_parsing(monkeypatch, tmp_path, provider, response, windows):
    settings = tmp_path / "providers.toml"
    settings.write_text(f"[{provider}.personal]\napi_key_env = 'TEST_PROVIDER_KEY'\nsession = 'test-session'\n")
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key")

    def get(url, headers):
        assert url == api.CODING_ENDPOINTS[provider][0]
        assert headers["Authorization"] == ("test-key" if provider == "zai-glm" else "Bearer test-key")
        if provider == "opencode-go":
            assert headers["x-opencode-session"] == "test-session"
        return response

    monkeypatch.setattr(api, "get_usage", get)
    job, = api.coding_plan_jobs(tmp_path, provider)
    row = job()
    assert row.account == "personal" and row.status == "ok"
    assert [(item.name, item.remaining_pct) for item in row.windows] == windows


def test_existing_codexbar_key_yields_to_explicit_configuration(monkeypatch, tmp_path):
    config = Path.home() / ".config/codexbar/config.json"
    common.write_json(config, {"providers": [{"id": "zai", "apiKey": "codexbar-test-key", "enabled": True}]})
    original = config.read_bytes()
    for name in api.CODING_ENDPOINTS["zai-glm"][1]:
        monkeypatch.delenv(name, raising=False)

    requests = []

    def get(url, headers):
        requests.append((url, headers["Authorization"]))
        return {"data": {"limits": [{"unit": 3, "percentage": 20}]}}

    monkeypatch.setattr(api, "get_usage", get)
    job, = api.coding_plan_jobs(tmp_path, "zai-glm")
    assert job().account == "CodexBar"
    monkeypatch.setenv("ZAI_CODING_PLAN_API_KEY", "explicit-test-key")
    job, = api.coding_plan_jobs(tmp_path, "zai-glm")
    assert job().account == "default"
    (tmp_path / "providers.toml").write_text('[zai-glm.personal]\napi_key = "toml-test-key"\n')
    job, = api.coding_plan_jobs(tmp_path, "zai-glm")
    assert job().account == "personal"
    url = api.CODING_ENDPOINTS["zai-glm"][0]
    assert requests == [(url, key) for key in ("codexbar-test-key", "explicit-test-key", "toml-test-key")]
    assert config.read_bytes() == original


def test_kimi_cli_credentials_use_only_the_official_regional_host(monkeypatch, tmp_path):
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    config = tmp_path / ".kimi-code/config.toml"
    config.parent.mkdir()
    config.write_text('[providers."managed:kimi-code"]\nbase_url = "https://api.kimi.ai/coding/v1"\napi_key = "test-key"\n')
    assert coding_credentials.local_coding_credential("kimi-coding", tmp_path) == (
        "test-key", "Kimi CLI", "https://api.kimi.ai/coding/v1/usages",
    )
    config.write_text('[providers."managed:kimi-code"]\nbase_url = "https://api.kimi.ai.example.org/coding"\napi_key = "test-key"\n')
    assert coding_credentials.local_coding_credential("kimi-coding", tmp_path) == ("", "-", None)


def test_opencode_go_legacy_auth_and_unconfigured_row(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    for name in api.CODING_ENDPOINTS["opencode-go"][1]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api, "get_usage", lambda *_: pytest.fail("unexpected request without a Go credential"))
    auth = tmp_path / "opencode/auth.json"
    common.write_json(auth, {"opencode": {"type": "api", "key": "legacy-zen-test-key"}})
    job, = api.coding_plan_jobs(tmp_path, "opencode-go")
    assert job().status == "unconfigured"
    common.write_json(auth, {"opencode-go": {"type": "api", "key": "legacy-go-test-key"}})

    def get(url, headers):
        assert url == "https://opencode.ai/zen/go/v1/usage"
        assert headers["Authorization"] == "Bearer legacy-go-test-key"
        return {"usage": {"rolling": {"status": "rate-limited", "percent": 80},
                          "weekly": {"percent": 10}, "monthly": {"percent": 20}}}

    monkeypatch.setattr(api, "get_usage", get)
    job, = api.coding_plan_jobs(tmp_path, "opencode-go")
    row = job()
    assert row.status == "ok" and row.account == "OpenCode"
    assert [w.remaining_pct for w in row.windows] == [0, 90, 80]


def test_opencode_go_current_database_selects_the_active_go_login(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    directory = tmp_path / "opencode"
    directory.mkdir()
    database = directory / "opencode.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE credential (integration_id TEXT, active INTEGER, value TEXT)")
        db.executemany("INSERT INTO credential VALUES (?, ?, ?)", [
            ("opencode", 1, json.dumps({"type": "key", "key": "generic-test-key"})),
            ("opencode-go", 0, json.dumps({"type": "key", "key": "inactive-test-key"})),
            ("opencode-go", 1, json.dumps({"type": "oauth", "access": "fresh-test-token",
                                          "expires": int(time.time() * 1000) + 60000})),
        ])
    original = database.read_bytes()
    assert coding_credentials.local_coding_credential("opencode-go", tmp_path) == (
        "fresh-test-token", "OpenCode", None,
    )
    assert database.read_bytes() == original
    with sqlite3.connect(database) as db:
        db.execute("UPDATE credential SET active = 1 WHERE integration_id = 'opencode-go'")
    with pytest.raises(common.ProviderError, match="Multiple active OpenCode credentials"):
        coding_credentials.local_coding_credential("opencode-go", tmp_path)


def test_http_denial_does_not_expose_body_or_headers(monkeypatch):
    def fail(request, timeout):
        assert request.get_method() == "GET"
        raise HTTPError(request.full_url, 403, "secret-from-reason", {}, io.BytesIO(b"secret-from-body"))

    monkeypatch.setattr(api, "build_opener", lambda *_: SimpleNamespace(open=fail))
    with pytest.raises(common.ProviderError, match=r"^HTTP 403: access denied or subscription unavailable$"):
        api.get_usage(api.CODING_ENDPOINTS["opencode-go"][0], {"Authorization": "secret-from-header"})


def test_opencode_entitlement_error_has_a_fixed_message(monkeypatch):
    def fail(request, timeout):
        body = io.BytesIO(b'{"error":{"type":"EntitlementError","message":"secret-from-body"}}')
        raise HTTPError(request.full_url, 403, "Forbidden", {}, body)

    monkeypatch.setattr(api, "build_opener", lambda *_: SimpleNamespace(open=fail))
    with pytest.raises(common.ProviderError, match=r"^HTTP 403: OpenCode Go subscription required$"):
        api.get_usage(api.CODING_ENDPOINTS["opencode-go"][0], {"Authorization": "secret-from-header"})


def test_claude_uses_only_usage_endpoint_and_ignores_header_probe_data(monkeypatch):
    creds = json.dumps({"claudeAiOauth": {"accessToken": "test-token", "refreshToken": "test-refresh"}})
    row = common.ProviderAccountQuota("claude", "one@example.com")

    def get(url, headers):
        assert url == "https://api.anthropic.com/api/oauth/usage"
        assert headers["Authorization"] == "Bearer test-token"
        return {"five_hour": {"utilization": 11}, "limits": [
            {"scope": {"model": {"display_name": "Fable"}}, "percent": 43},
        ]}

    monkeypatch.setattr(claude, "get_usage", get)
    result = claude._claude_row(row, creds, {})
    assert [(item.name, item.remaining_pct) for item in result.windows] == [("5h", 89), ("Fable 7d", 57)]

    def fail(*_):
        raise common.ProviderError("HTTP 429")

    monkeypatch.setattr(claude, "get_usage", fail)
    previous = {"source": claude.oauth.USAGE_SOURCE_HEADERS, "five_hour": {"pct": 2}}
    result = claude._claude_row(row, creds, previous)
    assert result.status == "error" and not result.windows


def test_quota_cache_filters_refreshes_and_invalidates_per_provider(monkeypatch, tmp_path):
    calls = []

    def jobs(provider, root, switcher):
        calls.append(provider)
        return [lambda: common.ProviderAccountQuota(provider, "personal", windows=(common.window("5h", 72),))]

    monkeypatch.setattr(quota, "_jobs", jobs)
    first = quota.collect_quota(("agy", "codex", "agy"), root=tmp_path)
    assert [row.provider for row in first] == ["agy", "codex"]
    assert sorted(calls) == ["agy", "codex"]
    assert quota.collect_quota(("agy",), root=tmp_path) == first[:1]
    assert len(calls) == 2
    quota.collect_quota(("agy",), root=tmp_path, refresh=True)
    quota.invalidate_quota("codex", tmp_path)
    quota.collect_quota(("agy", "codex"), root=tmp_path)
    assert calls[2:] == ["agy", "codex"]


def test_reset_clock_uses_korea_date():
    now = datetime.fromisoformat("2026-11-19T00:01:00+09:00")
    assert common.reset_clock("2026-11-18T15:30:00+00:00", now) == "00:30"
    assert common.reset_clock("2026-11-19T15:30:00+00:00", now) == "11-20 00:30"


def test_legacy_list_keeps_accounts_contract(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cswap", "list", "--json"])
    monkeypatch.setattr(main_cli, "_use_native_tls", lambda: None)
    main_cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == 1
    assert payload["accounts"] == []
    assert "activeAccountNumber" in payload
