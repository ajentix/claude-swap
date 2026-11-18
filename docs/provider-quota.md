# Provider quota and accounts

```sh
cswap quota
cswap quota --provider agy --provider codex --json --refresh
```

The table shows remaining quota, the active login (`*`), and reset times in KST. JSON has `schemaVersion` and an `accounts` array. Each row includes `provider`, `account`, `number`, `active`, `switchable`, `plan`, `windows`, `status` and `error`. Windows include `name`, `remaining_pct`, UTC `resets_at` and `disabled`. Missing values are `null`. `cswap list --json` still uses the Claude list schema.

Quota results are cached per provider for 60 seconds in `~/.cswap/quota_cache.json`. Use `--refresh` for a measurement now. Requests go to subscription usage endpoints or the Antigravity `/usage` command. They never send a prompt. Claude plan names come from the existing subscription store, when available. A failed Claude fetch can show a previous reading marked `stale`.

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
