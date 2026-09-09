import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { appendFile, link, lstat, mkdir, mkdtemp, readFile, rename, rm, symlink, truncate, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { ActivityObserver, ObservationError } from "./monitor.mjs";
import { LIMITS, RecordDecoder, validateRecord } from "./records.mjs";
import { attachLifecycle, createActivityTools } from "./tools.mjs";

function record(seq = 1, event = "activity", extra = {}) {
    return {
        schema_version: 1, run_id: "synthetic-run", seq, timestamp: 1700000000 + seq,
        elapsed_s: seq, event, message: `Reported activity ${seq}`, ...extra,
    };
}

function jsonl(...records) {
    return records.map((value) => JSON.stringify(value)).join("\n") + "\n";
}

async function fixture(t, options = {}) {
    const workspacePath = await mkdtemp(join(tmpdir(), "skyn3t-activity-test-"));
    const logs = [];
    const diagnostics = [];
    let time = 0;
    const observer = new ActivityObserver({
        workspacePath, autoPoll: false, now: () => time,
        log: async (message, logOptions) => logs.push({ message, ...logOptions }),
        diagnostic: (message) => diagnostics.push(message),
        ...options,
    });
    t.after(async () => {
        await observer.shutdown();
        await rm(workspacePath, { recursive: true });
    });
    return {
        observer, workspacePath, logs, diagnostics,
        advance(ms) { time += ms; },
        async watch() {
            const allocation = await observer.watch();
            return { ...allocation, monitor: observer.get(allocation.monitor_id) };
        },
    };
}

async function waitFor(predicate) {
    const deadline = Date.now() + 4000;
    while (!predicate()) {
        assert.ok(Date.now() < deadline, "Expected automatic activity observation");
        await delay(20);
    }
}

test("watch allocates unique private future files without starting or creating a job", async (t) => {
    const f = await fixture(t);
    const first = await f.watch();
    const second = await f.watch();
    assert.notEqual(first.monitor_id, second.monitor_id);
    assert.notEqual(first.activity_file, second.activity_file);
    assert.equal(first.state, "waiting");
    assert.deepEqual(first.activity_flag_argv, ["--activity-file", first.activity_file]);
    assert.match(first.instruction, /no SkyN3t job has been started or completed/);
    assert.match(first.activity_file, /\/files\/skyn3t-activity\/[a-f0-9-]+\.jsonl$/u);
    assert.equal((await lstat(dirname(first.activity_file))).mode & 0o777, 0o700);
    await assert.rejects(lstat(first.activity_file), { code: "ENOENT" });
    await first.monitor.poll();
    assert.equal(first.monitor.status().state, "waiting");
    assert.equal(first.monitor.status().last_event_age_s, null);
    assert.equal(f.logs.length, 0);
});

test("workspace absence and non-private or symlinked directories reject clearly", async (t) => {
    const f = await fixture(t);
    for (const workspacePath of [undefined, "", "relative", join(f.workspacePath, "missing")]) {
        const observer = new ActivityObserver({ workspacePath, log: async () => {} });
        await assert.rejects(observer.watch(), { code: "workspace_unavailable" });
    }
    const files = join(f.workspacePath, "files");
    await mkdir(files, { mode: 0o700 });
    const publicDirectory = join(files, "skyn3t-activity");
    await mkdir(publicDirectory, { mode: 0o755 });
    await assert.rejects(f.observer.watch(), { code: "unsafe_directory" });
    await rm(publicDirectory, { recursive: true });
    await symlink(f.workspacePath, publicDirectory);
    await assert.rejects(f.observer.watch(), { code: "unsafe_directory" });
});

test("tools return structured JSON, reject paths/unknown IDs, and stop only the feed", async (t) => {
    const f = await fixture(t);
    const [watch, status, stop] = createActivityTools(() => f.observer);
    const allocationResult = await watch.handler();
    assert.equal(allocationResult.resultType, "success");
    const allocation = JSON.parse(allocationResult.textResultForLlm);
    assert.equal(allocation.state, "waiting");
    for (const [tool, args] of [
        [watch, { path: "/unrelated" }], [watch, { label: "not-supported" }],
        [status, { monitor_id: allocation.monitor_id, activity_file: "/unrelated" }],
        [status, { monitor_id: "/unrelated" }], [stop, {}], [watch, null],
    ]) {
        assert.equal((await tool.handler(args)).resultType, "rejected");
    }
    const unknown = await status.handler({ monitor_id: "00000000-0000-0000-0000-000000000000" });
    assert.equal(JSON.parse(unknown.textResultForLlm).error, "unknown_monitor");
    const stopped = JSON.parse((await stop.handler({ monitor_id: allocation.monitor_id })).textResultForLlm);
    assert.equal(stopped.state, "stopped");
    assert.match(stopped.message, /does not cancel SkyN3t/);
    const snapshot = JSON.parse((await status.handler({ monitor_id: allocation.monitor_id })).textResultForLlm);
    assert.equal(snapshot.monitoring, false);
    assert.equal(snapshot.process_outcome, stopped.process_outcome);
});

test("automatic native logging occurs before the synthetic producer completes", async (t) => {
    const f = await fixture(t, { autoPoll: true, now: Date.now });
    const { monitor, activity_file } = await f.watch();
    assert.equal(monitor.timer.hasRef(), false);
    await monitor.poll();
    assert.equal(monitor.state, "waiting");
    await writeFile(activity_file, jsonl(record(1, "started", { operation: "improve" })), { flag: "wx", mode: 0o600 });
    await waitFor(() => f.logs.some((entry) => entry.message.includes("Improving")));
    assert.equal(monitor.status().state, "observing");
    assert.equal(monitor.status().observed_outcome, null);
    assert.equal(monitor.active, true);
    await appendFile(activity_file, jsonl(record(2, "completed", { message: "Proof finished" })));
    await waitFor(() => !monitor.active);
    assert.equal(monitor.status().state, "completed");
    assert.equal(monitor.handle, undefined);
    assert.equal(monitor.timer, undefined);
});

test("queued and partial lines retain order, stage/model context and terminal result", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    const stage = jsonl(record(2, "stage", { stage: "Improving", message: "Starting edit pass" }));
    await writeFile(activity_file, jsonl(record(1, "started")) + stage.slice(0, 70), { flag: "wx" });
    await monitor.poll();
    assert.equal(monitor.status().accepted_records, 1);
    assert.equal(monitor.active, true);
    f.advance(17000);
    await appendFile(activity_file, stage.slice(70) + jsonl(
        record(3, "model", { model: "gpt-6-astra", provider: "copilot" }),
        record(4, "activity", { elapsed_s: 18, message: "Read src/MapWorkspace.tsx", tool: "read", path: "src/MapWorkspace.tsx" }),
        record(5, "completed", { changed_files: 2, proof_passed: true }),
    ));
    await monitor.poll();
    const status = monitor.status();
    assert.equal(status.state, "completed");
    assert.equal(status.monitoring, false);
    assert.equal(status.accepted_records, 5);
    assert.deepEqual(status.latest, { stage: "Improving", model: "gpt-6-astra", provider: "copilot" });
    assert.match(f.logs[3].message, /\| 00:18\].*Improving.*gpt-6-astra.*Read src\/MapWorkspace\.tsx/u);
    assert.match(f.logs.at(-1).message, /Completed:.*changed files: 2, proof: passed/u);
    f.advance(5000);
    assert.equal(monitor.status().last_event_age_s, 5);
    assert.equal(monitor.decoder.pending.length, 0);
});

