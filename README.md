# Claude Codex

Run the **Claude Code harness with GPT-6 Astra**, using your ChatGPT subscription through CLIProxyAPI's Codex OAuth provider. A separate `claude-codex` command configures the proxy connection and reasoning level. The original `claude` executable and its user configuration stay in place.

Optionally adds a separate **Claude Codex · GPT-6 Astra** provider to an existing Paseo installation, with a provider-scoped compatibility patch for live context usage, forked-skill subagent tracking, and a permission-mode catalog without Claude's auto mode. Paseo is not required for terminal use. No particular Paseo version is assumed; the installer uses whichever Paseo executable you have.

```text
Terminal: claude-codex ──────────┐
                               ├─ Claude Code → local CLIProxyAPI → Codex OAuth → GPT-6 Astra
Paseo: Claude Codex provider ────┘
```

## What this project provides

This repository contains a **Python standard-library installer and launcher**, not a fork of Claude Code, Paseo, or CLIProxyAPI. Claude Code supplies the terminal/agent harness; CLIProxyAPI translates its Anthropic-format requests into Codex requests authenticated with ChatGPT OAuth.

- **`claude-codex`** launches Claude Code with Astra model mappings and the selected reasoning level, using an isolated user profile for terminal sessions. It starts the managed local proxy when needed.
- **`claude-codex-proxy`** manages proxy startup/shutdown, login, diagnostics, and the persistent terminal reasoning default.
- **`paseo-codex`**, installed only when Paseo is detected, invokes your existing Paseo executable with the configured home. The generated provider uses `claude-codex` and a stream-JSON usage adapter for Paseo's context meter.

The integration does not grant model access, bypass subscription limits, or make every native Claude feature available through the translated API. See the compatibility notes below.

## Install with one command

From a checkout of this repository, run this on the computer hosting Claude Code and, if used, the Paseo daemon:

```bash
bash install.sh
```

Complete the ChatGPT sign-in when prompted. The script configures the proxy and, when detected, Paseo, including a small live request to verify Astra access. If your bin directory was added to `PATH`, open a new terminal after installation or use the absolute command printed by the installer.

Requirements:

- Linux (including WSL2) or macOS, on x86_64 or ARM64.
- Python 3.9+, plus `curl` to download CLIProxyAPI (`curl` is not required with `--proxy-binary`).
- Existing Claude Code, or Node.js **22+** and npm to install it privately.
- Paseo is optional: setup runs only when an executable is found on PATH or supplied with `--paseo-bin`. The installer reuses that exact executable and does not manage Paseo releases. The Paseo compatibility patch requires Node.js and a user-writable npm-installed Paseo package with a recognized Claude provider module; the installer applies the scoped patch described below. The Paseo desktop app's bundled `paseo` command (for example the symlink into `Paseo.app` on macOS) packs that module inside its `app.asar` archive and is rejected; install `@getpaseo/cli` at the app's version into a private prefix (`npm install -g --prefix ~/.local/share/paseo-cli @getpaseo/cli@$(paseo --version)`) and pass `--paseo-bin ~/.local/share/paseo-cli/bin/paseo` once; later plain `./install.sh` runs reuse that selection. The desktop app attaches to the daemon that CLI starts and leaves it running across app quits; after upgrading the app, upgrade the npm package to the same version and rerun the installer.
- A ChatGPT account with subscription-based Codex access **and access to `gpt-6-astra`**.

Model access and usage limits come from the signed-in ChatGPT account, not a proxy setting. A subscription's plan label alone does not establish Astra entitlement. By default, the installer checks an actual request and reports access/quota failures. This integration does not require an OpenAI API key or configure pay-as-you-go API billing.

### What the installer does

