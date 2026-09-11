"""Install a provider-scoped compatibility adapter into the selected Paseo package.

The adapter gives the Claude Codex provider live context usage, subagent
tracking for forked skills, and a mode catalog without Claude's auto mode.
Paseo's regular Claude provider keeps its original behavior.
"""

import os
from pathlib import Path
import shutil
import subprocess

from claude_codex import SetupError, atomic_write, file_lock, read_json, say


AGENT_PATH = Path("dist/server/server/agent/providers/claude/agent.js")
TASK_SOURCE_PATH = Path("subagents/live-source.js")
PATCH_MARKER = "// Managed by claude-codex: live context usage"
BASE_CLASS = "class ClaudeContextUsageState {"
RENAMED_CLASS = "class ClaudeCodexBaseContextUsageState {"
SESSION_CLASS = "class ClaudeAgentSession {"
INITIALIZER = "new ClaudeContextUsageState(findClaudeModel(this.config.model)?.contextWindowMaxTokens)"
# Provider settings and per-launch overrides are distinct in Paseo. Do not use
# process.env: a daemon started from Claude Codex must not mark native providers.
PATCHED_INITIALIZER = INITIALIZER[:-1] + ", { ...this.runtimeSettings?.env, ...this.launchEnv })"
TASK_SOURCE_IMPORT = 'import { ClaudeTaskProtocolSource, } from "./subagents/live-source.js";'
RENAMED_TASK_SOURCE_IMPORT = ('import { ClaudeTaskProtocolSource as ClaudeCodexBaseTaskProtocolSource, } '
                              'from "./subagents/live-source.js";')
TASK_SOURCE_NEW = "new ClaudeTaskProtocolSource({"
PATCHED_TASK_SOURCE_NEW = (TASK_SOURCE_NEW
                           + "\n            codexForkedSkills: () => claudeCodexForkedSkills(this),"
                           + "\n            getToolName: (toolUseId) => this.toolUseCache.get(toolUseId)?.name ?? null,")
RESOLVE_SIDECHAIN = "const canonicalSubagentId = this.taskProtocolSource.resolveSubagentId(parentToolUseId);"
PATCHED_RESOLVE_SIDECHAIN = (RESOLVE_SIDECHAIN[:-1]
                             + " ?? this.taskProtocolSource.declareForkedSkill(parentToolUseId);")
FINISH_SIDECHAIN = ('events.push(...this.sidechainTracker.finish(chunk.tool_use_id, '
                    'chunk.is_error ? "failed" : "completed"));')
PATCHED_FINISH_SIDECHAIN = (FINISH_SIDECHAIN[:-2]
                            + ', ...foldSubagentObservations(this.taskProtocolSource.finishForkedSkill('
                              'chunk.tool_use_id, chunk.is_error ? "failed" : "completed"))'
                              '.map((event) => ({ type: "provider_subagent", provider: "claude", event })));')
MODE_CATALOG = "function claudeModeCatalog(env) {\n    if (claudeAutoModeUnavailableOn(env)) {"
PATCHED_MODE_CATALOG = MODE_CATALOG[:-3] + " || claudeCodexAutoModeUnavailable(env)) {"
AVAILABLE_MODES = "getAvailableModes() {\n        return this.availableModes;"
PATCHED_AVAILABLE_MODES = AVAILABLE_MODES.replace("return this.availableModes;",
                                                  "return claudeCodexAvailableModes(this, this.availableModes);", 1)