test("byte framing handles every UTF-8 boundary and rejects invalid UTF-8", () => {
    const decoder = new RecordDecoder();
    const original = record(1, "stage", { message: "Read caf\u00e9/\u6587\u4ef6-\ud83d\ude80.ts" });
    const results = [];
    const bytes = Buffer.from(jsonl(original));
    for (let i = 0; i < bytes.length; i++) {
        results.push(...decoder.push(bytes.subarray(i, i + 1)));
    }
    assert.deepEqual(results, [{ record: original }]);
    assert.equal(decoder.pending.length, 0);
    assert.deepEqual(decoder.push(Buffer.from([0xff, 10])), [{ error: "invalid_utf8" }]);
    assert.deepEqual(decoder.push(Buffer.from(jsonl(record(2)))), [{ record: record(2) }]);
});

test("filesystem chunk boundaries do not corrupt multibyte or partial UTF-8", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    const bytes = Buffer.from(jsonl(record(1, "stage", { message: "Inspect caf\u00e9.ts" })));
    const split = bytes.indexOf(Buffer.from("\u00e9")) + 1;
    await writeFile(activity_file, bytes.subarray(0, split), { flag: "wx" });
    await monitor.poll();
    assert.equal(monitor.status().accepted_records, 0);
    await appendFile(activity_file, bytes.subarray(split));
    await monitor.poll();
    assert.equal(monitor.status().last_event.message, "Inspect caf\u00e9.ts");
});

