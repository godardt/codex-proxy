// Verbatim copy of @getpaseo/server's dist/server/server/agent/providers/claude/subagents/live-source.js for offline tests (source map reference removed).
import { resolveObservedClaudeModelId } from "../models.js";
import { buildClaudeSubagentSubtitle, } from "./presentation.js";
/** Task-tool subagents. Backgrounded shell commands announce as `local_bash`. */
const CLAUDE_SUBAGENT_TASK_TYPE = "local_agent";
/** Workflow executions use the same announced task lifecycle as Task-tool subagents. */
const CLAUDE_WORKFLOW_TASK_TYPE = "local_workflow";
/**
 * Not every announced task belongs in the subagents track. Verified on the wire:
 *
 *   Task subagent      task_type "local_agent", subagent_type "general-purpose"
 *   Workflow           task_type "local_workflow", no subagent_type
 *   background shell   task_type "local_bash",  no subagent_type
 *
 * All three carry a `tool_use_id`, so presence of an id is not a discriminator — filtering on it
 * alone puts `sleep 20` in the subagents track. Releases that predate `task_type` are covered
 * by requiring a subagent type instead.
 */
function isProviderSubagentTask(message) {
    if (message.task_type) {
        return (message.task_type === CLAUDE_SUBAGENT_TASK_TYPE ||
            message.task_type === CLAUDE_WORKFLOW_TASK_TYPE);
    }
    return readString(message.subagent_type) !== undefined;
}
function readUsage(usage) {
    if (!usage)
        return null;
    const observed = {};
    if (typeof usage.total_tokens === "number")
        observed.totalTokens = usage.total_tokens;
    return Object.keys(observed).length > 0 ? observed : null;
}
function readString(value) {
    return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
}
/**
 * The descriptor has no paused or pending state, and neither is terminal, so both read as
 * running. `killed` is a cancellation, not a failure — the child was stopped, it did not error.
 */
