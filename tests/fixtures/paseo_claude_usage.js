// Offline representative of the installed Claude provider contract.
// The usage helpers, original state class, mode catalog, replay fact readers,
// and sidechain routing below are copied verbatim from installed source; the
// subagent modules beside this file are verbatim copies too. Only the model
// lookup, the legacy sidechain tracker, and the session wrapper are reduced;
// every anchor the installer edits retains its original spelling.
// No SDK, daemon, package installation, or credentials are required.
import { ClaudeTaskProtocolSource, } from "./subagents/live-source.js";
import { foldSubagentObservations } from "./subagents/observation.js";

function isObjectRecord(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
function toObjectRecord(value) {
    return isObjectRecord(value) ? value : undefined;
}
function readTrimmedString(value) {
    if (typeof value !== "string") {
        return undefined;
    }
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : undefined;
}
function extractContextWindowSize(modelUsage) {
    const usageRecord = toObjectRecord(modelUsage);
    if (!usageRecord) {
        return undefined;
    }
    let maxContextWindow;
    for (const value of Object.values(usageRecord)) {
        const valueRecord = toObjectRecord(value);
        if (!valueRecord) {
            continue;
        }
        const contextWindow = valueRecord.contextWindow;
        if (typeof contextWindow !== "number" ||
            !Number.isFinite(contextWindow) ||
            contextWindow <= 0) {
            continue;
        }
        maxContextWindow = Math.max(maxContextWindow ?? 0, contextWindow);
    }
    return maxContextWindow;
}
function readStreamRequestInputTokens(event) {
    const messageUsage = toObjectRecord(toObjectRecord(event.message)?.usage);
    if (!messageUsage) {
        return undefined;
    }
    const usage = messageUsage;
    const inputTokens = typeof usage.input_tokens === "number" && Number.isFinite(usage.input_tokens)
        ? usage.input_tokens
        : undefined;
    const cacheCreationInputTokens = typeof usage.cache_creation_input_tokens === "number" &&
        Number.isFinite(usage.cache_creation_input_tokens)
        ? usage.cache_creation_input_tokens
        : 0;
    const cacheReadInputTokens = typeof usage.cache_read_input_tokens === "number" &&
        Number.isFinite(usage.cache_read_input_tokens)
        ? usage.cache_read_input_tokens
        : 0;
    if (typeof inputTokens !== "number" || inputTokens < 0) {
        return undefined;
    }
    return inputTokens + cacheCreationInputTokens + cacheReadInputTokens;
}
function readStreamRequestOutputTokens(event) {
    const outputTokens = toObjectRecord(event.usage)?.output_tokens;
    if (typeof outputTokens !== "number" || !Number.isFinite(outputTokens) || outputTokens < 0) {
        return undefined;
    }
    return outputTokens;
}
function readLastUsageIteration(usage) {
    const iterations = toObjectRecord(usage)?.iterations;
    if (!Array.isArray(iterations)) {
        return undefined;
    }
    for (let index = iterations.length - 1; index >= 0; index -= 1) {
        const candidate = toObjectRecord(iterations[index]);
        if (candidate) {
            return candidate;
        }
    }
    return undefined;
}
function readUsageTokenTotal(usage) {
    const usageWithCacheCreation = usage;
    const inputTokens = typeof usage.input_tokens === "number" && Number.isFinite(usage.input_tokens)
        ? usage.input_tokens
        : 0;
    const cacheCreationInputTokens = typeof usageWithCacheCreation.cache_creation_input_tokens === "number" &&
        Number.isFinite(usageWithCacheCreation.cache_creation_input_tokens)
        ? usageWithCacheCreation.cache_creation_input_tokens
        : 0;
    const cacheReadInputTokens = typeof usage.cache_read_input_tokens === "number" &&
        Number.isFinite(usage.cache_read_input_tokens)
        ? usage.cache_read_input_tokens
        : 0;
    const outputTokens = typeof usage.output_tokens === "number" && Number.isFinite(usage.output_tokens)
        ? usage.output_tokens
        : 0;
    const total = inputTokens + cacheCreationInputTokens + cacheReadInputTokens + outputTokens;
    return total > 0 ? total : undefined;
}
function readActiveUsageTokens(usage) {
    const activeUsage = readLastUsageIteration(usage);
    return activeUsage ? readUsageTokenTotal(activeUsage) : undefined;
}
function readLegacyResultUsageTokens(usage) {
    const usageRecord = toObjectRecord(usage);
    return usageRecord ? readUsageTokenTotal(usageRecord) : undefined;
}
class ClaudeContextUsageState {
    constructor(initialContextWindowMaxTokens) {
        this.completedResultTurns = 0;
        this.contextWindowMaxTokens = initialContextWindowMaxTokens;
    }
    beginTurn() {
        this.streamRequestInputTokens = undefined;
        this.streamRequestOutputTokens = undefined;
        this.compactedContextWindowUsedTokens = undefined;
    }
    setInitialContextWindowMaxTokens(contextWindowMaxTokens) {
        this.contextWindowMaxTokens = contextWindowMaxTokens;
    }
    recordModelUsage(modelUsage) {
        const contextWindowMaxTokens = extractContextWindowSize(modelUsage);
        if (contextWindowMaxTokens !== undefined) {
            this.contextWindowMaxTokens = contextWindowMaxTokens;
        }
        return this.contextWindowMaxTokens;
    }
    buildStreamUsageEvent(event) {
        const streamEvent = toObjectRecord(event);
        if (!streamEvent) {
            return null;
        }
        const eventType = readTrimmedString(streamEvent.type);
        if (eventType === "message_start") {
            const inputTokens = readStreamRequestInputTokens(streamEvent);
            if (typeof inputTokens !== "number") {
                return null;
            }
            this.streamRequestInputTokens = inputTokens;
            this.streamRequestOutputTokens = 0;
        }
        else if (eventType === "message_delta") {
            const outputTokens = readStreamRequestOutputTokens(streamEvent);
            if (typeof outputTokens !== "number") {
                return null;
            }
            this.streamRequestOutputTokens = outputTokens;
        }
        else {
            return null;
        }
        const usedTokens = this.streamUsedTokens();
        if (usedTokens === undefined) {
            return null;
        }
        return this.createUsageUpdatedEvent(usedTokens);
    }
    buildResultUsage(message, modelUsage) {
        try {
            if (!message.usage) {
                return undefined;
            }
            const usage = {
                inputTokens: message.usage.input_tokens,
                cachedInputTokens: message.usage.cache_read_input_tokens,
                outputTokens: message.usage.output_tokens,
                totalCostUsd: message.total_cost_usd,
            };
            const modelContextWindowMaxTokens = this.recordModelUsage(modelUsage ?? message.modelUsage);
            if (this.contextWindowMaxTokens !== undefined) {
                usage.contextWindowMaxTokens = this.contextWindowMaxTokens;
            }
            else if (modelContextWindowMaxTokens !== undefined) {
                usage.contextWindowMaxTokens = modelContextWindowMaxTokens;
            }
            const activeResultUsageTokens = readActiveUsageTokens(message.usage) ??
                (this.completedResultTurns === 0 ? readLegacyResultUsageTokens(message.usage) : undefined);
            const usedTokens = this.streamUsedTokens() ?? activeResultUsageTokens ?? this.compactedContextWindowUsedTokens;
            if (usedTokens !== undefined) {
                usage.contextWindowUsedTokens = usedTokens;
            }
            return usage;
        }
        finally {
            this.compactedContextWindowUsedTokens = undefined;
            this.completedResultTurns += 1;
        }
    }
    streamUsedTokens() {
        if (typeof this.streamRequestInputTokens !== "number" ||
            typeof this.streamRequestOutputTokens !== "number") {
            return undefined;
        }
        const usedTokens = this.streamRequestInputTokens + this.streamRequestOutputTokens;
        return usedTokens > 0 ? usedTokens : undefined;
    }
    createUsageUpdatedEvent(contextWindowUsedTokens) {
        const usage = {
            contextWindowUsedTokens,
        };
        if (this.contextWindowMaxTokens !== undefined) {
            usage.contextWindowMaxTokens = this.contextWindowMaxTokens;
        }
        return {
            type: "usage_updated",
            provider: "claude",
            usage,
        };
    }
    buildCompactionUsageEvent(postTokens) {
        this.streamRequestInputTokens = undefined;
        this.streamRequestOutputTokens = undefined;
        this.compactedContextWindowUsedTokens = postTokens;
        const usage = {};
        if (this.contextWindowMaxTokens !== undefined) {
            usage.contextWindowMaxTokens = this.contextWindowMaxTokens;
        }
        if (postTokens !== undefined) {
            usage.contextWindowUsedTokens = postTokens;
        }
        return {
            type: "usage_updated",
            provider: "claude",
            usage,
        };
    }
}
const DEFAULT_MODES = [
    {
        id: "plan",
        label: "Plan Mode",
        description: "Analyze the codebase without executing tools or edits",
    },
    {
        id: "default",
        label: "Always Ask",
        description: "Prompts for permission the first time a tool is used",
    },
    {
        id: "acceptEdits",
        label: "Accept File Edits",
        description: "Automatically approves edit-focused tools without prompting",
    },
    {
        id: "auto",
        label: "Auto mode",
        description: "Uses a model classifier to review permission prompts automatically",
    },
    {
        id: "bypassPermissions",
        label: "Bypass",
        description: "Skip all permission prompts (use with caution)",
    },
];
function isTruthyEnvValue(value) {
    const normalized = value?.trim().toLowerCase();
    return (normalized !== undefined &&
        normalized.length > 0 &&
        normalized !== "0" &&
        normalized !== "false" &&
        normalized !== "no" &&
        normalized !== "off");
}
function claudeAutoModeUnavailableOn(env) {
    if (isTruthyEnvValue(env.CLAUDE_CODE_USE_BEDROCK)) {
        return "Bedrock";
    }
    if (isTruthyEnvValue(env.CLAUDE_CODE_USE_VERTEX)) {
        return "Vertex";
    }
    return null;
}
function claudeModeCatalog(env) {
    if (claudeAutoModeUnavailableOn(env)) {
        return { modes: DEFAULT_MODES.filter((mode) => mode.id !== "auto"), defaultModeId: "default" };
    }
    return { modes: DEFAULT_MODES, defaultModeId: "auto" };
}
function readNonEmptyString(value) {
    return typeof value === "string" && value.trim().length > 0 ? value : undefined;
}
function readClaudeParentToolUseId(message) {
    if (!("parent_tool_use_id" in message)) {
        return null;
    }
    const parentToolUseId = message.parent_tool_use_id;
    return typeof parentToolUseId === "string" && parentToolUseId.length > 0 ? parentToolUseId : null;
}
// Reduced legacy tracker: descriptor ownership and finish semantics are kept,
// child timeline extraction is not.
class ClaudeSidechainTracker {
    constructor(input) {
        this.activeSidechains = new Map();
        this.getToolInput = input.getToolInput;
        this.isDescriptorOwnedElsewhere = input.isDescriptorOwnedElsewhere ?? (() => false);
        this.needsSyntheticParentToolCard = input.needsSyntheticParentToolCard ?? (() => true);
    }
    handleMessage(message, parentToolUseId) {
        const state = this.activeSidechains.get(parentToolUseId) ?? {};
        this.activeSidechains.set(parentToolUseId, state);
        const taskInput = this.getToolInput(parentToolUseId);
        state.name = readNonEmptyString(taskInput?.name) ?? state.name;
        state.subAgentType = readNonEmptyString(taskInput?.subagent_type) ?? state.subAgentType;
        state.description = readNonEmptyString(taskInput?.description) ?? state.description;
        const descriptorEvents = this.isDescriptorOwnedElsewhere()
            ? []
            : [{
                    type: "provider_subagent",
                    provider: "claude",
                    event: {
                        type: "upsert",
                        id: parentToolUseId,
                        title: state.name ?? state.subAgentType ?? "Claude subagent",
                        description: state.description ?? null,
                        status: "running",
                        toolCallId: parentToolUseId,
                    },
                }];
        const parentCard = this.needsSyntheticParentToolCard(parentToolUseId)
            ? [{ type: "timeline", provider: "claude", item: { type: "tool_call", name: "Task", callId: parentToolUseId, status: "running", detail: { type: "sub_agent" } } }]
            : [];
        return [...descriptorEvents, ...parentCard];
    }
    finish(id, status) {
        const state = this.activeSidechains.get(id);
        if (!state)
            return [];
        this.activeSidechains.delete(id);
        if (this.isDescriptorOwnedElsewhere())
            return [];
        return [{
                type: "provider_subagent",
                provider: "claude",
                event: {
                    type: "upsert",
                    id,
                    title: state.name ?? state.subAgentType ?? "Claude subagent",
                    description: state.description ?? null,
                    status,
                    toolCallId: id,
                },
            }];
    }
    clear() {
        this.activeSidechains.clear();
    }
}
class ClaudeAgentSession {
    constructor(config, options) {
        this.config = config;
        this.toolUseCache = new Map();
        this.availableModes = DEFAULT_MODES;
        this.currentMode = "default";
        this.taskProtocolSource = new ClaudeTaskProtocolSource({
            getToolInput: (toolUseId) => this.toolUseCache.get(toolUseId)?.input ?? null,
            readWorkflowResult: () => undefined,
        });
        this.sidechainTracker = new ClaudeSidechainTracker({
            getToolInput: (toolUseId) => this.toolUseCache.get(toolUseId)?.input ?? null,
            isDescriptorOwnedElsewhere: () => this.taskProtocolSource.isActive,
            needsSyntheticParentToolCard: (toolUseId) => this.taskProtocolSource.needsSyntheticParentToolCard(toolUseId),
        });
        this.launchEnv = options.launchEnv;
        this.runtimeSettings = options.runtimeSettings;
        this.contextUsage = new ClaudeContextUsageState(findClaudeModel(this.config.model)?.contextWindowMaxTokens);
    }
    async setModel(modelId) {
        const normalizedModelId = typeof modelId === "string" && modelId.trim().length > 0 ? modelId.trim() : null;
        this.config.model = normalizedModelId ?? undefined;
        this.contextUsage.setInitialContextWindowMaxTokens(findClaudeModel(this.config.model)?.contextWindowMaxTokens);
    }
    buildSdkEnv() {
        return { ...process.env, ...this.runtimeSettings?.env, ...this.launchEnv };
    }
    getAvailableModes() {
        return this.availableModes;
    }
    handleSystemMessage(message) {
        if (message.subtype !== "init") {
            return;
        }
        this.availableModes = DEFAULT_MODES;
        this.currentMode = message.permissionMode;
    }
    translateMessageToEvents(message) {
        const parentToolUseId = readClaudeParentToolUseId(message);
        if (parentToolUseId) {
            return this.translateSidechainFrameToEvents(message, parentToolUseId);
        }
        const events = [];
        const subagentObservations = this.taskProtocolSource.observe(message);
        for (const event of foldSubagentObservations(subagentObservations)) {
            events.push({ type: "provider_subagent", provider: "claude", event });
        }
        for (const observation of subagentObservations) {
            if (observation.kind !== "declared")
                continue;
            if (!this.taskProtocolSource.needsSyntheticParentToolCard(observation.id))
                continue;
            const card = this.buildSubagentToolCallCard(observation);
            if (card)
                events.push(card);
        }
        switch (message.type) {
            case "system":
                this.handleSystemMessage(message);
                break;
            case "user":
                this.appendSidechainResultEvents(message, events);
                break;
            case "assistant": {
                for (const block of message.message?.content ?? []) {
                    if (block?.type === "tool_use" && typeof block.id === "string") {
                        this.toolUseCache.set(block.id, { id: block.id, name: block.name, input: block.input ?? null, started: true });
                    }
                }
                this.appendSidechainResultEvents(message, events);
                break;
            }
            default:
                break;
        }
        return events;
    }
    /**
     * A frame from inside a subagent, routed to that child rather than the parent's transcript.
     */
    translateSidechainFrameToEvents(message, parentToolUseId) {
        const canonicalSubagentId = this.taskProtocolSource.resolveSubagentId(parentToolUseId);
        if (this.taskProtocolSource.announcesTasks && !canonicalSubagentId) {
            return [];
        }
        const runtimeEvents = foldSubagentObservations(this.taskProtocolSource.observeSidechainFrame(message, canonicalSubagentId ?? parentToolUseId)).map((event) => ({ type: "provider_subagent", provider: "claude", event }));
        const routedId = canonicalSubagentId ?? parentToolUseId;
        return [...runtimeEvents, ...this.sidechainTracker.handleMessage(message, routedId)];
    }
    buildSubagentToolCallCard(declaration) {
        if (declaration.parentSubagentId)
            return null;
        return {
            type: "timeline",
            provider: "claude",
            item: {
                type: "tool_call",
                name: "Task",
                callId: declaration.id,
                status: "running",
                detail: {
                    type: "sub_agent",
                    ...(declaration.title ? { subAgentType: declaration.title } : {}),
                    ...(declaration.description ? { description: declaration.description } : {}),
                    log: "",
                    actions: [],
                },
            },
        };
    }
    appendSidechainResultEvents(message, events) {
        const content = toObjectRecord(toObjectRecord(message)?.message)?.content;
        if (!Array.isArray(content))
            return;
        for (const block of content) {
            const chunk = toObjectRecord(block);
            if (chunk?.type !== "tool_result" || typeof chunk.tool_use_id !== "string")
                continue;
            events.push(...this.sidechainTracker.finish(chunk.tool_use_id, chunk.is_error ? "failed" : "completed"));
        }
    }
    // Reduced replay entry point: returns the replay input instead of folding it
    // through the replay source, so tests can inspect the facts it derives.
    ingestPersistedSidechains(parentContent, sidechains) {
        const parentEntries = parseClaudeHistoryRecords(parentContent).filter((entry) => entry.isSidechain !== true);
        const sidechainEntries = [parentContent, ...sidechains.contents]
            .flatMap(parseClaudeHistoryRecords)
            .filter((entry) => entry.isSidechain === true && typeof entry.agentId === "string");
        return {
            subagents: [...groupClaudeSidechainEntries(sidechainEntries)].map(([agentId, entries]) => ({
                agentId,
                meta: sidechains.metaByAgentId.get(agentId) ?? null,
                entries,
                parentFacts: readClaudeReplayParentFacts(entries),
            })),
            parent: readClaudeReplayParentFacts(parentEntries),
        };
    }
}
function findClaudeModel(modelId) {
    return {
        "native-model": { contextWindowMaxTokens: 200000 },
        "larger-native-model": { contextWindowMaxTokens: 300000 },
    }[modelId];
}
function parseClaudeHistoryRecords(contents) {
    const entries = [];
    for (const line of contents.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed)
            continue;
        try {
            const entry = toObjectRecord(JSON.parse(trimmed));
            if (entry)
                entries.push(entry);
        }
        catch {
            // Ignore individual corrupt history rows, matching the parent history replay behavior.
        }
    }
    return entries;
}
function readClaudeReplayParentFacts(parentEntries) {
    const toolCalls = new Map();
    for (const [id, call] of readClaudeHistoricalSubagentToolCalls(parentEntries)) {
        toolCalls.set(id, {
            ...((call.name ?? call.subagentType) ? { title: call.name ?? call.subagentType } : {}),
            ...(call.description ? { description: call.description } : {}),
        });
    }
    const outcomesByToolCallId = new Map();
    for (const entry of parentEntries) {
        const content = toObjectRecord(entry.message)?.content;
        if (!Array.isArray(content))
            continue;
        for (const value of content) {
            const block = toObjectRecord(value);
            if (block?.type !== "tool_result" || typeof block.tool_use_id !== "string")
                continue;
            if (!toolCalls.has(block.tool_use_id))
                continue;
            outcomesByToolCallId.set(block.tool_use_id, { failed: block.is_error === true });
        }
    }
    return {
        toolCalls,
        linksByAgentId: readClaudeHistoricalSubagentToolResults(parentEntries),
        outcomesByToolCallId,
    };
}
function readClaudeHistoricalSubagentToolCalls(entries) {
    const toolCalls = new Map();
    for (const entry of entries) {
        const content = toObjectRecord(entry.message)?.content;
        if (!Array.isArray(content))
            continue;
        for (const value of content) {
            const block = toObjectRecord(value);
            if (block?.type !== "tool_use" ||
                (block.name !== "Task" && block.name !== "Agent") ||
                typeof block.id !== "string") {
                continue;
            }
            const input = toObjectRecord(block.input);
            const name = readNonEmptyString(input?.name);
            const subagentType = readNonEmptyString(input?.subagent_type);
            const description = readNonEmptyString(input?.description);
            toolCalls.set(block.id, {
                ...(name ? { name } : {}),
                ...(subagentType ? { subagentType } : {}),
                ...(description ? { description } : {}),
            });
        }
    }
    return toolCalls;
}
function readClaudeHistoricalSubagentToolResults(entries) {
    const results = new Map();
    for (const entry of entries) {
        const content = toObjectRecord(entry.message)?.content;
        if (!Array.isArray(content))
            continue;
        for (const value of content) {
            const block = toObjectRecord(value);
            if (block?.type !== "tool_result" || typeof block.tool_use_id !== "string")
                continue;
            const match = /agentId:\s*([\w-]+)/.exec(JSON.stringify(block.content));
            if (!match?.[1])
                continue;
            results.set(match[1], { toolCallId: block.tool_use_id, failed: block.is_error === true });
        }
    }
    return results;
}
function groupClaudeSidechainEntries(entries) {
    const entriesByAgentId = new Map();
    for (const entry of entries) {
        if (typeof entry.agentId !== "string")
            continue;
        const grouped = entriesByAgentId.get(entry.agentId) ?? [];
        grouped.push(entry);
        entriesByAgentId.set(entry.agentId, grouped);
    }
    return entriesByAgentId;
}

export { ClaudeContextUsageState, ClaudeAgentSession, claudeModeCatalog, readClaudeReplayParentFacts, DEFAULT_MODES };