test("schema rejects raw payloads, sensitive values, wrong types and unsafe paths", () => {
    const disallowed = [
        null, [], "text", record(1, "unknown"), record(0), record(1.1),
        record(1, "stage", { schema_version: 2 }), record(1, "stage", { elapsed_s: -1 }),
        record(1, "stage", { timestamp: Infinity }), record(1, "stage", { run_id: "" }),
        record(1, "stage", { run_id: "synthetic-\u0000run" }),
        record(1, "stage", { operation: "deploy" }), record(1, "stage", { changed_files: -1 }),
        record(1, "stage", { proof_passed: "yes" }), record(1, "stage", { stage: {} }),
        record(1, "stage", { message: "x".repeat(513) }),
        ...["prompt", "source", "arguments", "result", "assistant_text", "reasoning", "__proto__"].map(
            (key) => ({ ...record(), [key]: "synthetic raw payload" }),
        ),
        ...["api_key=synthetic-value", "Bearer synthetic-value", "ghp_1234567890abcdefghijkl",
            "sk-proj-abcdefghijklmnop", "password: synthetic-value", "-----BEGIN PRIVATE KEY-----",
            "client_secret=synthetic", "Authorization: Basic synthetic", "Cookie: session=synthetic",
            "https://user:synthetic-password@example.test", "ghp_\u00001234567890abcdefghijkl"].map(
            (message) => record(1, "stage", { message }),
        ),
        ...["/etc/example", "../example", "src/../../example", "C:\\example", "src//example", "./example"].map(
            (path) => record(1, "activity", { path }),
        ),
    ];
    for (const value of disallowed) assert.ok(validateRecord(value).error);
    for (const field of ["stage", "model", "provider", "project", "tool", "path", "run_id"]) {
        assert.ok(validateRecord(record(1, "stage", { [field]: "api_key=synthetic" })).error);
    }
    const safe = validateRecord(record(1, "stage", {
        message: "\u001b[31mRead\u001b[0m src/a.ts\n\u202e", stage: "Inspect",
    }));
    assert.equal(safe.record.message, "Read src/a.ts");
    assert.equal(safe.record.stage, "Inspect");
});

