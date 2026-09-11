// Managed by claude-codex: live context usage (begin)
class ClaudeContextUsageState extends ClaudeCodexBaseContextUsageState {
    constructor(initialContextWindowMaxTokens, launchEnv) {
        super(initialContextWindowMaxTokens);
        this.codexLateUsage = launchEnv?.CLAUDE_CODEX_PASEO_USAGE === "1";
        const configured = launchEnv?.CLAUDE_CODE_MAX_CONTEXT_TOKENS;
        const window = typeof configured === "string" && /^\d+$/.test(configured)
            ? Number(configured) : undefined;
        this.codexContextWindow = this.codexLateUsage && Number.isSafeInteger(window) && window > 0
            ? window : undefined;
        this.setInitialContextWindowMaxTokens(initialContextWindowMaxTokens);
    }
    setInitialContextWindowMaxTokens(value) {
        super.setInitialContextWindowMaxTokens(this.codexContextWindow ?? value);
    }
    recordModelUsage(modelUsage) {
        if (this.codexContextWindow !== undefined) {
            this.contextWindowMaxTokens = this.codexContextWindow;
            return this.contextWindowMaxTokens;
        }
        return super.recordModelUsage(modelUsage);
    }
    buildStreamUsageEvent(event) {
        if (!this.codexLateUsage) {
            return super.buildStreamUsageEvent(event);
        }
        if (event?.type === "message_start") {
            // The gateway has no measured input yet. Keep the displayed last
            // request, but never combine its input with this request's output.
            this.streamRequestInputTokens = undefined;
            this.streamRequestOutputTokens = undefined;
            return null;
        }
        if (event?.type === "message_delta") {
            const usage = toObjectRecord(event.usage);
            if (usage && Object.hasOwn(usage, "input_tokens")) {
                const keys = ["input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"];
                const values = keys.map(key => Object.hasOwn(usage, key) ? usage[key] : 0);
                const valid = values.every(value => typeof value === "number" &&
                    Number.isFinite(value) && value >= 0);
                const input = valid ? values[0] + values[2] + values[3] : NaN;
                if (!valid || !Number.isFinite(input + values[1])) {
                    this.streamRequestInputTokens = undefined;
                    this.streamRequestOutputTokens = undefined;
                    return null;
                }
                this.streamRequestInputTokens = input;
                this.streamRequestOutputTokens = undefined;
            }
        }
        return super.buildStreamUsageEvent(event);
    }
}
// The provider entry written by the claude-codex installer sets this marker in
// its environment. Sessions of Paseo's regular Claude provider never carry it.
function claudeCodexProviderEnv(session) {
    return { ...session?.runtimeSettings?.env, ...session?.launchEnv };
}
function claudeCodexEnabled(env) {
    return env?.CLAUDE_CODEX_PASEO_USAGE === "1";
}
// Claude's auto mode needs Anthropic's classifier models, which the Codex
// gateway cannot serve; the launcher therefore disables auto mode in Claude
// itself. Paseo's mode catalog must not offer or default to a mode that the
// session silently replaces, unless the user opted back in.
function claudeCodexAutoModeUnavailable(env) {
    return claudeCodexEnabled(env) && env?.CLAUDE_CODEX_AUTO_MODE !== "1";
}
function claudeCodexAvailableModes(session, modes) {
    if (!Array.isArray(modes) || !claudeCodexAutoModeUnavailable(session.buildSdkEnv())) {
        return modes;
    }
    return modes.filter((mode) => mode?.id !== "auto");
}
function claudeCodexForkedSkills(session) {
    return claudeCodexEnabled(claudeCodexProviderEnv(session));
}
function claudeCodexSkillText(value) {
    return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
}
// Claude Code runs a `context: fork` skill inside a subagent without announcing
// it through its task protocol. The child's frames carry the Skill call id as
// parent_tool_use_id, and only the Skill tool_result ends it. The Skill call is
// therefore its declaration, and the tool_result its terminal status; without
// this the child is created by the frame-derived tracker, orphaned once any
// announced task hands ownership to the protocol, and its own Task children
// are declared without a parent.
class ClaudeTaskProtocolSource extends ClaudeCodexBaseTaskProtocolSource {
    constructor(input = {}) {
        super(input);
        this.codexForkedSkills = input.codexForkedSkills ?? (() => false);
        this.codexToolName = input.getToolName ?? (() => null);
        this.codexForkIds = new Set();
        /** Skill calls issued inside a declared child, keyed by tool_use id, for nested forks. */
        this.codexSidechainSkillCalls = new Map();
        /** Declaration observations waiting for the frame that triggered them. */
        this.codexPendingObservations = new Map();
    }
    declareForkedSkill(toolUseId) {
        if (typeof toolUseId !== "string" || toolUseId.length === 0) {
            return undefined;
        }
        if (this.codexForkIds.has(toolUseId)) {
            return toolUseId;
        }
        if (this.declaredIds.has(toolUseId) || !this.codexForkedSkills()) {
            return undefined;
        }
        const nested = this.codexSidechainSkillCalls.has(toolUseId);
        if (!nested && this.codexToolName(toolUseId) !== "Skill") {
            return undefined;
        }
        const input = nested ? this.codexSidechainSkillCalls.get(toolUseId) : this.getToolInput(toolUseId);
        const title = claudeCodexSkillText(input?.skill) ?? "Skill";
        const description = claudeCodexSkillText(input?.args);
        const parentSubagentId = this.ownerSubagentIdByToolUseId.get(toolUseId);
        this.codexForkIds.add(toolUseId);
        this.canonicalIdByToolUseId.set(toolUseId, toolUseId);
        this.declaredIds.add(toolUseId);
        this.lastStatusById.set(toolUseId, "running");
        // The Skill call already has its own card in the transcript that issued it.
        this.idsWithExistingParentToolCard.add(toolUseId);
        this.sawTaskStarted = true;
        this.codexPendingObservations.set(toolUseId, [
            {
                kind: "declared",
                id: toolUseId,
                toolCallId: toolUseId,
                title,
                ...(description ? { description } : {}),
                ...(parentSubagentId ? { parentSubagentId } : {}),
            },
            ...this.updatePresentation(toolUseId, { title }),
        ]);
        return toolUseId;
    }
    finishForkedSkill(toolUseId, status) {
        if (!this.codexForkIds.has(toolUseId) || this.lastStatusById.get(toolUseId) === status) {
            return [];
        }
        this.lastStatusById.set(toolUseId, status);
        return [{ kind: "status", id: toolUseId, status }];
    }
    observeSidechainFrame(message, subagentId) {
        const observations = this.codexPendingObservations.get(subagentId) ?? [];
        this.codexPendingObservations.delete(subagentId);
        const content = message?.message?.content;
        if (Array.isArray(content) && this.declaredIds.has(subagentId)) {
            for (const block of content) {
                if (!block || typeof block !== "object") {
                    continue;
                }
                if (message.type === "assistant" && block.type === "tool_use" &&
                        block.name === "Skill" && typeof block.id === "string") {
                    this.codexSidechainSkillCalls.set(block.id, block.input ?? null);
                }
                else if (message.type === "user" && block.type === "tool_result" &&
                        typeof block.tool_use_id === "string") {
                    observations.push(...this.finishForkedSkill(block.tool_use_id,
                        block.is_error === true ? "failed" : "completed"));
                }
            }
        }
        observations.push(...super.observeSidechainFrame(message, subagentId));
        return observations;
    }
    observeTaskStarted(message) {
        const observations = super.observeTaskStarted(message);
        // A child launched directly in the background is announced that way on
        // task_started; only later changes arrive as a task_updated patch.
        if (message?.is_backgrounded === true) {
            const id = this.subagentIdByTaskId.get(message.task_id);
            if (id !== undefined) {
                this.backgroundedIds.add(id);
            }
        }
        return observations;
    }
    reset() {
        super.reset();
        this.codexForkIds.clear();
        this.codexSidechainSkillCalls.clear();
        this.codexPendingObservations.clear();
    }
}
// Replay links a forked skill's transcript through the Skill call and the
// agentId Claude records on that call's result, mirroring the live declaration.
function readClaudeReplayParentFacts(parentEntries, forkedSkills = false) {
    const facts = claudeCodexBaseReadClaudeReplayParentFacts(parentEntries);
    if (!forkedSkills) {
        return facts;
    }
    for (const entry of parentEntries) {
        const content = toObjectRecord(entry.message)?.content;
        if (!Array.isArray(content)) {
            continue;
        }
        for (const value of content) {
            const block = toObjectRecord(value);
            if (block?.type === "tool_use" && block.name === "Skill" && typeof block.id === "string") {
                const input = toObjectRecord(block.input);
                const description = claudeCodexSkillText(input?.args);
                if (!facts.toolCalls.has(block.id)) {
                    facts.toolCalls.set(block.id, {
                        title: claudeCodexSkillText(input?.skill) ?? "Skill",
                        ...(description ? { description } : {}),
                    });
                }
                continue;
            }
            if (block?.type !== "tool_result" || typeof block.tool_use_id !== "string") {
                continue;
            }
            const result = toObjectRecord(entry.toolUseResult) ?? toObjectRecord(entry.tool_use_result);
            const agentId = claudeCodexSkillText(result?.agentId);
            if (result?.status !== "forked" || !agentId || !facts.toolCalls.has(block.tool_use_id)) {
                continue;
            }
            const failed = block.is_error === true || result.success === false;
            facts.linksByAgentId.set(agentId, { toolCallId: block.tool_use_id, failed });
            facts.outcomesByToolCallId.set(block.tool_use_id, { failed });
        }
    }
    return facts;
}
// Managed by claude-codex: live context usage (end)