1. Reuses your Claude binary. If missing, installs `@anthropic-ai/claude-code@2.1.246` into a private directory.
2. Selects the executable supplied with `--paseo-bin`, or the existing `paseo` on PATH. When PATH resolves only to the desktop app bundle, or to nothing, the executable selected by the previous install is reused if it still exists. **If none is available, skips all Paseo configuration and daemon operations.** No Paseo package is downloaded or version enforced; cached private copies from older installer runs are not selected automatically. `paseo-codex` invokes the detected executable.
3. Downloads **CLIProxyAPI 7.2.155** and verifies a pinned SHA-256 checksum. Linux uses the static build without plugin support.
4. Creates an isolated configuration, OAuth directory, random local API token, and Claude profile with private file permissions.
5. Installs `claude-codex`, `claude-codex-proxy`, and (when Paseo is detected) `paseo-codex` into `~/.local/bin`, then adds that directory to your shell's startup configuration.
6. When Paseo is detected, validates and backs up its Claude provider module, then applies the Claude-Codex-only compatibility patch: live context usage, forked-skill subagent tracking, and a mode catalog without auto mode. Also backs up and merges the new provider into `$PASEO_HOME/config.json` (default `~/.paseo/config.json`). Other providers and settings are preserved; unfamiliar or unwritable package layouts fail clearly rather than being patched blindly.
7. Runs CLIProxyAPI's own ChatGPT OAuth login if needed, starts the local proxy, and sends a small Anthropic Messages request to Astra.
8. When Paseo is detected, restarts the local Paseo daemon using that existing executable and reloads its configuration. Finish active Paseo sessions first, or pass `--skip-paseo-start` to activate it later.

After updating the repository, run `./install.sh` again to copy the updated runtime, regenerate the installed launchers, and apply or verify the Paseo compatibility patch; editing this checkout alone does not update an existing installation. A Paseo package carrying the earlier usage-only patch is upgraded in place from its recovered upstream source. Reuse any custom directory options. The installer preserves the local API token and existing OAuth credentials. Rerunning stops the managed proxy and restarts Paseo when detected, so finish active sessions first. Existing unrelated programs called `claude-codex`, `claude-codex-proxy`, or `paseo-codex` are never overwritten. Explicit executable paths must point to the original programs, not these generated wrappers; self-referencing Claude and Paseo selections are rejected, including symlink aliases to the destination wrappers.

Installers targeting the same configuration directory are serialized with `install.lock`. A second run waits until the first finishes or is interrupted, including any login and daemon-restart steps, so concurrent runs cannot mix their saved settings and proxy configuration. The usage-reader patch also has a package-local lock shared by installations using the same Paseo package. These locks are not crash rollback or general coordination between separate installations sharing other output directories. Do not delete a lock file to bypass a running installer.

For Bash, PATH setup updates `.bashrc` and the first existing login file in this order: `.bash_profile`, `.bash_login`, `.profile` (creating `.profile` if none exists). Zsh uses `.zshrc` and `.zprofile`; Fish uses a `conf.d` snippet. Existing Bash/Zsh dotfile symlinks are preserved, and repeated installation does not duplicate an unchanged PATH block.

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

The wrapper respects the operand boundaries of supported native options: a value passed to `--append-system-prompt`, for example, remains literal even if it looks like `--model=...` or `--reasoning=...`. Bare `--resume`, boolean `-p`, empty values, and variadic SDK options remain supported. Use a standalone `--` before prompt text that should not be interpreted as options:

```bash
claude-codex -p -- '--reasoning is literal prompt text here'
```

Unknown SDK flags are forwarded, but their operand counts cannot be inferred automatically. Newly introduced native options with separate values may require updating the option table in `scripts/claude_codex.py`; `--option=value` avoids that ambiguity when supported by the native option.

The wrapper sends model names such as `gpt-6-astra(xhigh)`. CLIProxyAPI strips the suffix and sets OpenAI's `reasoning.effort`. Valid Astra levels are **low, medium, high, xhigh, max**. The proxy does not accept `none`, `minimal`, or `ultra` as Astra API reasoning values. Claude Code's separate Ultra Code workflow mode is available with `xhigh`, as described below.

Use the wrapper option or the model suffix to choose reasoning; Claude's `/effort` and thinking-budget controls do not override the suffix. Claude's Opus, Sonnet, Haiku, Fable, small/fast, and default subagent mappings all target Astra at the launch effort. A custom agent definition that explicitly hardcodes another model can still override its own model selection.

### Permission modes

Claude Code's **auto mode** judges every tool call with Anthropic's safety classifier models. CLIProxyAPI cannot serve those from a Codex subscription (it answers Claude model names with HTTP 400), and Claude Code then runs the classifier prompt on the session model instead: Astra, at the session's reasoning effort. Each tool call gains a full-transcript classifier request, the verdicts come from a model the prompt was not written for, and in auto mode a denial is final, with no approval prompt to fall back to. Observed results included ordinary `git commit` and `gh gist create` calls being refused.