test("oversized and malformed records never leak content and buffer stays bounded", async (t) => {
    const decoder = new RecordDecoder();
    let rejected = 0;
    for (let i = 0; i < 20; i++) {
        rejected += decoder.push(Buffer.alloc(4096, 120)).filter((entry) => entry.error).length;
        assert.ok(decoder.pending.length <= LIMITS.lineBytes);
    }
    assert.equal(rejected, 1);
    assert.deepEqual(decoder.push(Buffer.from("\n" + jsonl(record()))), [{ record: record() }]);

    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file,
        "RAW_SENTINEL_NOT_JSON\n" + "x".repeat(LIMITS.lineBytes + 100) + "\n"
        + jsonl(record(1, "stage", { prompt: "RAW_SENTINEL_PROMPT" }),
            record(1, "stage", { message: "api_key=RAW_SENTINEL_SECRET" }),
            record(1, "stage", { message: "Safe stage" })), { flag: "wx" });
    await monitor.poll();
    assert.equal(monitor.status().rejected_records, 4);
    assert.equal(monitor.status().accepted_records, 1);
    assert.equal(f.logs.filter((entry) => entry.level === "warning").length, 1);
    assert.doesNotMatch(JSON.stringify([f.logs, monitor.status()]), /RAW_SENTINEL|x{100}/u);
    assert.equal(monitor.status().last_event.message, "Safe stage");
});

test("run identity and sequence are pinned to accepted records, replay is rejected", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(
        record(1, "started"), record(1, "failed"), record(2, "completed", { run_id: "other-run" }),
        record(0, "completed"), record(3, "stage", { stage: "Inspecting" }),
        record(2, "completed"), record(4, "completed"),
    ), { flag: "wx" });
    await monitor.poll();
    const status = monitor.status();
    assert.equal(status.run_id, "synthetic-run");
    assert.equal(status.last_seq, 4);
    assert.equal(status.accepted_records, 3);
    assert.equal(status.rejected_records, 4);
    assert.equal(status.state, "completed");
    assert.equal(status.latest.stage, "Inspecting");
    assert.ok(!f.logs.some((entry) => entry.message.includes("Failed:")));
});

test("symlinks and hardlinks are never consumed", async (t) => {
    const f = await fixture(t);
    const target = join(f.workspacePath, "synthetic-decoy.jsonl");
    await writeFile(target, jsonl(record(1, "completed", { message: "DECOY_CONTENT" })), { flag: "wx" });
    for (const createLink of [symlink, link]) {
        const { monitor, activity_file } = await f.watch();
        await createLink(target, activity_file);
        await monitor.poll();
        assert.equal(monitor.status().state, "monitor_error");
        assert.equal(monitor.status().error.code, "unsafe_file");
        assert.equal(monitor.status().accepted_records, 0);
        assert.equal(monitor.handle, undefined);
    }
    assert.doesNotMatch(JSON.stringify(f.logs), /DECOY_CONTENT/u);
});

test("directory replacement by a symlink is rejected before file consumption", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    const directory = dirname(activity_file);
    await rename(directory, `${directory}-original`);
    await symlink(f.workspacePath, directory);
    await monitor.poll();
    assert.equal(monitor.status().error.code, "unsafe_directory");
    assert.equal(monitor.status().accepted_records, 0);
});

test("replacement and removal of an opened file stop with a monitor error", async (t) => {
    const f = await fixture(t);
    for (const replace of [true, false]) {
        const { monitor, activity_file } = await f.watch();
        await writeFile(activity_file, jsonl(record(1, "started")), { flag: "wx" });
        await monitor.poll();
        await rename(activity_file, `${activity_file}.original`);
        if (replace) await writeFile(activity_file, jsonl(record(2, "completed")), { flag: "wx" });
        await monitor.poll();
        assert.equal(monitor.status().state, "monitor_error");
        assert.equal(monitor.status().error.code, "file_replaced");
        assert.equal(monitor.status().accepted_records, 1);
        assert.equal(monitor.status().observed_outcome, null);
        assert.equal(monitor.handle, undefined);
    }
});

