import { stripVTControlCharacters } from "node:util";

export const LIMITS = Object.freeze({
    lineBytes: 32768,
    chunkBytes: 16384,
    bytesPerPoll: 65536,
    fileBytes: 32 * 1024 * 1024,
    recentLines: 32,
    activeMonitors: 8,
    totalMonitors: 64,
    pollMs: 500,
    activityMs: 1500,
    staleMs: 30000,
    livenessMs: 60000,
    rejectionMs: 30000,
});

export const TERMINAL_EVENTS = new Set(["completed", "failed", "interrupted"]);
const EVENTS = new Set([
    "started", "stage", "model", "activity", "heartbeat", "warning",
    ...TERMINAL_EVENTS,
]);
const TEXT_LIMITS = {
    run_id: 128, message: 512, project: 128, stage: 128,
    model: 128, provider: 128, tool: 128, path: 512,
};
const ALLOWED_FIELDS = new Set([
    "schema_version", "seq", "timestamp", "elapsed_s", "event", "operation",
    "changed_files", "proof_passed", ...Object.keys(TEXT_LIMITS),
]);
const CONTROL_TEXT = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/gu;
const SENSITIVE_TEXT = /(?:\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bBearer\s+\S+|\b(?:api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|token|password|passwd|secret|authorization|cookie|set-cookie)\s*[:=]\s*\S+|https?:\/\/[^\s/]+:[^\s/]+@|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b)/iu;

export function safeText(value) {
    return stripVTControlCharacters(value).replace(CONTROL_TEXT, "").trim();
}

export function validateRecord(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return { error: "invalid_schema" };
    }
    if (Object.keys(value).some((key) => !ALLOWED_FIELDS.has(key))) {
        return { error: "unexpected_fields" };
    }
    if (value.schema_version !== 1 || !EVENTS.has(value.event)
        || !Number.isSafeInteger(value.seq) || value.seq <= 0
        || !Number.isFinite(value.timestamp) || value.timestamp < 0
        || !Number.isFinite(value.elapsed_s) || value.elapsed_s < 0
        || value.elapsed_s > Number.MAX_SAFE_INTEGER) {
        return { error: "invalid_schema" };
    }
    const record = {
        schema_version: 1, event: value.event, seq: value.seq,
        timestamp: value.timestamp, elapsed_s: value.elapsed_s,
    };
    for (const [key, limit] of Object.entries(TEXT_LIMITS)) {
        if (value[key] === undefined && key !== "run_id" && key !== "message") continue;
        if (typeof value[key] !== "string" || [...value[key]].length > limit) {
            return { error: "invalid_text" };
        }
        const text = safeText(value[key]);
        if (!text || SENSITIVE_TEXT.test(text)) return { error: "unsafe_text" };
        record[key] = text;
    }
    if (record.run_id !== value.run_id || !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/u.test(record.run_id)) {
        return { error: "invalid_identity" };
    }
    if (record.path !== undefined
        && (record.path.startsWith("/") || record.path.includes("\\")
            || record.path.includes(":")
            || record.path.split("/").some((part) => !part || part === "." || part === ".."))) {
        return { error: "invalid_path" };
    }
    if (value.operation !== undefined) {
        if (!["build", "improve"].includes(value.operation)) return { error: "invalid_schema" };
        record.operation = value.operation;
    }
    if (value.changed_files !== undefined) {
        if (!Number.isSafeInteger(value.changed_files) || value.changed_files < 0) {
            return { error: "invalid_schema" };
        }
        record.changed_files = value.changed_files;
    }
    if (value.proof_passed !== undefined) {
        if (typeof value.proof_passed !== "boolean") return { error: "invalid_schema" };
        record.proof_passed = value.proof_passed;
    }
    return { record };
}

// Frame bytes before decoding: a UTF-8 character can span any number of reads.
export class RecordDecoder {
    constructor() {
        this.pending = Buffer.alloc(0);
        this.discarding = false;
        this.decoder = new TextDecoder("utf-8", { fatal: true });
    }

    push(chunk) {
        const results = [];
        let start = 0;
        while (start < chunk.length) {
            const newline = chunk.indexOf(10, start);
            const end = newline === -1 ? chunk.length : newline;
            const part = chunk.subarray(start, end);
            if (!this.discarding) {
                if (this.pending.length + part.length > LIMITS.lineBytes) {
                    this.pending = Buffer.alloc(0);
                    this.discarding = true;
                    results.push({ error: "oversized_line" });
                } else {
                    this.pending = Buffer.concat([this.pending, part]);
                }
            }
            if (newline === -1) break;
            if (!this.discarding) results.push(this.#decode());
            this.pending = Buffer.alloc(0);
            this.discarding = false;
            start = newline + 1;
        }
        return results;
    }

    #decode() {
        let text;
        try {
            text = this.decoder.decode(this.pending);
        } catch {
            return { error: "invalid_utf8" };
        }
        let value;
        try {
            value = JSON.parse(text);
        } catch {
            return { error: "invalid_json" };
        }
        return validateRecord(value);
    }

    clear() {
        this.pending = Buffer.alloc(0);
        this.discarding = false;
    }
}

