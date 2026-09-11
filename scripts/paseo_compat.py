"""Install a provider-scoped live-usage adapter into the selected Paseo package."""

import os
from pathlib import Path
import shutil
import subprocess

from claude_codex import SetupError, atomic_write, file_lock, read_json, say


AGENT_PATH = Path("dist/server/server/agent/providers/claude/agent.js")
PATCH_MARKER = "// Managed by claude-codex: live context usage"
BASE_CLASS = "class ClaudeContextUsageState {"
RENAMED_CLASS = "class ClaudeCodexBaseContextUsageState {"
SESSION_CLASS = "class ClaudeAgentSession {"
INITIALIZER = "new ClaudeContextUsageState(findClaudeModel(this.config.model)?.contextWindowMaxTokens)"
# Provider settings and per-launch overrides are distinct in Paseo. Do not use
# process.env: a daemon started from Claude Codex must not mark native providers.
PATCHED_INITIALIZER = INITIALIZER[:-1] + ", { ...this.runtimeSettings?.env, ...this.launchEnv })"


def patch_source(source):
    adapter = Path(__file__).with_name("paseo_usage.js").read_text() + "\n"
    if PATCH_MARKER in source:
        # Recognize our entire patch, not just its marker. A partial or locally
        # edited patch must not be mistaken for a working installation.
        if (source.count(adapter) != 1 or source.count(RENAMED_CLASS) != 1
                or source.count(PATCHED_INITIALIZER) != 1):
            raise SetupError("Paseo's claude-codex usage patch is incomplete or modified; "
                             "restore its backup or reinstall Paseo, then rerun ./install.sh")
        original = source.replace(adapter, "", 1).replace(RENAMED_CLASS, BASE_CLASS, 1)
        original = original.replace(PATCHED_INITIALIZER, INITIALIZER, 1)
        if patch_source(original) != source:
            raise SetupError("Paseo's claude-codex usage patch has an unsupported structure")
        return source
    if RENAMED_CLASS in source or PATCHED_INITIALIZER in source:
        raise SetupError("Paseo has a partial claude-codex usage patch; restore the original and rerun ./install.sh")
    # These are the consumer contracts the subclass relies on, not a package
    # version check. Leave unfamiliar source untouched rather than guessing.
    anchors = (
        BASE_CLASS, SESSION_CLASS, INITIALIZER, "function toObjectRecord(value) {",
        "constructor(initialContextWindowMaxTokens) {",
        "setInitialContextWindowMaxTokens(contextWindowMaxTokens) {",
        "recordModelUsage(modelUsage) {", "buildStreamUsageEvent(event) {",
        "createUsageUpdatedEvent(contextWindowUsedTokens) {",
        "buildCompactionUsageEvent(postTokens) {",
        "this.streamRequestInputTokens = inputTokens;",
        "this.streamRequestOutputTokens = outputTokens;",
        "this.contextUsage.setInitialContextWindowMaxTokens(findClaudeModel(this.config.model)?.contextWindowMaxTokens);",
        "this.streamUsedTokens() ?? activeResultUsageTokens ?? this.compactedContextWindowUsedTokens",
    )
    supported = all(source.count(anchor) == 1 for anchor in anchors)
    if supported:
        setup = source[source.index(SESSION_CLASS):source.index(INITIALIZER)]
        supported = all(setup.count(anchor) == 1 for anchor in (
            "this.runtimeSettings = options.runtimeSettings;", "this.launchEnv = options.launchEnv;"))
    if not supported:
        raise SetupError("Unsupported Paseo Claude usage reader; no patch was applied. "
                         "Update this checkout for the installed Paseo layout, or use --skip-paseo for terminal-only setup")
    return (source.replace(BASE_CLASS, RENAMED_CLASS, 1)
            .replace(SESSION_CLASS, adapter + SESSION_CLASS, 1)
            .replace(INITIALIZER, PATCHED_INITIALIZER, 1))


def find_usage_reader(paseo_bin):
    entry = Path(paseo_bin).resolve()
    cli_root = None
    for directory in entry.parents:
        manifest = directory / "package.json"
        if manifest.is_file() and read_json(manifest).get("name") == "@getpaseo/cli":
            cli_root = directory
            break
    if cli_root is not None:
        # Match Node's package lookup from this CLI, including hoisted npm and
        # symlinked package-manager layouts. Never choose a different PATH CLI.
        for directory in (cli_root, *cli_root.parents):
            package = directory / "node_modules" / "@getpaseo" / "server"
            if not package.exists():
                continue
            manifest = package / "package.json"
            if manifest.is_file() and read_json(manifest).get("name") == "@getpaseo/server":
                target = package / AGENT_PATH
                if target.is_file():
                    return target.resolve()
            break
    raise SetupError(f"Cannot locate the Paseo Claude usage reader for {paseo_bin}. "
                     "Use --paseo-bin with the original npm-installed Paseo executable, "
                     "or --skip-paseo for terminal-only setup; no package was modified")


def check_syntax(source):
    node = shutil.which("node")
    if not node:
        raise SetupError("Node.js is required to validate the Paseo compatibility patch")
    env = dict(os.environ)
    env.pop("NODE_OPTIONS", None)  # Syntax validation must not execute preload hooks.
    try:
        result = subprocess.run([node, "--input-type=module", "--check"], input=source,
                                capture_output=True, text=True, env=env, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise SetupError("Paseo compatibility syntax check timed out; no patch was applied") from exc
    if result.returncode:
        raise SetupError(f"Paseo compatibility syntax check failed; no patch was applied: {result.stderr.strip()}")


def prepare_patch(paseo_bin):
    path = find_usage_reader(paseo_bin)
    try:
        source = path.read_text()
        patched = patch_source(source)
        check_syntax(patched)
        if patched != source and not os.access(path.parent, os.W_OK):
            raise PermissionError(f"package directory is not writable: {path.parent}")
    except OSError as exc:
        raise SetupError(f"Cannot prepare Paseo usage compatibility: {exc}. "
                         "Use a user-writable Paseo installation and rerun ./install.sh") from exc
    return path


def apply_patch(path, backup):
    path = Path(path)
    try:
        # Share a lock across installations with different config directories
        # that use the same Paseo package. Re-read under the lock before editing.
        with file_lock(path.with_name(".claude-codex-usage.lock")):
            source = path.read_text()
            patched = patch_source(source)
            check_syntax(patched)
            if patched == source:
                say(f"Paseo live context usage already configured: {path}")
                return
            mode = path.stat().st_mode & 0o777
            backup(path)
            atomic_write(path, patched, mode=mode)
        say(f"Configured Paseo live context usage: {path}")
    except OSError as exc:
        raise SetupError(f"Cannot apply Paseo usage compatibility to {path}: {exc}. "
                         "Use a user-writable Paseo installation and rerun ./install.sh") from exc