The launcher therefore passes Claude's own `permissions.disableAutoMode` setting on every launch, merged into any `--settings` operand the caller already supplies. Claude starts in its normal prompting mode (`default`), the mode carousel does not offer auto, and no classifier request reaches the gateway. Choose `acceptEdits` or `bypassPermissions` when you want fewer prompts. To keep auto mode anyway, accepting that Astra judges its own actions, set `CLAUDE_CODEX_AUTO_MODE=1` in the environment; the launcher then leaves the settings untouched.

The proxy starts automatically on each launch if necessary, including after reboot. It remains running after a session exits. This is an on-demand background process, without a systemd/launchd service. If it crashes, the next launcher invocation starts it again; an already-running session may need a retry.

## Use with Paseo

After installation, select the **Claude Codex · GPT-6 Astra** provider when creating an agent. The **model picker** lists the five Astra reasoning levels plus **GPT-6 Astra · Ultra Code**. The provider uses `extends: "claude"` and the absolute path to `claude-codex`, so the daemon does not depend on your interactive shell's PATH.

### Permission modes in Paseo

The compatibility patch removes **Auto mode** from the mode catalog of this provider and makes **Always Ask** the default for new agents, matching what Claude actually runs (see "Permission modes" above). Permission prompts appear in the app as usual; **Accept File Edits** and **Bypass** remain available. An existing agent or agent profile that still names `auto` is started by Claude in `default` mode, and the agent's mode display follows Claude's report. To keep auto mode for this provider, add `"CLAUDE_CODEX_AUTO_MODE": "1"` to the provider's `env` in `config.json` and reload Paseo; the terminal launcher and the patch honor the same variable. Paseo's regular Claude provider keeps its own catalog.

### Subagents from forked skills

Claude Code runs a skill declared with `context: fork` inside a subagent without announcing it through its task protocol: the child's frames carry the Skill call id as their parent, and only the Skill tool result ends it. Paseo's Claude provider learned subagents either from that protocol or from a frame-derived fallback, and a forked skill fell between the two once any announced task appeared. The fork stayed "running" forever, the background agents it launched were listed flat with never-completing task cards in the parent transcript, and a daemon restart dropped the whole subtree. Astra's review workflows, which fork a skill and fan out background agents, hit this every time.

For the Claude Codex provider the patch declares a forked skill from its Skill call, nests the agents it launches under it, finishes it on the Skill result, and links the same rows on replay from the transcripts on disk. A child announced as already backgrounded is also recognized as such, so interrupting a turn no longer marks it canceled. Paseo's regular Claude provider is unchanged.

The context circle uses the last completed main-agent request's measured input and output tokens, including cached input, against the **1,050,000-token** window. It updates **between tool calls during a running turn**, not just when the agent finishes the entire turn. Before the first measured response arrives, usage is unknown; while the next request is running, the previous measurement stays visible. Text and tools still stream immediately. Totals from earlier turns and subagents are not added to the current context size.

The launcher removes provisional input estimates. The installer also applies a compatibility patch to the selected Paseo package's Claude provider module (`agent.js`) so it accepts late input/cache counts and knows the configured window before the first turn ends, tracks forked-skill subagents, and offers the mode catalog described above. The added behavior is enabled only by the Claude Codex provider's `CLAUDE_CODEX_PASEO_USAGE` environment setting, which the installer writes into the provider entry; the regular Claude provider keeps its original behavior. The original module is backed up alongside it as `agent.js.claude-codex-backup-*`, its file permissions are preserved, and unchanged reinstallations do not duplicate the patch or backup. Source-structure and JavaScript syntax checks reject unsupported or modified modules before applying changes, and the sibling task protocol module is read to confirm the contract the patch extends; no Paseo version number is used to decide compatibility.

To repair an existing installation, finish active work and rerun `./install.sh` **on the daemon host** (with the same custom directory options, if any). The normal install restarts and reloads that daemon; resume or start an agent afterward to load the updated code. Reinstalling or updating Paseo can replace the patched module, so rerun this installer afterward as well. With `--skip-paseo-start`, explicitly run `paseo-codex daemon restart` and `paseo-codex reload` when ready. Restoring the module's backup, or reinstalling Paseo, removes the patch; restart the daemon after doing so.

### Conversation history in Paseo