test("truncation, same-size overwrite and truncate/regrow are not silently followed", async (t) => {
    const f = await fixture(t);
    for (const mutation of ["truncate", "overwrite", "regrow"]) {
        const { monitor, activity_file } = await f.watch();
        const first = jsonl(record(1, "started", { message: "Original report" }));
        await writeFile(activity_file, first, { flag: "wx" });
        await monitor.poll();
        if (mutation === "truncate") await truncate(activity_file, 0);
        if (mutation === "overwrite") await writeFile(activity_file, first.replace("Original", "Modified"));
        if (mutation === "regrow") {
            await writeFile(activity_file,
                first.replace("Original", "Modified") + jsonl(record(2, "completed")));
        }
        await monitor.poll();
        assert.equal(monitor.status().error.code, "file_truncated", mutation);
        assert.equal(monitor.status().accepted_records, 1);
        assert.equal(monitor.status().observed_outcome, null);
    }
});

test("file-size/read budgets are enforced and complete queued records drain before auto-stop", async (t) => {
    const f = await fixture(t);
    const oversized = await f.watch();
    await writeFile(oversized.activity_file, "", { flag: "wx" });
    await truncate(oversized.activity_file, LIMITS.fileBytes + 1);
    await oversized.monitor.poll();
    assert.equal(oversized.monitor.status().error.code, "file_too_large");
    assert.equal(oversized.monitor.offset, 0);

    const queued = await f.watch();
    const records = [record(1, "started"), record(2, "completed")];
    for (let seq = 3; seq <= 900; seq++) records.push(record(seq, "heartbeat"));
    const content = jsonl(...records);
    assert.ok(Buffer.byteLength(content) > LIMITS.bytesPerPoll);
    await writeFile(queued.activity_file, content, { flag: "wx" });
    await queued.monitor.poll();
    assert.equal(queued.monitor.offset, LIMITS.bytesPerPoll);
    assert.equal(queued.monitor.active, true);
    while (queued.monitor.active) await queued.monitor.poll();
    assert.equal(queued.monitor.status().accepted_records, 900);
    assert.equal(queued.monitor.status().state, "completed");
    assert.equal(queued.monitor.offset, Buffer.byteLength(content));
    assert.equal(queued.monitor.decoder.pending.length, 0);
});

test("a terminal record with an incomplete trailing line stops without interpreting the fragment", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(record(1, "completed")) + '{"message":"RAW_FRAGMENT', { flag: "wx" });
    await monitor.poll();
    assert.equal(monitor.status().state, "completed");
    assert.equal(monitor.status().rejected_records, 1);
    assert.doesNotMatch(JSON.stringify(f.logs), /RAW_FRAGMENT/u);
});

test("recent history is bounded, independent snapshots cannot mutate observations", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(...Array.from({ length: 100 }, (_, i) => record(i + 1))), { flag: "wx" });
    await monitor.poll();
    const status = monitor.status();
    assert.equal(status.recent_lines.length, LIMITS.recentLines);
    assert.match(status.recent_lines[0], /Reported activity 69/u);
    assert.equal(f.logs.length, 1);
    status.recent_lines.length = 0;
    status.last_event.message = "Changed externally";
    status.latest.stage = "Changed externally";
    assert.equal(monitor.status().recent_lines.length, LIMITS.recentLines);
    assert.equal(monitor.status().last_event.message, "Reported activity 100");
    assert.equal(monitor.status().latest.stage, undefined);
});

test("activity bursts coalesce but stage/model, warnings and terminal results stay visible", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(
        record(1), record(2), record(3),
        record(4, "stage", { stage: "Proof" }),
        record(5, "model", { model: "gpt-6-astra", provider: "copilot" }),
        record(6, "warning", { message: "Proof reported a warning" }),
        record(7, "failed", { message: "Proof failed", proof_passed: false }),
    ), { flag: "wx" });
    await monitor.poll();
    assert.equal(f.logs.length, 6);
    assert.match(f.logs[0].message, /Reported activity 1/u);
    assert.match(f.logs[1].message, /Reported activity 3/u);
    assert.match(f.logs[2].message, /Proof.*Reported activity 4/u);
    assert.match(f.logs[3].message, /gpt-6-astra/u);
    assert.equal(f.logs[4].level, "warning");
    assert.equal(f.logs[5].level, "error");
    assert.match(f.logs[5].message, /Failed: Proof failed/u);
    assert.equal(monitor.status().state, "failed");
    assert.equal(monitor.status().throttled_activity_records, 2);
});