REPLAY_FACTS = "function readClaudeReplayParentFacts(parentEntries) {"
RENAMED_REPLAY_FACTS = "function claudeCodexBaseReadClaudeReplayParentFacts(parentEntries) {"
REPLAY_ROOT_CALL = "parent: readClaudeReplayParentFacts(parentEntries),"
PATCHED_REPLAY_ROOT_CALL = REPLAY_ROOT_CALL[:-2] + ", claudeCodexForkedSkills(this)),"
REPLAY_CHILD_CALL = "parentFacts: readClaudeReplayParentFacts(entries),"
PATCHED_REPLAY_CHILD_CALL = REPLAY_CHILD_CALL[:-2] + ", claudeCodexForkedSkills(this)),"
# These are the consumer contracts the adapter relies on, not a package version
# check. Leave unfamiliar source untouched rather than guessing.
AGENT_ANCHORS = (
    "function toObjectRecord(value) {",
    "constructor(initialContextWindowMaxTokens) {",
    "setInitialContextWindowMaxTokens(contextWindowMaxTokens) {",
    "recordModelUsage(modelUsage) {", "buildStreamUsageEvent(event) {",
    "createUsageUpdatedEvent(contextWindowUsedTokens) {",
    "buildCompactionUsageEvent(postTokens) {",
    "this.streamRequestInputTokens = inputTokens;",
    "this.streamRequestOutputTokens = outputTokens;",
    "this.contextUsage.setInitialContextWindowMaxTokens(findClaudeModel(this.config.model)?.contextWindowMaxTokens);",
    "this.streamUsedTokens() ?? activeResultUsageTokens ?? this.compactedContextWindowUsedTokens",
    'import { foldSubagentObservations } from "./subagents/observation.js";',
    "function claudeAutoModeUnavailableOn(env) {",
    'DEFAULT_MODES.filter((mode) => mode.id !== "auto")',
    "buildSdkEnv() {",
    "translateSidechainFrameToEvents(message, parentToolUseId) {",
    "appendSidechainResultEvents(message, events) {",
    "ingestPersistedSidechains(parentContent, sidechains) {",
    "if (this.taskProtocolSource.announcesTasks && !canonicalSubagentId) {",
    "isDescriptorOwnedElsewhere: () => this.taskProtocolSource.isActive,",
)
# The subclass in the adapter extends this class, which Paseo keeps in a
# sibling module. It is read to confirm the contract and never modified.
TASK_SOURCE_ANCHORS = (
    "export class ClaudeTaskProtocolSource {",
    "this.subagentIdByTaskId = new Map();",
    "this.canonicalIdByToolUseId = new Map();",
    "this.ownerSubagentIdByToolUseId = new Map();",
    "this.declaredIds = new Set();",
    "this.idsWithExistingParentToolCard = new Set();",
    "this.backgroundedIds = new Set();",
    "this.lastStatusById = new Map();",
    "return this.sawTaskStarted;",
    "this.getToolInput = input.getToolInput ?? (() => null);",
    "observeSidechainFrame(message, subagentId) {",
    "observeTaskStarted(message) {",
    "updatePresentation(id, patch) {",
    "if (this.backgroundedIds.has(id))",
    "reset() {",
)


def adapter_source(name="paseo_usage.js"):
    return Path(__file__).with_name(name).read_text() + "\n"


def previous_replacements():
    """The layout written by earlier installers, recognized only to upgrade it."""
    return (
        (BASE_CLASS, RENAMED_CLASS),
        (SESSION_CLASS, adapter_source("paseo_usage_previous.js") + SESSION_CLASS),
        (INITIALIZER, PATCHED_INITIALIZER),
    )


def reverse_edits(source, edits):
    """Return the upstream source if `source` carries exactly `edits`, else None."""
    if any(source.count(patched) != 1 for _, patched in edits):
        return None
    original = source
    for upstream, patched in reversed(edits):
        original = original.replace(patched, upstream, 1)
    reapplied = original
    for upstream, patched in edits:
        reapplied = reapplied.replace(upstream, patched, 1)
    return original if reapplied == source else None


def replacements():
    return (
        (BASE_CLASS, RENAMED_CLASS),
        (SESSION_CLASS, adapter_source() + SESSION_CLASS),
        (INITIALIZER, PATCHED_INITIALIZER),
        (TASK_SOURCE_IMPORT, RENAMED_TASK_SOURCE_IMPORT),
        (TASK_SOURCE_NEW, PATCHED_TASK_SOURCE_NEW),
        (RESOLVE_SIDECHAIN, PATCHED_RESOLVE_SIDECHAIN),
        (FINISH_SIDECHAIN, PATCHED_FINISH_SIDECHAIN),
        (MODE_CATALOG, PATCHED_MODE_CATALOG),
        (AVAILABLE_MODES, PATCHED_AVAILABLE_MODES),
        (REPLAY_FACTS, RENAMED_REPLAY_FACTS),
        (REPLAY_ROOT_CALL, PATCHED_REPLAY_ROOT_CALL),
        (REPLAY_CHILD_CALL, PATCHED_REPLAY_CHILD_CALL),
    )


