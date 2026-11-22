# Provider quota and accounts

```sh
cswap quota
cswap quota --provider agy --provider codex --json --refresh
```

The table shows remaining quota, the active login (`*`), and reset times in KST. JSON has `schemaVersion` and an `accounts` array. Each row includes `provider`, `account`, `number`, `active`, `switchable`, `plan`, `windows`, `status` and `error`. Windows include `name`, `remaining_pct`, UTC `resets_at` and `disabled`. Missing values are `null`. `cswap list --json` still uses the Claude list schema.

Watch, quota, and Codex auto-switch share provider readings for 60 seconds in `~/.cswap/quota_cache.json`. `--refresh` requests a fresh reading when no other collector holds the provider's claim and no `Retry-After` is active. Each account retains its own measurement time. The table shows read and retry times in KST. JSON adds Unix times `fetched_at` and `retry_at`, and the upstream `http_status` when available. Claude quota also reuses recent usage-endpoint readings and account backoff from the native Claude store. The direct quota collector calls usage endpoints or Antigravity's `/usage` command.

## Login diagnostics

```sh
cswap doctor
cswap doctor --provider codex --provider opencode-go --live --json
```

Without `--live`, doctor reads local credentials and reports their source, known expiry, configuration problems, and sign-in steps. `--live` also checks subscription access using usage endpoints and Antigravity's isolated `/usage` command. API-key Codex logins and inference-only Claude setup tokens have no subscription usage API access.

## Reset credits

Claude, ChatGPT Codex, and z.ai GLM subscriptions show a **Reset credits** row when the provider reports a balance. The time in that row is the next credit expiration in KST. A missing balance appears as `reset_credits: null` in quota JSON. Kimi Coding and OpenCode Go show the renewal times reported by their usage endpoints.

```sh
cswap quota --provider claude --provider codex --refresh
cswap reset claude 2 --list
cswap reset claude 2
cswap reset codex personal --list --json
cswap reset codex personal --credit 2
cswap reset zai-glm CodexBar --list
cswap reset zai-glm CodexBar --credit 1
```

In the TUI, open **All Providers Quota**, select an account and press `x`. Highlight a credit to see the limits it clears and whether it can be used now. Held credits show the known condition, such as reaching a usage limit or waiting for a cooldown. The screen also shows credits left and any previous request that can be retried. Press Enter on an available credit to review the account and affected limits before confirming. For scripts, `--yes` confirms the selected credit and `--json` prints the outcome. CLI credit numbers refer to the live order shown by `--list`.

- **Claude** shows the windows the grant clears, such as Fable, account-wide windows, or an explicitly all-limits grant. Claude chooses which grant is eligible next and may require reaching a limit or waiting out a cooldown.
- **Codex** shows each available credit and its expiration. With no explicit choice, it picks the earliest available expiration.
- **z.ai GLM** shows separate 5-hour and weekly counts for personal plans. Each use request targets exactly one pack and the limit designated by the provider.

After a reset, the provider's quota cache is invalidated. If the outcome is unknown, inspect the request in the TUI or with `--list`. For 24 hours, the TUI and `--retry` resend the same credit and request ID. Pending records live in `~/.cswap/reset_requests/`. The credit identifier is removed when the outcome is settled.

Protocol references include [OpenAI Codex's reset client](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client/rate_limit_resets.rs), [Claude redemption in Sub2API](https://github.com/Wei-Shaw/sub2api/blob/main/backend/internal/service/claude_reset_redeem.go), [z.ai pack operations in MaxPool](https://github.com/2solarmax/maxpool/blob/HEAD/src/zai-reset-cards.js), and [OpenCodex's retry fix](https://github.com/lidge-jun/opencodex/pull/3919).

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

When a saved Codex login is more than 8 days past its last refresh, nearly expired, or returns 401 to a usage request, cswap can refresh it after verifying its account identity. Refresh, activation, and a scoped run of the same login wait for each other. The live login remains under Codex CLI's control. For an expired saved login, sign in with `codex` and run `cswap codex add` again. The `codex auto --dry-run` command only reads usage.

### Automatic Codex switching

```sh
cswap codex auto                        # check every 60s, switch before a limit
cswap codex auto --threshold 85         # 5-hour line
cswap codex auto --weekly-threshold 97  # weekly line (default: the same)
cswap codex auto --once --json          # one check for a scheduler; exit 0 switched, 1 error, 2 nothing, 3 blocked
cswap codex auto --once --dry-run --json
cswap codex policy                      # list each login's priority and hold
cswap codex policy personal --priority 5
cswap codex policy personal --hold
cswap codex policy personal --release
```

An account on hold is excluded from automatic selection and can still be selected manually. Higher priority wins among accounts eligible under the threshold and improvement rules. Ties use remaining quota. Defaults are priority 0 and no hold. Policy follows the login when its label or slot changes. Dry-run shows each account's status, priority, headroom, and the selected account. A five-minute cooldown follows each automatic switch.

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
