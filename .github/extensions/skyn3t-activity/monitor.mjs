import { constants } from "node:fs";
import { lstat, mkdir, open, realpath } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { isAbsolute, join } from "node:path";
import { LIMITS, RecordDecoder, TERMINAL_EVENTS, duration, eventLine } from "./records.mjs";

const ERRORS = Object.freeze({
    workspace_unavailable: "The current session has no usable private workspacePath.",
    unsafe_directory: "The activity directory is not a private, stable directory.",
    unsafe_file: "The activity path is not a regular, single-link, non-symlink file.",
    file_replaced: "The activity file was removed or replaced.",
    file_truncated: "The activity file was truncated or rewritten.",
    file_too_large: "The activity file exceeded the 32 MiB observation limit.",
    file_unavailable: "The activity file could not be opened or read safely.",
    timeline_unavailable: "The native activity timeline could not be updated.",
    close_failed: "The activity file handle could not be closed.",
    monitor_limit: "The session activity monitor limit has been reached.",
    unknown_monitor: "Unknown monitor_id; use an ID returned by skyn3t_activity_watch.",
    invalid_arguments: "Invalid arguments; only the declared tool fields are accepted.",
    shutting_down: "The activity observer is shutting down.",
    not_ready: "The activity observer has not joined the session yet.",
});

export class ObservationError extends Error {
    constructor(code) {
        super(ERRORS[code]);
        this.code = code;
    }
}

function sameFile(a, b) {
    return a.dev === b.dev && a.ino === b.ino;
}

async function stat(path) {
    return lstat(path, { bigint: true });
}

function validDirectory(info, privateOnly = false) {
    return info.isDirectory() && !info.isSymbolicLink()
        && (!privateOnly || ((info.mode & 0o077n) === 0n
            && (typeof process.getuid !== "function" || info.uid === BigInt(process.getuid()))));
}

async function prepareLocation(workspacePath) {
    if (typeof workspacePath !== "string" || !isAbsolute(workspacePath)) {
        throw new ObservationError("workspace_unavailable");
    }
    try {
        if (!validDirectory(await stat(workspacePath))) throw new ObservationError("unsafe_directory");
        const workspace = await realpath(workspacePath);
        const parents = [{ path: workspace, info: await stat(workspace) }];
        for (const [name, privateOnly] of [["files", false], ["skyn3t-activity", true]]) {
            const path = join(parents.at(-1).path, name);
            try {
                await mkdir(path, { mode: 0o700 });
            } catch (error) {
                if (error.code !== "EEXIST") throw error;
            }
            const info = await stat(path);
            if (!validDirectory(info, privateOnly)) throw new ObservationError("unsafe_directory");
            parents.push({ path, info, privateOnly });
        }
        return parents;
    } catch (error) {
        if (error instanceof ObservationError) throw error;
        throw new ObservationError("workspace_unavailable");
    }
}

export class ActivityMonitor {
    constructor({ monitorId, activityFile, parents, log, diagnostic, now = Date.now, autoPoll = true }) {
        this.id = monitorId;
        this.path = activityFile;
        this.parents = parents;
        this.log = log;
        this.diagnostic = diagnostic;
        this.now = now;
        this.autoPoll = autoPoll;
        this.createdAt = now();
        this.state = "waiting";
        this.active = true;
        this.latest = {};
        this.recent = [];
        this.decoder = new RecordDecoder();
        this.offset = 0;
        this.rejected = 0;
        this.accepted = 0;
        this.suppressed = 0;
        this.lastActivityLogAt = -Infinity;
        this.lastRejectionLogAt = -Infinity;
        this.lastLivenessAt = this.createdAt;
        this.#schedule(0);
    }