Paseo reloads an agent's transcript from the Claude profile **its daemon** resolves: the daemon's own `CLAUDE_CONFIG_DIR`, or `~/.claude`. It does not consult the environment of the provider it spawns. Paseo sessions therefore run in that profile rather than in the launcher's isolated profile, so the conversation survives daemon restarts, reloads, and reopening the app. The isolated profile remains in use for terminal launches only.

Earlier versions recorded Paseo sessions in the isolated profile, which made a conversation appear empty whenever Paseo reloaded it from disk. After updating, rerun the installer so the provider entry no longer pins the isolated profile, then reload Paseo. When Paseo resumes an agent created before the update, the launcher moves that session's transcript into the daemon's profile; Claude continues the conversation immediately, and Paseo shows the earlier history after its next reload or daemon restart. The user-level settings, hooks, and plugins of the daemon's profile apply to Paseo sessions, the same as for Paseo's regular Claude provider.

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

`paseo-codex` is a convenience command that invokes your selected Paseo executable with the configured `PASEO_HOME`. When Node was detected during installation, the wrapper prepends that Node directory to the caller's **current PATH**; it does not freeze the entire installation-time PATH. Newly added tool directories and activated environments remain available, though the saved Node directory takes precedence. An already-running daemon keeps its existing environment until restarted. The wrapper helps when using an explicit executable path or a nondefault home:

```bash
paseo-codex provider diagnostic claude-codex
paseo-codex run --provider claude-codex 'Explain this project'
```

The regular Claude provider remains available. Install on each remote **daemon host**, not just on the phone or desktop UI; `127.0.0.1` refers to that host. The installed version must support custom providers that extend Claude and the documented daemon commands. Version management for the daemon and desktop/mobile UI remains yours.

## Installer options

```bash
# Choose the initial default.
bash install.sh --reasoning max

# Headless host: device-code login.
bash install.sh --device-login

# Print the OAuth URL without opening a browser.
bash install.sh --no-browser

# Different local port and Paseo home.
bash install.sh --port 18317 --paseo-home ~/.paseo-work

# Choose existing executables explicitly.
bash install.sh --claude-bin /path/to/claude --paseo-bin /path/to/paseo

# Terminal-only installation.
bash install.sh --skip-paseo

# Stage files without login, live model requests, Paseo startup, or shell edits.
bash install.sh --skip-login --skip-paseo-start --no-path

bash install.sh --help
```

`--skip-smoke-test` skips the subscription-consuming verification request. `--skip-paseo-start` stages the provider configuration and usage-reader patch but leaves daemon activation to you; subsequently run `paseo-codex daemon restart` and `paseo-codex reload`.

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

Explicit `login` stops the managed proxy, runs CLIProxyAPI's browser or device login, and requires a newly created or updated valid Codex credential file. An unchanged old credential no longer makes a failed login look successful when CLIProxyAPI exits with status zero. The wrapper checks file contents and identity/modification time, so successful rewrites of identical credentials are accepted; it does not delete old credentials before retrying. This checks that credentials were saved, not which account will be selected among multiple credentials. A successful explicit login is followed by a live smoke-test request using subscription quota; ordinary `doctor` checks credentials and the local model catalog, not account entitlement.

The local listener binds to `127.0.0.1`, requires the generated API token, and disables the management API/UI. The Paseo config contains a placeholder gateway token; the launcher replaces it with the private token before executing Claude. Proxy startup messages go to a log file and wrapper errors go to stderr, preserving stdout for Paseo's protocol.

Default files:

| Path | Contents |
| --- | --- |
| `~/.config/claude-codex/settings.json` | Launcher settings and local API token |
| `~/.config/claude-codex/proxy.yaml` | Generated CLIProxyAPI configuration (JSON syntax, valid YAML) |
| `~/.config/claude-codex/install.lock` | Persistent lock file used to serialize installers for this configuration |
| `~/.config/claude-codex/auth/` | ChatGPT OAuth credentials |
| `~/.config/claude-codex/claude/` | Isolated Claude user profile and sessions for terminal launches |
| `~/.local/share/claude-codex/` | Runtime, versioned proxy binary, optional private npm CLIs |
| `~/.local/state/claude-codex/` | PID, lock, startup log, rotating CLIProxyAPI logs |
| `~/.paseo/config.json` | Merged provider entry; timestamped backup beside it |

Keep the config/auth directory private. It is outside this repository and should not be committed.

Troubleshooting:

