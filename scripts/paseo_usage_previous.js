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
// Managed by claude-codex: live context usage (end)
