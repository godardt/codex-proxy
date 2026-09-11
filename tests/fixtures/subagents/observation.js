// Verbatim copy of @getpaseo/server's dist/server/server/agent/providers/claude/subagents/observation.js for offline tests (source map reference removed).
/**
 * Turn observations into store events.
 *
 * Pure and stateless by construction. Both the task protocol and a persisted transcript are
 * edge-triggered — a subagent is declared once and its status changes only when it changes — so
 * there is nothing to debounce here. Accumulation across partial upserts is the store's sticky
 * merge (omitted field preserves, explicit null clears), which is why a `status` observation can
 * carry status alone without blanking the title.
 */
export function foldSubagentObservations(observations) {
    const events = [];
    for (const observation of observations) {
        if (observation.kind === "timeline") {
            events.push({
                type: "timeline",
                id: observation.id,
                item: observation.item,
                ...(observation.timestamp ? { timestamp: observation.timestamp } : {}),
            });
            continue;
        }
        if (observation.kind === "subtitle") {
            events.push({
                type: "upsert",
                id: observation.id,
                subtitle: observation.subtitle,
                ...(observation.timestamp ? { timestamp: observation.timestamp } : {}),
            });
            continue;
        }
        if (observation.kind === "status") {
            events.push({
                type: "upsert",
                id: observation.id,
                status: observation.status,
                ...(observation.timestamp ? { timestamp: observation.timestamp } : {}),
            });
            continue;
        }
        // A declared subagent is running until something says otherwise. Its terminal status
        // arrives as its own observation, so this never has to guess at completion.
        events.push({
            type: "upsert",
            id: observation.id,
            status: "running",
            ...(observation.title === undefined ? {} : { title: observation.title }),
            ...(observation.description === undefined ? {} : { description: observation.description }),
            ...(observation.toolCallId === undefined ? {} : { toolCallId: observation.toolCallId }),
            ...(observation.parentSubagentId === undefined
                ? {}
                : { parentSubagentId: observation.parentSubagentId }),
            ...(observation.timestamp ? { timestamp: observation.timestamp } : {}),
        });
    }
    return events;
}