- **Port occupied:** rerun with `--port 18317` or another unused port. The installer does not kill unrelated listeners.
- **Login error on a headless machine:** try `--device-login`. Device login must be allowed by your ChatGPT account/workspace. For browser OAuth over SSH, forward the callback port shown by CLIProxyAPI (normally 1455).
- **401 / missing login:** run `claude-codex-proxy login`.
- **No new or updated OAuth credentials:** the login attempt did not save a valid new or changed Codex credential. Check CLIProxyAPI's login output and retry; existing credentials alone do not establish success.
- **Installer appears to wait:** another installer may hold the same configuration's `install.lock`, including while waiting for OAuth. Finish or interrupt that run rather than deleting the lock file.
- **Executable rejected as recursive:** point `--claude-bin` or `--paseo-bin` at the original executable, not an installed integration wrapper or a symlink to it.
- **Astra unavailable / 403 / quota error:** check access on the signed-in account and run `doctor --smoke-test`. The proxy catalog alone does not prove entitlement, and changing the model name cannot add subscription access.
- **Paseo provider absent:** use `paseo-codex reload`, confirm app/daemon version and `PASEO_HOME`, then run the provider diagnostic above. If Paseo reports that a restart is required, restart its daemon after finishing active sessions.
- **Paseo lists a subagent as working long after the agent went idle:** the daemon is running an unpatched or older-patched Claude provider module. Rerun the installer on the daemon host and restart the daemon; rows created before the fix are rebuilt from the transcripts on the next reload.
- **A tool call was "denied by the Claude Code auto mode classifier":** the session predates this launcher's auto-mode setting or runs with `CLAUDE_CODEX_AUTO_MODE=1`. Start a new agent, or switch the existing one to another permission mode; see "Permission modes".
- **Paseo shows an empty conversation for an existing agent:** the transcript is not in the profile the daemon reads. Rerun the installer and reload Paseo; see "Conversation history in Paseo" above. Sending one message to the agent moves an older transcript across, and the history returns after the next reload.
- **Proxy startup failure:** inspect `~/.local/state/claude-codex/proxy-startup.log` and the `logs/` directory there. Avoid sharing credentials from config files.
- **Unexpected routing:** inspect project `.claude/settings.json`, `.claude/settings.local.json`, and managed settings for conflicting model/provider environment variables.

This is a third-party compatibility bridge. Claude's server-side `WebSearch` is disabled because it is not supported by this integration; use a separately configured search tool if needed. Auto mode is disabled because its classifier cannot run on this gateway (see "Permission modes"). Existing user-level Claude plugins, credentials, and settings are not copied into the terminal launcher's isolated profile. Project `CLAUDE.md` and project settings load normally. Native Claude/Codex features are not all interchangeable through an API translator.

## Remove

1. Finish active proxied sessions and installer runs, then run `claude-codex-proxy paths` to inspect this installation's locations and `claude-codex-proxy stop` to stop its proxy.
2. If Paseo was configured, remove only `agents.providers.claude-codex` from its config and reload Paseo. Do not restore an old whole-file backup over newer unrelated edits. The compatibility patch is inactive without the provider marker; to remove it too, reinstall Paseo using your usual package manager and restart its daemon.
3. Remove the generated `claude-codex` and `claude-codex-proxy` launchers, plus `paseo-codex` if installed. **Do not delete the bin directory itself**, your original Claude/Paseo executables, or the whole Paseo home.
4. After reviewing the reported paths, remove only this integration's `config_dir`, `data_dir`, and `state_dir` when you no longer need the credentials or terminal session history they contain. This also removes any private Claude installation inside the integration's data directory. Paseo session transcripts live in the daemon's Claude profile and are not affected.
5. Remove installer-marked PATH entries from shell startup files if desired, or the generated Fish `conf.d/claude-codex.fish` snippet. Keep shared PATH entries that you still need for other programs.

## Development and verification

```bash
python3 -m unittest discover -s tests -v
```

Without integration environment variables, the real-binary tests are skipped. Node.js is used for offline JavaScript compatibility and installer checks; those tests skip when Node is unavailable. Offline coverage includes:

