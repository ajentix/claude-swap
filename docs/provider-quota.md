# Provider quota and accounts

```sh
cswap quota
cswap quota --provider agy --provider codex --json --refresh
```

The table shows remaining quota, the active login (`*`), and reset times in KST. JSON has `schemaVersion` and an `accounts` array. Each row includes `provider`, `account`, `number`, `active`, `switchable`, `plan`, `windows`, `status` and `error`. Windows include `name`, `remaining_pct`, UTC `resets_at` and `disabled`. Missing values are `null`. `cswap list --json` still uses the Claude list schema.

Quota results are cached per provider for 60 seconds in `~/.cswap/quota_cache.json`. Use `--refresh` for a measurement now. Requests go to subscription usage endpoints or the Antigravity `/usage` command. They never send a prompt. Claude plan names come from the existing subscription store, when available. A failed Claude fetch can show a previous reading marked `stale`.

## Reset credits

Claude and ChatGPT Codex subscription accounts show a **Reset credits** row when the provider reports a balance. The time in that row is the next credit expiration in KST. Claude can report banked credits before they are usable. A missing balance appears as `reset_credits: null` in quota JSON.

```sh
cswap quota --provider claude --provider codex --refresh
cswap reset claude 2
cswap reset codex personal
```

In the TUI, open **All Providers Quota**, select an account and press `x`. Review the account shown in the confirmation dialog. For scripts, use `--yes` to explicitly confirm and `--json` for the provider's outcome. A reset uses one credit on the selected account. It neither switches logins nor purchases credits.

After a reset, the provider's quota cache is invalidated. If the outcome is unknown, check the provider's quota. To retry within 24 hours, repeat the same command with `--retry`. `cswap` keeps the exact credit and request ID for that retry. It stores pending requests locally in `~/.cswap/reset_requests/` and removes the credit identifier when the outcome is settled.

Protocol references include [OpenAI Codex's reset client](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client/rate_limit_resets.rs), [Claude reset usage and redemption in Sub2API](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/claude_reset_redeem.go), [Juicebar's confirmed redemption](https://github.com/Rasalas/juicebar/pull/21) and [OpenCodex's retry fix](https://github.com/lidge-jun/opencodex/pull/3919).

## Local accounts

After signing in with the provider CLI, save the login. Repeat after signing in to another account.

```sh
cswap agy add
cswap codex add personal
cswap agy list --refresh
cswap codex list
cswap agy switch 2 --json
cswap codex switch personal
cswap agy run 1 -- --version
cswap codex run personal -- --version
```

Antigravity uses Credential Manager on Windows and Keychain on macOS. Saved logins live in `~/.cswap/agy/`. Accounts under `~/.nova/agy/` are also discovered. Antigravity quota checks use isolated temporary homes and run at most two at a time. On Windows a check is skipped below 3.5 GiB of available RAM.

Codex uses the direct CLI login in `~/.codex/auth.json` and saves profiles in `~/.cswap/codex/`. Use file-based Codex authentication for this workflow. ChatGPT OAuth logins have subscription quota. API-key logins can be saved and switched and show quota as unavailable.

`switch` saves the outgoing login before installing the selected one. `run` launches the CLI with a temporary profile and passes it the arguments after `--`. The user chooses the CLI action executed by `run`. Stashed files contain credentials and should be kept private. In the Textual quota screen, use `r` to refresh, arrows and Enter to switch, or the displayed number keys for the first nine inactive switchable accounts.

## Coding plans

Use the `KIMI_CODING_API_KEY`, `ZAI_API_KEY` and `OPENCODE_GO_API_KEY` environment variables, or put account definitions in `~/.cswap/providers.toml`. A provider can have multiple named tables. `api_key_env` names an environment variable. `api_key` can hold the value directly in a private config file. OpenCode Go can also use `session_env`, `session` or the `OPENCODE_GO_SESSION` environment variable for its `x-opencode-session` header.

When a provider has no explicit configuration, `cswap` checks existing CodexBar and OpenCode logins, then the managed Kimi CLI configuration for Kimi Coding. These credential stores are read-only. A provider without a key shows `unconfigured`.

```toml
[kimi-coding.personal]
api_key_env = "KIMI_CODING_API_KEY"

[zai-glm.personal]
api_key_env = "ZAI_API_KEY"

[opencode-go.personal]
api_key_env = "OPENCODE_GO_API_KEY"
session_env = "OPENCODE_GO_SESSION"
```

Coding-plan rows are usage-only. HTTP errors appear in the affected row.

On Windows, install over the existing `cswap` tool from PowerShell and check the three plans together:

```powershell
uv tool install --force 'git+https://github.com/ajentix/claude-swap.git@main'
cswap quota --refresh --provider kimi-coding --provider zai-glm --provider opencode-go
```

The install uses the existing Claude account store for that Windows user. Antigravity and Codex read that user's active native logins. A subscription is discoverable when its key is available in an environment variable, in `providers.toml`, or in a supported local credential store.