test("pending activity flushes on the next rate window even without another append", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(record(1), record(2), record(3)), { flag: "wx" });
    await monitor.poll();
    assert.equal(f.logs.length, 1);
    f.advance(LIMITS.activityMs);
    await monitor.poll();
    assert.equal(f.logs.length, 2);
    assert.match(f.logs[1].message, /Reported activity 3/u);
    await monitor.poll();
    assert.equal(f.logs.length, 2);
});

test("stage/model/provider changes on activity or heartbeat events bypass burst throttling", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(
        record(1, "activity", { stage: "Inspect" }),
        record(2, "activity", { stage: "Proof" }),
        record(3, "activity", { stage: "Proof" }),
        record(4, "activity", { model: "gpt-6-astra", provider: "copilot" }),
        record(5, "heartbeat", { provider: "synthetic-provider", message: "UNSUPPORTED_HEARTBEAT_CLAIM" }),
        record(6, "heartbeat", { provider: "synthetic-provider" }),
    ), { flag: "wx" });
    await monitor.poll();
    assert.equal(f.logs.length, 5);
    assert.match(f.logs[0].message, /Inspect/u);
    assert.match(f.logs[1].message, /Proof/u);
    assert.match(f.logs[2].message, /Reported activity 3/u);
    assert.match(f.logs[3].message, /gpt-6-astra \(copilot\)/u);
    assert.match(f.logs[4].message, /synthetic-provider.*Reported stage\/model\/provider changed/u);
    assert.doesNotMatch(JSON.stringify(f.logs), /UNSUPPORTED_HEARTBEAT_CLAIM/u);
});

test("heartbeats and stale periods are low-noise and never invent process progress", async (t) => {
    const f = await fixture(t);
    const { monitor, activity_file } = await f.watch();
    f.advance(LIMITS.livenessMs);
    await monitor.poll();
    assert.equal(f.logs.length, 1);
    assert.equal(f.logs[0].ephemeral, true);
    assert.match(f.logs[0].message, /no SkyN3t start has been observed/u);
    await writeFile(activity_file, jsonl(record(1, "started")), { flag: "wx" });
    await monitor.poll();
    for (let seq = 2; seq <= 6; seq++) {
        f.advance(15000);
        await appendFile(activity_file, jsonl(record(seq, "heartbeat", { message: "UNSUPPORTED_CLAIM_STILL_CODING" })));
        await monitor.poll();
    }
    assert.equal(f.logs.length, 3);
    assert.equal(f.logs[2].ephemeral, true);
    assert.match(f.logs[2].message, /No new reported activity for 01:00; process outcome is unknown/u);
    assert.doesNotMatch(JSON.stringify(f.logs), /UNSUPPORTED_CLAIM|hung/u);
    assert.equal(monitor.status().last_event_age_s, 0);
    assert.equal(monitor.status().last_reported_activity_age_s, 75);
    assert.equal(monitor.status().state, "observing");
});

test("interruption differs from success and stop preserves observations and file contents", async (t) => {
    const f = await fixture(t);
    const terminal = await f.watch();
    await writeFile(terminal.activity_file, jsonl(record(1, "interrupted", { message: "Run interrupted" })), { flag: "wx" });
    await terminal.monitor.poll();
    assert.equal(terminal.monitor.status().state, "interrupted");
    assert.equal(f.logs[0].level, "warning");

    const manual = await f.watch();
    const content = jsonl(record(1, "stage", { stage: "Improving", model: "gpt-6-astra" }));
    await writeFile(manual.activity_file, content, { flag: "wx" });
    await manual.monitor.poll();
    await manual.monitor.stop();
    await manual.monitor.stop();
    assert.equal(manual.monitor.status().state, "stopped");
    assert.equal(manual.monitor.status().latest.stage, "Improving");
    assert.equal(manual.monitor.status().observed_outcome, null);
    assert.equal(manual.monitor.handle, undefined);
    assert.equal(await readFile(manual.activity_file, "utf8"), content);
    await appendFile(manual.activity_file, jsonl(record(2, "completed")));
    await manual.monitor.poll();
    assert.equal(manual.monitor.status().accepted_records, 1);
});