def patch_source(source):
    edits = replacements()
    if PATCH_MARKER in source:
        # Recognize our entire patch, not just its marker. A partial or locally
        # edited patch must not be mistaken for a working installation.
        original = reverse_edits(source, edits)
        if original is not None:
            if patch_source(original) != source:
                raise SetupError("Paseo's claude-codex compatibility patch has an unsupported structure")
            return source
        # An earlier installer's layout is upgraded from the recovered upstream source.
        original = reverse_edits(source, previous_replacements())
        if original is None:
            raise SetupError("Paseo's claude-codex compatibility patch is incomplete or modified; "
                             "restore its backup or reinstall Paseo, then rerun ./install.sh")
        return patch_source(original)
    if any(patched in source for _, patched in edits):
        raise SetupError("Paseo has a partial claude-codex compatibility patch; "
                         "restore the original and rerun ./install.sh")
    supported = all(source.count(anchor) == 1 for anchor in (*AGENT_ANCHORS, *(upstream for upstream, _ in edits)))
    if supported:
        setup = source[source.index(SESSION_CLASS):source.index(INITIALIZER)]
        supported = all(setup.count(anchor) == 1 for anchor in (
            "this.runtimeSettings = options.runtimeSettings;", "this.launchEnv = options.launchEnv;"))
    if not supported:
        raise SetupError("Unsupported Paseo Claude provider source; no patch was applied. "
                         "Update this checkout for the installed Paseo layout, or use --skip-paseo for terminal-only setup")
    for upstream, patched in edits:
        source = source.replace(upstream, patched, 1)
    return source


def check_task_source(agent_path):
    path = Path(agent_path).parent / TASK_SOURCE_PATH
    try:
        source = path.read_text()
    except OSError as exc:
        raise SetupError(f"Cannot read Paseo's Claude task protocol source {path}: {exc}. "
                         "Use --skip-paseo for terminal-only setup") from exc
    if any(source.count(anchor) != 1 for anchor in TASK_SOURCE_ANCHORS):
        raise SetupError(f"Unsupported Paseo Claude task protocol source {path}; no patch was applied. "
                         "Update this checkout for the installed Paseo layout, or use --skip-paseo for terminal-only setup")
    return path


def desktop_bundle_root(entry):
    # Paseo's desktop app ships its CLI shim next to an Electron app.asar that
    # packs the server module; files inside that archive cannot be patched.
    for directory in entry.parents:
        if (directory / "app.asar").is_file():
            return directory
    return None


def find_usage_reader(paseo_bin):
    entry = Path(paseo_bin).resolve()
    bundle = desktop_bundle_root(entry)
    if bundle is not None:
        raise SetupError(f"{paseo_bin} is the Paseo desktop app's bundled CLI ({bundle / 'app.asar'}); "
                         "its Claude provider module is packed inside the app archive and cannot be patched. "
                         "Install the npm CLI matching the app version into a private prefix, for example "
                         "npm install -g --prefix ~/.local/share/paseo-cli @getpaseo/cli@$(paseo --version), "
                         "then rerun ./install.sh --paseo-bin ~/.local/share/paseo-cli/bin/paseo "
                         "(later reruns reuse that selection while PATH still resolves to the bundle); "
                         "or use --skip-paseo for terminal-only setup. No package was modified")
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
        check_task_source(path)
        check_syntax(patched)
        if patched != source and not os.access(path.parent, os.W_OK):
            raise PermissionError(f"package directory is not writable: {path.parent}")
    except OSError as exc:
        raise SetupError(f"Cannot prepare Paseo compatibility: {exc}. "
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
            check_task_source(path)
            check_syntax(patched)
            if patched == source:
                say(f"Paseo compatibility already configured: {path}")
                return
            mode = path.stat().st_mode & 0o777
            backup(path)
            atomic_write(path, patched, mode=mode)
        say(f"Configured Paseo compatibility (live context usage, forked-skill subagents, mode catalog): {path}")
    except OSError as exc:
        raise SetupError(f"Cannot apply Paseo compatibility to {path}: {exc}. "
                         "Use a user-writable Paseo installation and rerun ./install.sh") from exc
