# Claude Codex

Run the **Claude Code harness with GPT-6 Astra**, using your ChatGPT subscription through CLIProxyAPI's Codex OAuth provider. A separate `claude-codex` command configures the proxy connection and reasoning level. The original `claude` executable and its user configuration stay in place.

Includes a separate **Claude Codex · GPT-6 Astra** provider for your existing Paseo installation. Compatibility has been tested with **Paseo v0.8.0-beta.1**.

```text
Terminal: claude-codex ──────────┐
                               ├─ Claude Code → local CLIProxyAPI → Codex OAuth → GPT-6 Astra
Paseo: Claude Codex provider ────┘
```

## Install with one command

Run this on the computer hosting Claude Code and the Paseo daemon:

```bash
bash install.sh
```

Complete the ChatGPT sign-in when prompted. The script configures the proxy and, when detected, Paseo, including a small live request to verify Astra access. If your bin directory was added to `PATH`, open a new terminal after installation or use the absolute command printed by the installer.

Requirements:

- Linux (including WSL2) or macOS, on x86_64 or ARM64.
- Python 3.9+ and `curl`.
- Existing Claude Code, or Node.js **22+** and npm to install it privately.
- Paseo is optional: setup runs only when an executable is found on PATH or supplied with `--paseo-bin`. The installer reuses that exact executable and version. It never installs, upgrades, downgrades, or replaces Paseo.
- A ChatGPT account with subscription-based Codex access **and access to `gpt-6-astra`**.

The requested “OpenAI Max 20x” subscription is used by signing into its ChatGPT account. There is no `20x` proxy setting: model access and usage limits come from that account. A plan label alone does not establish Astra entitlement. The installer checks the actual request and reports access/quota failures. It does not require an OpenAI API key or configure pay-as-you-go API billing.

### What the installer does

1. Reuses your Claude binary. If missing, installs `@anthropic-ai/claude-code@2.1.246` into a private directory.
2. Selects the executable supplied with `--paseo-bin`, or the existing `paseo` on PATH. **If neither is available, skips all Paseo configuration and daemon operations.** No Paseo package is downloaded or version enforced; cached private copies from older installer runs are not selected automatically. `paseo-codex` invokes the detected executable.
3. Downloads **CLIProxyAPI 7.2.155** and verifies a pinned SHA-256 checksum. Linux uses the static build without plugin support.
4. Creates an isolated configuration, OAuth directory, random local API token, and Claude profile with private file permissions.
5. Installs `claude-codex`, `claude-codex-proxy`, and (when Paseo is detected) `paseo-codex` into `~/.local/bin`, then adds that directory to your shell's startup configuration.
6. When Paseo is detected, backs up and merges the new provider into `$PASEO_HOME/config.json` (default `~/.paseo/config.json`). Other providers and settings are preserved.
7. Runs CLIProxyAPI's own ChatGPT OAuth login if needed, starts the local proxy, and sends a small Anthropic Messages request to Astra.
8. When Paseo is detected, restarts the local Paseo daemon using that existing executable and reloads its configuration. Finish active Paseo sessions first, or pass `--skip-paseo-start` to activate it later.

Run the same command again to update this integration. It preserves the local API token and existing OAuth credentials. Rerunning briefly stops the managed proxy and restarts Paseo when detected, so finish active sessions first. Existing unrelated programs called `claude-codex`, `claude-codex-proxy`, or `paseo-codex` are never overwritten.

## Use from the terminal

```bash
claude-codex                         # Default: high reasoning
claude-codex --reasoning low
claude-codex --reasoning medium
claude-codex --reasoning high
claude-codex --reasoning xhigh
claude-codex --reasoning max
claude-codex --reasoning ultracode    # xhigh + Claude workflow orchestration
claude-codex --reasoning high -p 'Explain this project'
claude-codex --reasoning max --resume
```