test("stop during an in-flight log waits for cleanup and does not emit queued activity", async (t) => {
    let release;
    let entered;
    const started = new Promise((resolve) => { entered = resolve; });
    const gate = new Promise((resolve) => { release = resolve; });
    let logged = 0;
    const f = await fixture(t, { log: async () => { logged++; entered(); await gate; } });
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(record(1, "started"), record(2, "stage")), { flag: "wx" });
    const poll = monitor.poll();
    await started;
    const stopped = monitor.stop();
    assert.equal(monitor.active, false);
    release();
    await Promise.all([poll, stopped]);
    assert.equal(logged, 1);
    assert.equal(monitor.handle, undefined);
    assert.equal(monitor.decoder.pending.length, 0);
    assert.equal(monitor.timer, undefined);
});

test("session shutdown and SIGTERM release timers/listeners/handles without cancelling a process", async (t) => {
    for (const event of ["session.shutdown", "SIGTERM"]) {
        const f = await fixture(t, { autoPoll: true });
        const first = await f.watch();
        const second = await f.watch();
        await writeFile(first.activity_file, jsonl(record(1, "started")), { flag: "wx" });
        await first.monitor.poll();
        assert.ok(first.monitor.handle);
        const sdk = new EventEmitter();
        const signals = new EventEmitter();
        const session = {
            on(name, handler) {
                sdk.on(name, handler);
                return () => sdk.removeListener(name, handler);
            },
        };
        const cleanup = attachLifecycle(session, f.observer, signals);
        (event === "SIGTERM" ? signals : sdk).emit(event);
        await cleanup();
        assert.equal(sdk.listenerCount("session.shutdown"), 0);
        assert.equal(signals.listenerCount("SIGTERM"), 0);
        for (const { monitor } of [first, second]) {
            assert.equal(monitor.active, false);
            assert.equal(monitor.timer, undefined);
            assert.equal(monitor.handle, undefined);
            assert.equal(monitor.status().stop_reason, "session_shutdown");
        }
        await assert.rejects(f.observer.watch(), { code: "shutting_down" });
    }
});

test("native logger failures surface a monitor error without leaking raw error text", async (t) => {
    const f = await fixture(t, {
        log: async () => { throw new Error("api_key=RAW_LOG_ERROR_SECRET"); },
    });
    const { monitor, activity_file } = await f.watch();
    await writeFile(activity_file, jsonl(record(1, "started")), { flag: "wx" });
    await monitor.poll();
    assert.equal(monitor.status().state, "monitor_error");
    assert.equal(monitor.status().error.code, "timeline_unavailable");
    assert.equal(monitor.handle, undefined);
    assert.equal(f.diagnostics.length, 1);
    assert.doesNotMatch(JSON.stringify([monitor.status(), f.diagnostics]), /RAW_LOG_ERROR_SECRET/u);
});

test("active and retained monitor counts are bounded", async (t) => {
    const f = await fixture(t);
    for (let i = 0; i < LIMITS.activeMonitors; i++) await f.watch();
    await assert.rejects(f.observer.watch(), { code: "monitor_limit" });
    for (const monitor of f.observer.monitors.values()) await monitor.stop();
    for (let i = LIMITS.activeMonitors; i < LIMITS.totalMonitors; i++) {
        const { monitor } = await f.watch();
        await monitor.stop();
    }
    await assert.rejects(f.observer.watch(), { code: "monitor_limit" });
    assert.throws(() => f.observer.get("unknown"), ObservationError);
});
