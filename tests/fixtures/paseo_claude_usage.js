// Offline representative of the installed Claude usage-reader contract.
// The usage helpers and original state class below are copied verbatim from
// installed source. Only the model lookup and session wrapper are reduced;
// constructor/model-change usage anchors retain their original spelling.
// No imports, SDK, daemon, package installation, or credentials are required.

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
class ClaudeAgentSession {
    constructor(config, options) {
        this.config = config;
        this.launchEnv = options.launchEnv;
        this.runtimeSettings = options.runtimeSettings;
        this.contextUsage = new ClaudeContextUsageState(findClaudeModel(this.config.model)?.contextWindowMaxTokens);
    }
    async setModel(modelId) {
        const normalizedModelId = typeof modelId === "string" && modelId.trim().length > 0 ? modelId.trim() : null;
        this.config.model = normalizedModelId ?? undefined;
        this.contextUsage.setInitialContextWindowMaxTokens(findClaudeModel(this.config.model)?.contextWindowMaxTokens);
    }
}
function findClaudeModel(modelId) {
    return {
        "native-model": { contextWindowMaxTokens: 200000 },
        "larger-native-model": { contextWindowMaxTokens: 300000 },
    }[modelId];
}

export { ClaudeContextUsageState, ClaudeAgentSession };
