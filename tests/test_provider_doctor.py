"""Provider diagnostics reuse credential discovery and redact authentication material."""

import json

from claude_swap.providers import common, doctor


def test_doctor_reports_missing_key_and_live_subscription_failure(tmp_path, monkeypatch):
    config = tmp_path / "providers.toml"
    config.write_text(
        '[kimi-coding.personal]\napi_key_env = "TEST_MISSING_CREDENTIAL"\n'
        '[opencode-go.personal]\napi_key_env = "TEST_GO_CREDENTIAL"\n'
    )
    monkeypatch.delenv("TEST_MISSING_CREDENTIAL", raising=False)
    monkeypatch.setenv("TEST_GO_CREDENTIAL", "opaque-synthetic-token")

    def collect(providers, **kwargs):
        assert providers == ("kimi-coding", "opencode-go")
        assert kwargs["refresh_login"] is False
        return [common.ProviderAccountQuota("opencode-go", "personal", status="error", http_status=403,
                                            error="HTTP 403: OpenCode Go subscription required")]

    monkeypatch.setattr(doctor, "collect_quota", collect)
    missing, go = doctor.diagnose(("kimi-coding", "opencode-go"), live=True, root=tmp_path)
    assert missing.source == "providers.toml" and missing.status == "missing-key"
    assert go.status == "configured" and go.quota_status == "error"
    assert go.action == "Verify subscription access for this login"
    assert "opaque-synthetic-token" not in json.dumps([vars(missing), vars(go)])
