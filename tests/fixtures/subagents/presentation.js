// Verbatim copy of @getpaseo/server's dist/server/server/agent/providers/claude/subagents/presentation.js for offline tests (source map reference removed).
import { findClaudeModel } from "../models.js";
/** Build the complete compact subtitle Claude exposes to provider-neutral clients. */
export function buildClaudeSubagentSubtitle(facts) {
    const parts = [
        readPart(facts.title),
        formatModel(facts.model),
        formatEffort(facts.effort),
        formatTokens(facts.usage?.totalTokens),
    ].filter((part) => part !== undefined);
    return parts.length > 0 ? parts.join(" · ") : undefined;
}
function readPart(value) {
    const trimmed = value?.trim();
    return trimmed ? trimmed : undefined;
}
function formatModel(modelId) {
    const normalized = readPart(modelId);
    if (!normalized)
        return undefined;
    return findClaudeModel(normalized)?.label ?? normalized;
}
function formatEffort(effort) {
    const normalized = readPart(effort);
    if (!normalized)
        return undefined;
    if (normalized === "xhigh")
        return "Extra High";
    return normalized
        .split(/[-_\s]+/)
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ");
}
function formatTokens(totalTokens) {
    if (typeof totalTokens !== "number" || totalTokens <= 0)
        return undefined;
    if (totalTokens < 1000)
        return `${Math.round(totalTokens)} tokens`;
    return `${Math.round(totalTokens / 100) / 10}k tokens`;
}