Both the terminal launcher and Paseo use Astra's full **1,050,000-token context window** at every reasoning level. The launcher sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1050000`, overriding an inherited 200K value, and the generated Paseo provider declares the same window. Claude Code therefore budgets the session against Astra's documented window instead of its 200K fallback for unknown gateway model IDs. Automatic compaction remains enabled; usable input space also reserves room for output. See [Astra's model specification](https://developers.openai.com/api/docs/models/gpt-6-astra) and [Claude's gateway context configuration](https://code.claude.com/docs/en/model-config#correct-the-window-for-a-gateway-or-custom-model-id).

After updating an existing installation, restart terminal sessions or create a new Paseo agent to pick up the new environment. Setting the client window does not change the upstream account's model access or limits.

Set the persistent terminal default:

```bash
claude-codex-proxy reasoning xhigh
```

Or override it for one invocation:

```bash
CLAUDE_CODEX_REASONING=medium claude-codex
```

Claude Code arguments are forwarded, including print mode, session resume, permissions, hooks, and the Agent SDK's stream-JSON options. `--reasoning` is handled by the wrapper. You can also use `--model 'gpt-6-astra(max)'`. Conflicting model-suffix and `--reasoning` values are rejected.

The wrapper sends model names such as `gpt-6-astra(xhigh)`. CLIProxyAPI strips the suffix and sets OpenAI's `reasoning.effort`. Valid Astra levels are **low, medium, high, xhigh, max**. The proxy does not accept `none`, `minimal`, or `ultra` as Astra API reasoning values. Claude Code's separate Ultra Code workflow mode is available with `xhigh`, as described below.

Use the wrapper option or the model suffix to choose reasoning; Claude's `/effort` and thinking-budget controls do not override the suffix. Claude's Opus, Sonnet, Haiku, Fable, small/fast, and default subagent mappings all target Astra at the launch effort. A custom agent definition that explicitly hardcodes another model can still override its own model selection.

The proxy starts automatically on each launch if necessary, including after reboot. It remains running after a session exits. This is an on-demand background process, without a systemd/launchd service. If it crashes, the next launcher invocation starts it again; an already-running session may need a retry.

## Use with Paseo

After installation, select the **Claude Codex · GPT-6 Astra** provider when creating an agent. The **model picker** lists the five Astra reasoning levels plus **GPT-6 Astra · Ultra Code**. The provider uses `extends: "claude"` and the absolute path to `claude-codex`, so the daemon does not depend on your interactive shell's PATH.

The context circle uses the last completed main-agent request's measured input and output tokens, including cached input. For Paseo's stream-JSON connection, the launcher removes provisional input estimates and supplies final per-request usage to its adapter. Text still streams immediately; the meter updates when the turn completes. Totals from earlier turns and subagents are not added to the current context size. This compatibility adapter is enabled by the provider's `CLAUDE_CODEX_PASEO_USAGE` environment setting and does not modify the installed Paseo package.

The five API reasoning levels are encoded in model IDs. For consistent helper/subagent effort, choose the variant when creating the session; helpers retain their launch-time environment if you change the main model during a session.

### Ultra Code

Select **GPT-6 Astra · Ultra Code** directly in Paseo's **model picker**. This enables Claude Code's native workflow mode automatically, even if Paseo does not expose a separate thinking control. The proxy alias `gpt-6-astra-ultracode` routes to Astra with `xhigh` reasoning and the full 1,050,000-token context window. It is a launcher profile for the same Astra model.

The earlier **GPT-6 Astra · xhigh → Ultra Code** thinking option is retained for existing agents; **Standard** on that entry keeps ordinary xhigh reasoning.

Ultra Code is workflow orchestration at xhigh per-message reasoning. It is not an API effort above `max`, and it is different from Codex's own Ultra mode. Dynamic workflows must be available and enabled in your Claude Code installation; Claude's normal workflow permissions still apply. See [Claude Code's Ultra Code behavior](https://code.claude.com/docs/en/model-config#adjust-effort-level) and [Codex Max and Ultra](https://learn.chatgpt.com/docs/models#know-when-to-use-max-or-ultra).

The terminal equivalent uses Claude's native flag (requires Claude Code 2.1.203+):

```bash
claude-codex --reasoning ultracode
```

After updating an existing installation, reload Paseo and select **GPT-6 Astra · Ultra Code** in the model picker. If the app shows a cached list, reconnect to the host or reopen the app.

`paseo-codex` is a convenience command that invokes your selected Paseo executable with the configured `PASEO_HOME`. It helps when using an explicit executable path or a nondefault home:

```bash
paseo-codex provider diagnostic claude-codex
paseo-codex run --provider claude-codex 'Explain this project'
```

The regular Claude provider remains available. Install on each remote **daemon host**, not just on the phone or desktop UI; `127.0.0.1` refers to that host. The installed version must support custom providers that extend Claude and the documented daemon commands. This integration was tested with 0.8.0-beta.1; version management for the daemon and desktop/mobile UI remains yours.

## Installer options

```bash
# Choose the initial default.
bash install.sh --reasoning max

# Headless host: device-code login.
bash install.sh --device-login

# Print the OAuth URL without opening a browser.
bash install.sh --no-browser

# Different local port and Paseo home.
bash install.sh --port 18317 --paseo-home ~/.paseo-beta

# Choose existing executables explicitly.
bash install.sh --claude-bin /path/to/claude --paseo-bin /path/to/paseo

# Terminal-only installation.
bash install.sh --skip-paseo

# Stage files without login, requests, daemon startup, or shell edits.
bash install.sh --skip-login --skip-paseo-start --no-path

bash install.sh --help
```

`--skip-smoke-test` skips the subscription-consuming verification request. `--skip-paseo-start` writes the provider configuration and leaves daemon activation to you; subsequently run `paseo-codex daemon restart` and `paseo-codex reload`.

`--proxy-version X.Y.Z` selects another release and verifies its published checksum. `--proxy-binary /path/to/cli-proxy-api` uses an existing trusted binary without downloading or verifying it; use 7.2.155 or a compatible version with Astra and `max` support.

You can override `--bin-dir`, `--config-dir`, `--data-dir`, and `--state-dir`. The last three default to XDG directories when the corresponding XDG variables are set. Reuse the same directory options on later installer runs.

## Authentication and diagnostics

```bash
claude-codex-proxy status
claude-codex-proxy doctor               # Local catalog/auth-file checks
claude-codex-proxy doctor --smoke-test  # Small live Astra request
claude-codex-proxy login                # Login/reauthenticate with ChatGPT
claude-codex-proxy login --device
claude-codex-proxy restart
claude-codex-proxy stop
claude-codex-proxy paths
```

The local API token authenticates Claude to CLIProxyAPI. The separate OAuth credentials authenticate CLIProxyAPI to ChatGPT. An existing `codex login` is not imported: CLIProxyAPI owns its own refresh-token lifecycle.

The local listener binds to `127.0.0.1`, requires the generated API token, and disables the management API/UI. The Paseo config contains a placeholder gateway token; the launcher replaces it with the private token before executing Claude. Proxy startup messages go to a log file and wrapper errors go to stderr, preserving stdout for Paseo's protocol.

Default files:

| Path | Contents |
| --- | --- |
| `~/.config/claude-codex/settings.json` | Launcher settings and local API token |
| `~/.config/claude-codex/proxy.yaml` | Generated CLIProxyAPI configuration (JSON syntax, valid YAML) |
| `~/.config/claude-codex/auth/` | ChatGPT OAuth credentials |
| `~/.config/claude-codex/claude/` | Isolated Claude user profile and sessions |
| `~/.local/share/claude-codex/` | Runtime, versioned proxy binary, optional private npm CLIs |
| `~/.local/state/claude-codex/` | PID, lock, startup log, rotating CLIProxyAPI logs |
| `~/.paseo/config.json` | Merged provider entry; timestamped backup beside it |

Keep the config/auth directory private. It is outside this repository and should not be committed.

Troubleshooting:

- **Port occupied:** rerun with `--port 18317` or another unused port. The installer does not kill unrelated listeners.
- **Login error on a headless machine:** try `--device-login`. Device login must be allowed by your ChatGPT account/workspace. For browser OAuth over SSH, forward the callback port shown by CLIProxyAPI (normally 1455).
- **401 / missing login:** run `claude-codex-proxy login`.
- **Astra unavailable / 403 / quota error:** check access on the signed-in account and run `doctor --smoke-test`. The proxy catalog alone does not prove entitlement, and changing the model name cannot add subscription access.
- **Paseo provider absent:** use `paseo-codex reload`, confirm app/daemon version and `PASEO_HOME`, then run the provider diagnostic above. If Paseo reports that a restart is required, restart its daemon after finishing active sessions.
- **Proxy startup failure:** inspect `~/.local/state/claude-codex/proxy-startup.log` and the `logs/` directory there. Avoid sharing credentials from config files.
- **Unexpected routing:** inspect project `.claude/settings.json`, `.claude/settings.local.json`, and managed settings for conflicting model/provider environment variables.

This is a third-party compatibility bridge. Claude's server-side `WebSearch` is disabled because it is not supported by this integration; use a separately configured search tool if needed. Existing user-level Claude plugins, credentials, and settings are not copied into the isolated profile. Project `CLAUDE.md` and project settings load normally. Native Claude/Codex features are not all interchangeable through an API translator.

## Remove

Stop active proxied sessions, run `claude-codex-proxy stop`, remove only the `agents.providers.claude-codex` entry from your Paseo config, and reload Paseo. Delete the three installed launchers and the directories listed by `claude-codex-proxy paths` when you no longer need their credentials or session history. Remove the installer-marked PATH lines from shell startup files if desired. Do not restore an old whole-file Paseo backup over newer unrelated edits.

## Development and verification

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary directories and mocks; they do not read your OAuth credentials or change your existing Claude/Paseo settings. To include real CLIProxyAPI translation tests against a **local fake upstream**:

```bash
CLIPROXYAPI_TEST_BINARY=/absolute/path/to/cli-proxy-api \
  python3 -m unittest discover -s tests -v
```

To also exercise the real Claude harness and an isolated Paseo beta daemon:

```bash
CLIPROXYAPI_TEST_BINARY=/absolute/path/to/cli-proxy-api \
CLAUDE_TEST_BINARY=/absolute/path/to/claude \
PASEO_TEST_BINARY=/absolute/path/to/paseo \
  python3 -m unittest discover -s tests -v
```

The optional tests verify Anthropic streaming, reasoning translation, tool execution, and a complete Paseo session against a local fake Codex backend. The Paseo test starts/stops its own daemon using temporary configuration; the existing daemon is unaffected.

Development validation used Linux ARM64, CLIProxyAPI 7.2.155, Claude Code 2.1.266, and Paseo 0.8.0-beta.1. The context-meter regression is also tested with the installed Paseo version. macOS has not been exercised. Live ChatGPT OAuth and subscription entitlement remain account-dependent; the installer's smoke test checks that path after you sign in.

## Upstream references

- [GPT-6 Astra model and supported reasoning levels](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [OpenAI subscription authentication](https://learn.chatgpt.com/docs/auth)
- [CLIProxyAPI 7.2.155 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.155)
- [CLIProxyAPI Codex OAuth login](https://help.router-for.me/configuration/provider/codex)
- [CLIProxyAPI reasoning suffixes](https://help.router-for.me/configuration/thinking) and [pinned Astra model registry](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.155/internal/registry/models/models.json)
- [Claude gateway configuration](https://code.claude.com/docs/en/llm-gateway) and [model configuration](https://code.claude.com/docs/en/model-config)
- [Paseo 0.8.0-beta.1 custom provider schema](https://github.com/getpaseo/paseo/blob/v0.8.0-beta.1/docs/custom-providers.md) and [Claude adapter](https://github.com/getpaseo/paseo/blob/v0.8.0-beta.1/packages/server/src/server/agent/providers/claude/agent.ts)
