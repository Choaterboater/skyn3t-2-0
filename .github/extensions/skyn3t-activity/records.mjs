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

export function eventLine(monitorId, record, latest) {
    const prefix = `[SkyN3t ${monitorId.slice(0, 4)} | ${duration(record.elapsed_s)}]`;
    const stage = latest.stage
        ?? (latest.operation === "improve" ? "Improving" : latest.operation === "build" ? "Building" : null);
    const model = latest.model
        ? `${latest.model}${latest.provider ? ` (${latest.provider})` : ""}`
        : latest.provider;
    const kind = TERMINAL_EVENTS.has(record.event) || record.event === "warning"
        ? `${record.event[0].toUpperCase()}${record.event.slice(1)}: `
        : "";
    const details = [];
    if (record.changed_files !== undefined) details.push(`changed files: ${record.changed_files}`);
    if (record.proof_passed !== undefined) details.push(`proof: ${record.proof_passed ? "passed" : "failed"}`);
    return [prefix, ...[stage, model].filter(Boolean)].join(" | ")
        + ` | ${kind}${record.message}${details.length ? ` (${details.join(", ")})` : ""}`;
}