function mapTaskStatus(status) {
    switch (status) {
        case "pending":
        case "running":
        case "paused":
            return "running";
        case "completed":
            return "completed";
        case "failed":
            return "failed";
        case "killed":
        case "stopped":
            return "canceled";
        default:
            return undefined;
    }
}
export class ClaudeTaskProtocolSource {
    constructor(input = {}) {
        /** task_id -> canonical subagent id (the Task tool_use id). Populated by task_started. */
        this.subagentIdByTaskId = new Map();
        /** Every announced tool id -> the first tool id that publicly identifies the child. */
        this.canonicalIdByToolUseId = new Map();
        /** Tool calls made inside a sidechain, keyed to the direct child that emitted them. */
        this.ownerSubagentIdByToolUseId = new Map();
        /** Announced tasks inherit the owner recorded for their tool call, including local_bash. */
        this.ownerSubagentIdByTaskId = new Map();
        /**
         * Every subagent id this source declared. It is the source's whole vocabulary: an id that is
         * not in here was either filtered at declaration or never announced, and this source has
         * nothing to say about it.
         */
        this.declaredIds = new Set();
        /** Task ids declared specifically as workflows; only these may read workflow output files. */
        this.workflowTaskIds = new Set();
        /** Last result emitted per workflow task, so duplicate terminal notifications stay idempotent. */
        this.lastWorkflowResultByTaskId = new Map();
        /** Workflow invocations already own a real Workflow card in the parent timeline. */
        this.idsWithExistingParentToolCard = new Set();
        /**
         * Declared subagents that were moved to the background. They outlive the turn that spawned
         * them, so a turn ending is not evidence that they stopped.
         */
        this.backgroundedIds = new Set();
        /** Last status emitted per subagent, so a redundant announcement is not re-broadcast. */
        this.lastStatusById = new Map();
        /** Claude facts stay inside the provider boundary; clients receive one compact subtitle. */
        this.presentationById = new Map();
        this.lastSubtitleById = new Map();
        this.sawTaskStarted = false;
        this.sawAnyTask = false;
        this.getToolInput = input.getToolInput ?? (() => null);
        this.readWorkflowResult = input.readWorkflowResult ?? (() => undefined);
    }
    /**
     * True once this session has announced at least one task. Older Claude Code releases predate
     * the task protocol; callers use this to decide whether the legacy derivation still has work
     * to do, rather than assuming a version.
     */
    get isActive() {
        return this.sawTaskStarted;
    }
    /**
     * True once this session announced any task at all, including the ones filtered out.
     *
     * Deliberately not `isActive`: that one answers "does a declarative owner exist for subagent
     * descriptors", and stays false in a session whose only tasks were rejected. This one answers
     * "does this CLI announce its tasks" — and once it does, every task is announced, so a frame
     * for an id that was never declared is provably work the filter refused rather than something
     * this source has yet to hear about.
     */
    get announcesTasks() {
        return this.sawAnyTask;
    }
    /**
     * Whether this source declared the given subagent. Callers route frames through this before
     * attributing anything to an id: a frame for a task that was never declared belongs to work
     * the declaration filter already rejected.
     */
    isDeclared(subagentId) {
        return this.declaredIds.has(subagentId);
    }
    /** Resolve a Claude tool id to the provider descriptor id that owns its state and timeline. */
    resolveSubagentId(toolUseId) {
        const canonicalId = this.canonicalIdByToolUseId.get(toolUseId);
        return canonicalId && this.declaredIds.has(canonicalId) ? canonicalId : undefined;
    }
    /** Whether Claude's task protocol declared this task as a provider subagent. */
    isDeclaredTask(taskId) {
        const subagentId = this.subagentIdByTaskId.get(taskId);
        return subagentId !== undefined && this.declaredIds.has(subagentId);
    }
    /** Resolve a non-subagent task (for example local_bash) to its emitting sidechain. */
    resolveTaskOwner(taskId, toolUseId) {
        return (this.ownerSubagentIdByTaskId.get(taskId) ??
            (toolUseId ? this.ownerSubagentIdByToolUseId.get(toolUseId) : undefined));
    }
    needsSyntheticParentToolCard(subagentId) {
        return !this.idsWithExistingParentToolCard.has(subagentId);
    }
    observe(message) {
        if (message.type !== "system")
            return [];
        switch (message.subtype) {
            case "task_started":
                return this.observeTaskStarted(message);
            case "task_updated":
                return this.observeTaskUpdated(message);
            case "task_notification":
                return this.observeTaskNotification(message);
            case "task_progress":
                return this.observeTaskProgress(message);
            default:
                return [];
        }
    }
    /**
     * Forget the session.
     *
     * Task ids are session-scoped and outlive any single turn, so this belongs to session teardown
     * alone. Clearing it when a turn ends would drop the routing table while its tasks were still
     * live — see `cancelRunningForegroundTasks`, which is what a turn ending actually warrants.
     */
    reset() {
        this.subagentIdByTaskId.clear();
        this.canonicalIdByToolUseId.clear();
        this.ownerSubagentIdByToolUseId.clear();
        this.ownerSubagentIdByTaskId.clear();
        this.declaredIds.clear();
        this.workflowTaskIds.clear();
        this.lastWorkflowResultByTaskId.clear();
        this.idsWithExistingParentToolCard.clear();
        this.backgroundedIds.clear();
        this.lastStatusById.clear();
        this.presentationById.clear();
        this.lastSubtitleById.clear();
        this.sawTaskStarted = false;
        this.sawAnyTask = false;
    }
    /**
     * Terminalize the subagents that a canceled turn was running.
     *
     * Cancellation is a fact about the turn, not about the task table: the session, and every id in
     * it, outlives the turn. So this reports statuses and leaves the routing intact — that is what
     * lets a child which settles after the interrupt still find its descriptor, and what keeps a
     * later `task_notification` free to correct this guess.
     *
     * Backgrounded children are skipped. Being backgrounded is precisely the declaration that they
     * outlive the turn; everything else was running in the foreground and died with it.
     */
    cancelRunningForegroundTasks() {
        const observations = [];
        for (const id of this.declaredIds) {
            if (this.backgroundedIds.has(id))
                continue;
            if (this.lastStatusById.get(id) !== "running")
                continue;
            this.lastStatusById.set(id, "canceled");
            observations.push({ kind: "status", id, status: "canceled" });
        }
        return observations;
    }
    /** A lost Claude process terminates every task it owned, including backgrounded workflows. */
    failRunningTasks() {
        const observations = [];
        for (const id of this.declaredIds) {
            if (this.lastStatusById.get(id) !== "running")
                continue;
            this.lastStatusById.set(id, "failed");
            observations.push({ kind: "status", id, status: "failed" });
        }
        return observations;
    }
    observeTaskStarted(message) {
        // Recorded before the filter: what this proves is that the CLI announces its tasks, which is
        // true whether or not this particular one is a subagent.
        this.sawAnyTask = true;
        const id = readString(message.tool_use_id);
        const parentSubagentId = id ? this.ownerSubagentIdByToolUseId.get(id) : undefined;
        if (parentSubagentId)
            this.ownerSubagentIdByTaskId.set(message.task_id, parentSubagentId);
        // skip_transcript marks ambient housekeeping the transcript should not show.
        if (!id || message.skip_transcript === true || !isProviderSubagentTask(message))
            return [];
        this.sawTaskStarted = true;
        const existingId = this.subagentIdByTaskId.get(message.task_id);
        if (existingId) {
            return this.observeExistingTaskStart(message, id, existingId);
        }
        return this.observeNewTaskStart(message, id, parentSubagentId);
    }
    observeExistingTaskStart(message, toolUseId, existingId) {
        this.canonicalIdByToolUseId.set(toolUseId, existingId);
        const observations = [];
        if (this.lastStatusById.get(existingId) !== "running") {
            this.lastStatusById.set(existingId, "running");
            observations.push({ kind: "status", id: existingId, status: "running" });
        }
        const prompt = message.task_type === CLAUDE_WORKFLOW_TASK_TYPE
            ? readString(message.description)
            : readString(message.prompt);
        if (prompt) {
            observations.push({
                kind: "timeline",
                id: existingId,
                item: { type: "user_message", text: prompt },
            });
        }
        return observations;
    }
    observeNewTaskStart(message, id, parentSubagentId) {
        this.subagentIdByTaskId.set(message.task_id, id);
        this.canonicalIdByToolUseId.set(id, id);
        this.declaredIds.add(id);
        this.lastStatusById.set(id, "running");
        // An explicit `name` on the Task call wins over the agent type, matching how replay titles the
        // same subagent. Without it a fan-out of five Explores reads as five identical rows.
        const isWorkflow = message.task_type === CLAUDE_WORKFLOW_TASK_TYPE;
        if (isWorkflow || parentSubagentId) {
            this.idsWithExistingParentToolCard.add(id);
        }
        if (isWorkflow) {
            this.workflowTaskIds.add(message.task_id);
        }
        const title = isWorkflow
            ? "Workflow"
            : (readString(this.getToolInput(id)?.name) ?? readString(message.subagent_type));
        const description = readString(message.description);
        const observations = [
            {
                kind: "declared",
                id,
                toolCallId: id,
                ...(title ? { title } : {}),
                ...(description ? { description } : {}),
                ...(parentSubagentId ? { parentSubagentId } : {}),
            },
        ];
        const initialPresentation = title ? { title } : {};
        this.presentationById.set(id, initialPresentation);
        const initialSubtitle = buildClaudeSubagentSubtitle(initialPresentation);
        if (initialSubtitle)
            this.lastSubtitleById.set(id, initialSubtitle);
        // Open the provider-subagent timeline with the task it was actually given. Without this the
        // pane starts mid-conversation, showing replies to a question the reader never sees.
        // A workflow's prompt is its JavaScript source, not a user-authored task. Open its generic
        // timeline with Claude's summary instead, so the pane is meaningful without exposing source.
        const prompt = isWorkflow ? description : readString(message.prompt);
        if (prompt) {
            observations.push({ kind: "timeline", id, item: { type: "user_message", text: prompt } });
        }
        return observations;
    }
    observeTaskUpdated(message) {
        const id = this.subagentIdByTaskId.get(message.task_id);
        const backgrounded = message.patch?.is_backgrounded;
        if (id && typeof backgrounded === "boolean") {
            if (backgrounded)
                this.backgroundedIds.add(id);
            else
                this.backgroundedIds.delete(id);
        }
        return this.observeStatus(message.task_id, message.patch?.status);
    }
    observeTaskNotification(message) {
        const observations = this.observeWorkflowResult(message);
        observations.push(...this.observeUsage(message.task_id, message.usage));
        observations.push(...this.observeStatus(message.task_id, message.status));
        return observations;
    }
    observeWorkflowResult(message) {
        if (!this.workflowTaskIds.has(message.task_id))
            return [];
        const id = this.subagentIdByTaskId.get(message.task_id);
        const outputFile = readString(message.output_file);
        if (!id || !outputFile)
            return [];
        const text = this.readWorkflowResult(outputFile);
        if (!text || this.lastWorkflowResultByTaskId.get(message.task_id) === text)
            return [];
        this.lastWorkflowResultByTaskId.set(message.task_id, text);
        return [{ kind: "timeline", id, item: { type: "assistant_message", text } }];
    }
    /**
     * `task_progress` fires once per tool use, not on a timer, so this is an accurate live cost
     * signal rather than a sampled one. It matters most for backgrounded subagents, which emit no
     * sidechain frames at all and would otherwise show no activity while they work.
     */
    observeTaskProgress(message) {
        return this.observeUsage(message.task_id, message.usage);
    }
    observeUsage(taskId, raw) {
        const id = this.subagentIdByTaskId.get(taskId);
        const usage = readUsage(raw);
        if (!id || !usage)
            return [];
        return this.updatePresentation(id, { usage });
    }
    /**
     * Effort as reported by a hook firing inside a subagent.
     *
     * This is the only live source: the message stream carries no effort at any depth, and the
     * value Paseo requested is not necessarily the one that ran, because a model that does not
     * support the requested level is silently downgraded. Hooks report the post-downgrade level.
     *
     * `agent_id` is the same id `task_started` calls `task_id`, so it routes through the table
     * already built at declaration — no second identity to reconcile.
     */
    observeHook(input) {
        const taskId = readString(input.agent_id);
        const effort = readString(input.effort?.level);
        if (!taskId || !effort)
            return [];
        const id = this.subagentIdByTaskId.get(taskId);
        if (!id)
            return [];
        return this.updatePresentation(id, { effort });
    }
    /**
     * The model the child is actually running, read off its own assistant frames.
     *
     * Routed through the declaration for the same reason status is: a frame carrying the tool_use
     * id of a task this source filtered out — ambient housekeeping or a workflow child — would
     * otherwise fold to an upsert with no
     * identity and a defaulted "running" status. That is the nameless, never-finishing row the
     * declaration filter exists to prevent, arriving by a different door.
     */
    observeSidechainFrame(message, subagentId) {
        if (message.type !== "assistant" || !this.declaredIds.has(subagentId))
            return [];
        const content = message.message?.content;
        if (Array.isArray(content)) {
            for (const block of content) {
                if (!block || typeof block !== "object")
                    continue;
                const record = block;
                if (record.type === "tool_use" && typeof record.id === "string") {
                    this.ownerSubagentIdByToolUseId.set(record.id, subagentId);
                }
            }
        }
        const model = resolveObservedClaudeModelId(typeof message.message?.model === "string" ? message.message.model : undefined);
        if (!model)
            return [];
        return this.updatePresentation(subagentId, { model });
    }
    updatePresentation(id, patch) {
        const previous = this.presentationById.get(id) ?? {};
        const next = { ...previous, ...patch };
        this.presentationById.set(id, next);
        const subtitle = buildClaudeSubagentSubtitle(next);
        if (!subtitle || this.lastSubtitleById.get(id) === subtitle)
            return [];
        this.lastSubtitleById.set(id, subtitle);
        return [{ kind: "subtitle", id, subtitle }];
    }
    /**
     * Status is routed only through a task this source declared.
     *
     * `task_notification` also fires for the tasks deliberately filtered above, and it carries a
     * `tool_use_id`. Falling back to that id would readmit exactly what the filter rejected — as a
     * descriptor holding a status and no identity, which the track renders as a nameless row.
     * A status without a declaration describes nothing, so it is dropped.
     */
    observeStatus(taskId, rawStatus) {
        const id = this.subagentIdByTaskId.get(taskId);
        if (!id)
            return [];
        const status = mapTaskStatus(rawStatus);
        if (!status || this.lastStatusById.get(id) === status)
            return [];
        this.lastStatusById.set(id, status);
        return [{ kind: "status", id, status }];
    }
}