export function duration(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

const STAGE_LABELS = Object.freeze({
    initializing: "starting up",
    localize: "reading the project",
    generating: "updating files",
    prepare_dependencies: "preparing required packages",
    proof: "checking the changes",
    verifying: "checking the changes",
    repairing: "fixing problems",
    finalizing: "getting changes ready for checks",
    delivering: "saving checked changes",
});

export function stageLabel(stage) {
    return Object.hasOwn(STAGE_LABELS, stage) ? STAGE_LABELS[stage] : stage;
}

export function isToolAcknowledgement(record) {
    const tool = record.tool?.split(".").at(-1).toLowerCase();
    return record.event === "activity"
        && ["read", "read_file", "view", "edit", "edit_file", "apply_patch",
            "write", "write_file", "write_files", "create", "create_file",
            "list_files", "search_files", "find_files", "finish"].includes(tool)
        && (record.message === `Finished ${record.tool}` || record.message === `Finished ${tool}`);
}

export function isEditingFinished(record) {
    return record.event === "activity" && record.tool?.split(".").at(-1).toLowerCase() === "finish"
        && (record.message === `Using ${record.tool}` || record.message === "Using finish");
}

function friendlyMessage(record) {
    const retry = /^Retry decision: .+ \(([^)]+)\), attempt (\d+), delay (\d+(?:\.\d+)?s), outcome (\w+)$/u.exec(record.message);
    if (record.event === "warning" && retry) {
        const [, reason, attempt, delay, outcome] = retry;
        const cause = reason === "http_429" ? "The AI service is busy (request limit reached)"
            : reason === "no_write_progress" ? "The AI has not made file changes" : null;
        if (!cause) return record.message;
        if (outcome === "waiting") return `${cause}; trying again in ${delay} (attempt ${attempt}).`;
        if (outcome === "exhausted") return `${cause}. Automatic retries stopped; waiting for SkyN3t's next step.`;
        if (outcome === "fatal") return `${cause}. This request cannot be retried; waiting for the final result.`;
        if (outcome === "model_failover") return `${cause}. SkyN3t is trying another AI model.`;
    }
    const failure = /^Tool failed: ([\w.]+)(?:: (.+))?$/u.exec(record.message);
    if (record.event === "warning" && failure) {
        const tool = (record.tool ?? failure[1]).split(".").at(-1).toLowerCase();
        const action = ["edit", "edit_file", "apply_patch"].includes(tool) ? "apply an edit"
            : ["read", "read_file", "view"].includes(tool) ? "read a file"
            : ["write", "write_file", "write_files", "create", "create_file"].includes(tool) ? "save a file"
            : "complete this step";
        return `Could not ${action}${record.path ? `: ${record.path}` : ""}. The overall result is not known from this step alone.`
            + (failure[2] ? ` Details: ${failure[2]}` : "");
    }
    if (record.event === "warning") return record.message;
    if (isEditingFinished(record)) {
        return "The AI has finished editing; checks come next.";
    }
    if (record.message.startsWith("Agent started: ")) return "The AI is starting work.";
    if (record.message === "Reported stage/model/provider changed.") return "Work status updated.";
    const messages = {
        "Reading project context": "Reading the project to understand what needs changing.",
        "Codegen route selected": "AI model selected.",
        "Generating changes": "The AI is updating files.",
        "Preparing dependencies": "Preparing the packages the project needs.",
        "Verifying the candidate": "Checking that the changes work.",
        "Repairing the candidate": "Fixing problems found in the changes.",
        "Finalizing generated changes": "Getting the changes ready to check.",
        "Delivering verified files": "Saving the checked changes.",
        "Waiting for operator approval": "Waiting for your approval before continuing.",
        "SkyN3t run completed": "SkyN3t reports the work is complete.",
        "SkyN3t run failed; see the CLI outcome": "SkyN3t could not finish. The command result has the reason.",
    };
    return Object.hasOwn(messages, record.message) ? messages[record.message] : record.message;
}

export function eventLine(monitorId, record, latest, { showModel = record.event === "model" } = {}) {
    const prefix = `[SkyN3t ${monitorId.slice(0, 4)} | ${duration(record.elapsed_s)}]`;
    const stage = stageLabel(latest.stage)
        ?? (latest.operation === "improve" ? "Improving" : latest.operation === "build" ? "Building" : null);
    const model = showModel && latest.model
        ? `${latest.model}${latest.provider ? ` (${latest.provider})` : ""}`
        : showModel ? latest.provider : null;
    const kind = TERMINAL_EVENTS.has(record.event) || record.event === "warning"
        ? `${record.event[0].toUpperCase()}${record.event.slice(1)}: `
        : "";
    const details = [];
    if (record.changed_files !== undefined) details.push(`files changed: ${record.changed_files}`);
    if (record.proof_passed !== undefined) details.push(`checks: ${record.proof_passed ? "passed" : "failed"}`);
    return `${prefix} ${[stage, model].filter(Boolean).join(" - ")}${stage || model ? ": " : ""}`
        + `${kind}${friendlyMessage(record)}${details.length ? ` (${details.join(", ")})` : ""}`;
}
