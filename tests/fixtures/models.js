// Offline stand-in for Paseo's Claude model helpers used by the copied
// subagent modules. Only the two functions those modules import are provided.
const MODELS = {
    "native-model": { label: "Native Model", contextWindowMaxTokens: 200000 },
    "larger-native-model": { label: "Larger Native Model", contextWindowMaxTokens: 300000 },
};
export function findClaudeModel(modelId) {
    return MODELS[modelId];
}
export function resolveObservedClaudeModelId(value) {
    const trimmed = typeof value === "string" ? value.trim() : "";
    if (!trimmed || trimmed === "<synthetic>") {
        return null;
    }
    return trimmed;
}