- Model/reasoning normalization and native argument-value boundaries.
- The launcher's auto-mode setting: appended, merged into a caller's `--settings` JSON or file without replacing it, kept out of the literal prompt tail, and skipped with `CLAUDE_CODEX_AUTO_MODE=1`.
- Paseo launches keeping the daemon's Claude profile, and moving a resumed session's transcript out of the isolated profile exactly once.
- Login success/failure using temporary fake credentials, including stale credentials and same-content rewrites.
- Generated shell launchers, runtime PATH, Bash startup-file precedence, and recursion guards.
- Coordinated installer subprocesses verifying serialization, retained settings, and lock release on failure.
- Download/checksum and archive-handling fixtures, provider configuration merging, and the stream-JSON usage adapter.
- Live-usage cache accounting, request/compaction resets, malformed data, model switches, and unchanged native-provider behavior.
- Forked-skill subagents against verbatim copies of Paseo's subagent modules: declaration from the Skill call, nesting of background children, no duplicate task card, completion and failure from the Skill result, nested forks, backgrounded-at-start children, replay facts linking the fork's transcript, and unchanged events for unmarked sessions.
- The mode catalog and a running session's advertised modes with and without the provider marker and the override.
- Patch source guards for every edited anchor, the read-only task protocol contract check, syntax validation, atomic backups, package locking, in-place upgrade of the earlier usage-only patch, and installation/reapplication without losing credentials or unrelated configuration.

Tests use temporary directories, mocks, and local subprocesses; they do not read your OAuth credentials or change your existing Claude/Paseo settings. To include real CLIProxyAPI translation tests against a **local fake upstream**:

```bash
CLIPROXYAPI_TEST_BINARY=/absolute/path/to/cli-proxy-api \
  python3 -m unittest discover -s tests -v
```

To also exercise the real Claude harness and an isolated Paseo daemon:

```bash
CLIPROXYAPI_TEST_BINARY=/absolute/path/to/cli-proxy-api \
CLAUDE_TEST_BINARY=/absolute/path/to/claude \
PASEO_TEST_BINARY=/absolute/path/to/paseo \
  python3 -m unittest discover -s tests -v
```

The optional tests verify Anthropic streaming, reasoning translation, option-like system-prompt values reaching the upstream unchanged, tool execution, proxy lifecycle behavior, and complete Paseo sessions including Ultra Code and transcript persistence. A gated **Read → Read → text** regression inspects the actual daemon's context data while the first turn is still running, before any final result; it also checks that subsequent usage replaces earlier counts. Usage arrives only at response completion in the fake upstream. A real-Claude test shows the auto-mode classifier reaching the gateway as Astra requests without the launcher setting, and a session starting in `default` mode with no classifier traffic with it. A daemon test runs a forked skill that launches a background agent and checks, live and after a daemon restart, that both rows finish, nest, and leave no dangling task card; another checks that new agents default to Always Ask, that auto mode is absent from the advertised modes, and that an agent requesting auto mode runs in `default`.

The Paseo tests copy the selected CLI/server package into temporary directories, install twice into that private copy, and start/stop their own daemon using temporary configuration and a temporary Claude profile. Client connections explicitly target their loopback test endpoint, with no fallback to your normal daemon. The existing installed package, daemon, and `~/.claude` are unaffected. Allow temporary disk space for a copy of your Paseo package and dependencies. No real Codex backend or subscription quota is used by these tests.

All **118 tests passed**, with no skips, in development validation on Linux with CLIProxyAPI 7.2.155, Claude Code 2.1.266, and a private copy of the locally installed Paseo; the daemon tests upgraded that copy's earlier usage-only patch in place. macOS and live ChatGPT OAuth were not exercised by this validation; login failures are simulated in offline tests. Model entitlement remains account-dependent, and the installer's live smoke test checks that path after you sign in.

## Upstream references

- [GPT-6 Astra model and supported reasoning levels](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [OpenAI subscription authentication](https://learn.chatgpt.com/docs/auth)
- [CLIProxyAPI 7.2.155 release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.155)
- [CLIProxyAPI Codex OAuth login](https://help.router-for.me/configuration/provider/codex)
- [CLIProxyAPI reasoning suffixes](https://help.router-for.me/configuration/thinking) and [pinned Astra model registry](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.155/internal/registry/models/models.json)
- [Claude gateway configuration](https://code.claude.com/docs/en/llm-gateway) and [model configuration](https://code.claude.com/docs/en/model-config)
- [Paseo custom provider schema](https://github.com/getpaseo/paseo/blob/main/docs/custom-providers.md) and [Claude adapter](https://github.com/getpaseo/paseo/blob/main/packages/server/src/server/agent/providers/claude/agent.ts)