    #schedule(delay = LIMITS.pollMs) {
        if (!this.autoPoll || !this.active || this.timer) return;
        this.timer = setTimeout(() => {
            this.timer = undefined;
            void this.poll();
        }, delay);
        this.timer.unref();
    }

    poll() {
        if (this.inFlight) return this.inFlight;
        if (!this.active) return Promise.resolve();
        this.inFlight = this.#read()
            .catch((error) => this.#fail(error))
            .finally(async () => {
                if (!this.active) await this.#close();
                this.inFlight = undefined;
                this.#schedule();
            });
        return this.inFlight;
    }

    async #checkParents() {
        for (const parent of this.parents) {
            let info;
            try {
                info = await stat(parent.path);
            } catch {
                throw new ObservationError("unsafe_directory");
            }
            if (!validDirectory(info, parent.privateOnly) || !sameFile(info, parent.info)) {
                throw new ObservationError("unsafe_directory");
            }
        }
    }

    #checkFile(info) {
        if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1n) {
            throw new ObservationError("unsafe_file");
        }
        if (info.size > BigInt(LIMITS.fileBytes)) throw new ObservationError("file_too_large");
    }

    async #checkIdentity() {
        await this.#checkParents();
        let pathInfo;
        try {
            pathInfo = await stat(this.path);
        } catch {
            throw new ObservationError("file_replaced");
        }
        this.#checkFile(pathInfo);
        const info = await this.handle.stat({ bigint: true });
        this.#checkFile(info);
        if (!sameFile(info, this.identity) || !sameFile(pathInfo, this.identity)) {
            throw new ObservationError("file_replaced");
        }
        if (info.size < this.lastSize || info.size < BigInt(this.offset)
            || (this.lastInfo && info.size === this.lastInfo.size
                && (info.mtimeNs !== this.lastInfo.mtimeNs || info.ctimeNs !== this.lastInfo.ctimeNs))) {
            throw new ObservationError("file_truncated");
        }
        this.lastSize = info.size;
        return info;
    }

    async #read() {
        await this.#checkParents();
        if (!this.active) return;
        if (!this.handle) {
            let info;
            try {
                info = await stat(this.path);
            } catch (error) {
                if (error.code !== "ENOENT") throw error;
                await this.#liveness();
                return;
            }
            this.#checkFile(info);
            this.handle = await open(this.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
            this.identity = info;
            this.lastSize = info.size;
        }
        const info = await this.#checkIdentity();
        if (!this.active) return;

        // A bounded tail checkpoint also catches truncate-and-regrow between polls.
        if (this.anchor?.length) {
            const buffer = Buffer.alloc(this.anchor.length);
            const { bytesRead } = await this.handle.read(buffer, 0, buffer.length, this.offset - buffer.length);
            if (bytesRead !== buffer.length || !buffer.equals(this.anchor)) {
                throw new ObservationError("file_truncated");
            }
        }
        const end = Math.min(Number(info.size), this.offset + LIMITS.bytesPerPoll);
        while (this.active && this.offset < end) {
            const buffer = Buffer.alloc(Math.min(LIMITS.chunkBytes, end - this.offset));
            const { bytesRead } = await this.handle.read(buffer, 0, buffer.length, this.offset);
            if (bytesRead !== buffer.length) throw new ObservationError("file_truncated");
            await this.#checkIdentity();
            if (!this.active) return;
            this.offset += bytesRead;
            this.anchor = Buffer.concat([this.anchor ?? Buffer.alloc(0), buffer]).subarray(-256);
            for (const result of this.decoder.push(buffer)) {
                if (!this.active) return;
                if (result.error) await this.#reject();
                else await this.#observe(result.record);
            }
        }
        const after = await this.#checkIdentity();
        this.lastInfo = after;
        if (!this.active) return;
        await this.#flushActivity();
        if (this.outcome && this.offset === Number(after.size)) {
            await this.#flushActivity(true);
            if (this.decoder.pending.length || this.decoder.discarding) {
                await this.#reject();
            }
            this.#deactivate("terminal_record");
            return;
        }
        await this.#liveness();
    }

    async #emit(line, options = {}) {
        try {
            await this.log(line, options);
        } catch {
            throw new ObservationError("timeline_unavailable");
        }
    }

    async #reject() {
        this.rejected++;
        if (this.now() - this.lastRejectionLogAt >= LIMITS.rejectionMs) {
            this.lastRejectionLogAt = this.now();
            await this.#emit(`[SkyN3t ${this.id.slice(0, 4)}] Ignored an invalid or unsafe activity record; no raw content was displayed.`, { level: "warning" });
        }
    }

    async #observe(record) {
        if ((this.runId && record.run_id !== this.runId) || record.seq <= (this.seq ?? 0)) {
            await this.#reject();
            return;
        }
        this.runId = record.run_id;
        this.seq = record.seq;
        this.lastEventAt = this.now();
        this.lastEvent = record;
        this.accepted++;
        if (this.state === "waiting") this.state = "observing";
        const contextChanged = ["stage", "model", "provider"].some(
            (key) => record[key] !== undefined && record[key] !== this.latest[key],
        );
        for (const key of ["operation", "project", "stage", "model", "provider"]) {
            if (record[key] !== undefined) this.latest[key] = record[key];
        }
        if (record.event === "heartbeat") {
            this.lastHeartbeatAt = this.now();
            if (!contextChanged) return;
        }
        this.lastReportedAt = this.now();
        const displayRecord = record.event === "heartbeat"
            ? { ...record, message: "Reported stage/model/provider changed." }
            : record;
        const line = eventLine(this.id, displayRecord, this.latest);
        this.recent.push(line);
        if (this.recent.length > LIMITS.recentLines) this.recent.shift();

        if (record.event === "activity" && !contextChanged) {
            if (this.now() - this.lastActivityLogAt >= LIMITS.activityMs) {
                this.pendingActivity = undefined;
                await this.#emit(line);
                this.lastActivityLogAt = this.now();
            } else {
                this.pendingActivity = line;
                this.suppressed++;
            }
            return;
        }
        await this.#flushActivity(true);
        const level = record.event === "failed" ? "error"
            : ["warning", "interrupted"].includes(record.event) ? "warning" : "info";
        await this.#emit(line, { level });
        if (record.event === "activity") this.lastActivityLogAt = this.now();
        if (TERMINAL_EVENTS.has(record.event)) {
            this.outcome = record.event;
            this.state = record.event;
        }
    }

    async #flushActivity(force = false) {
        if (!this.pendingActivity || !this.active
            || (!force && this.now() - this.lastActivityLogAt < LIMITS.activityMs)) return;
        const line = this.pendingActivity;
        this.pendingActivity = undefined;
        await this.#emit(line);
        this.lastActivityLogAt = this.now();
    }

    async #liveness() {
        const now = this.now();
        const quietFor = now - (this.lastReportedAt ?? this.createdAt);
        if (!this.active || quietFor < LIMITS.staleMs
            || now - this.lastLivenessAt < LIMITS.livenessMs) return;
        this.lastLivenessAt = now;
        const message = this.lastEventAt === undefined
            ? "Waiting for reported activity; no SkyN3t start has been observed."
            : `No new reported activity for ${duration(quietFor / 1000)}; process outcome is unknown.`;
        await this.#emit(`[SkyN3t ${this.id.slice(0, 4)}] ${message}`, { ephemeral: true });
    }

    #deactivate(reason) {
        this.active = false;
        this.stopReason = reason;
        clearTimeout(this.timer);
        this.timer = undefined;
    }

    async #fail(error) {
        const code = error instanceof ObservationError ? error.code : "file_unavailable";
        this.error = { code, message: ERRORS[code] };
        this.state = "monitor_error";
        this.#deactivate("monitor_error");
        const message = `[SkyN3t ${this.id.slice(0, 4)}] Monitor error: ${ERRORS[code]} SkyN3t was not cancelled; use the Bash process result.`;
        if (code === "timeline_unavailable") {
            this.diagnostic(message);
            return;
        }
        try {
            await this.#emit(message, { level: "error" });
        } catch {
            this.diagnostic("SkyN3t activity monitor error could not be shown in the native timeline.");
        }
    }

    async #close() {
        const handle = this.handle;
        this.handle = undefined;
        this.decoder.clear();
        this.anchor = undefined;
        this.pendingActivity = undefined;
        if (!handle) return;
        try {
            await handle.close();
        } catch {
            await this.#fail(new ObservationError("close_failed"));
        }
    }

    async stop(reason = "feed_stopped") {
        if (this.active) {
            this.#deactivate(reason);
            if (!this.outcome) this.state = "stopped";
        }
        await this.inFlight;
        await this.#close();
        return this.status();
    }

    status() {
        return {
            monitor_id: this.id,
            activity_file: this.path,
            state: this.state,
            monitoring: this.active,
            observed_outcome: this.outcome ?? null,
            process_outcome: "Not observed by this extension; rely on the normal Bash process result.",
            run_id: this.runId ?? null,
            last_seq: this.seq ?? null,
            latest: { ...this.latest },
            last_event: this.lastEvent ? { ...this.lastEvent } : null,
            last_event_age_s: this.lastEventAt === undefined ? null : Math.max(0, (this.now() - this.lastEventAt) / 1000),
            last_heartbeat_age_s: this.lastHeartbeatAt === undefined ? null : Math.max(0, (this.now() - this.lastHeartbeatAt) / 1000),
            last_reported_activity_age_s: this.lastReportedAt === undefined ? null : Math.max(0, (this.now() - this.lastReportedAt) / 1000),
            accepted_records: this.accepted,
            rejected_records: this.rejected,
            throttled_activity_records: this.suppressed,
            recent_lines: [...this.recent],
            stop_reason: this.stopReason ?? null,
            error: this.error ? { ...this.error } : null,
        };
    }
}

export class ActivityObserver {
    constructor({ workspacePath, log, now, autoPoll = true, diagnostic = (message) => process.stderr.write(`${message}\n`) }) {
        this.workspacePath = workspacePath;
        this.options = { log, now, autoPoll, diagnostic };
        this.monitors = new Map();
    }

    #checkCapacity() {
        if (this.closed) throw new ObservationError("shutting_down");
        if (this.monitors.size >= LIMITS.totalMonitors
            || [...this.monitors.values()].filter((monitor) => monitor.active).length >= LIMITS.activeMonitors) {
            throw new ObservationError("monitor_limit");
        }
    }

    async watch() {
        this.#checkCapacity();
        const parents = await prepareLocation(this.workspacePath);
        const monitorId = randomUUID();
        const activityFile = join(parents.at(-1).path, `${monitorId}.jsonl`);
        try {
            await stat(activityFile);
            throw new ObservationError("unsafe_file");
        } catch (error) {
            if (error.code !== "ENOENT") throw error;
        }
        this.#checkCapacity();
        const monitor = new ActivityMonitor({
            ...this.options, monitorId, activityFile, parents,
        });
        this.monitors.set(monitorId, monitor);
        return {
            monitor_id: monitorId,
            activity_file: activityFile,
            state: "waiting",
            instruction: "Monitoring is ready only; no SkyN3t job has been started or completed. Start the normal SkyN3t CLI through Bash (studio improve or studio build) with the activity_flag_argv below, plus the usual run arguments. These are descriptive argv, not an executable shell command. Rely on the normal Bash process outcome as well as this feed.",
            activity_flag_argv: ["--activity-file", activityFile],
        };
    }

    get(monitorId) {
        const monitor = this.monitors.get(monitorId);
        if (!monitor) throw new ObservationError("unknown_monitor");
        return monitor;
    }

    async shutdown() {
        this.closed = true;
        await Promise.all([...this.monitors.values()].map((monitor) => monitor.stop("session_shutdown")));
    }
}
