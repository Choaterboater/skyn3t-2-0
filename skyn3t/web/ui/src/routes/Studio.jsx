import React, { useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";
import {
  PageHeader,
  Panel,
  PanelHead,
  Stat,
  Pill,
  Empty,
  verdictTone,
} from "../components/ui.jsx";
import { DebugTimeline, FilesSoFar, PreviewPanel } from "../components/cockpit.jsx";

// Canonical pipeline stages (mirrors the stage vocabulary in the backend).
const STAGES = [
  "brainstorm",
  "research",
  "architecture",
  "design",
  "codegen",
  "review",
  "verify_build",
  "package",
];

// Curated one-liner starters spanning the supported stacks. Clicking one fills
// the brief box so a new user has a good, varied jumping-off point.
const EXAMPLE_BRIEFS = [
  "A kids coloring app: gallery of animal line-art SVGs you can click to fill with color",
  "A FastAPI todo REST API with SQLite persistence and a /health endpoint",
  "A React dashboard that charts a CSV you upload",
  "A static landing page for a coffee shop with hours, menu, and a map",
  "A Python CLI that renames photos by EXIF date",
  "A markdown note-taking SPA with local-storage autosave",
  "A Node/Express URL shortener with an in-memory store",
  "An Expo mobile app: a habit tracker with daily streaks",
];

// Fallback stack ids if GET /stacks is empty/unavailable — the six real builders.
const FALLBACK_STACKS = [
  { id: "react", description: "a browser web app / SPA / dashboard UI (Vite + React)" },
  { id: "react_native", description: "a mobile app for iOS/Android (Expo)" },
  { id: "fastapi", description: "a Python web app or HTTP/REST API with a server + storage" },
  { id: "static", description: "a static website / landing page (HTML/CSS/JS, no backend)" },
  { id: "python", description: "a Python CLI tool, script, or library (no web UI)" },
  { id: "express", description: "a Node.js web server / API" },
];

// Today's default fan-out selection — keeps behavior unchanged if left untouched.
const DEFAULT_STACK_SELECTION = ["react", "static", "fastapi"];

function stageState(stage, events) {
  // Derive a stage's state from BUILD_STAGE_* events in the stream.
  let state = "pending";
  events.forEach((e) => {
    const s = (e.payload && (e.payload.stage || e.payload.capability)) || "";
    if (s !== stage) return;
    // EventType.value is lowercase-dotted on the wire (events.py), not the enum NAME.
    if (e.type === "build.stage.started") state = "running";
    if (e.type === "build.stage.completed") state = "done";
    if (e.type === "task.failed" || e.type === "build.failed") state = "failed";
  });
  return state;
}

// The forge line. Each stage is a node on a horizontal rail: it ignites EMBER
// while running, cools to PLASMA when done, sits ASH while pending, flares the
// hot ember on failure.
function ForgeStage({ stage, state }) {
  const hot = state === "running";
  const done = state === "done";
  const failed = state === "failed";

  const nodeCls = hot
    ? "border-ember/60 bg-ember/10 ring-heat animate-emberflare"
    : done
    ? "border-plasma/40 bg-plasma/5"
    : failed
    ? "border-ember/60 bg-ember/10"
    : "border-hairline bg-void/60";

  const dotCls = hot
    ? "bg-ember animate-forgepulse"
    : done
    ? "bg-plasma"
    : failed
    ? "bg-ember"
    : "bg-ash/40";

  const labelCls = hot
    ? "text-ember"
    : done
    ? "text-plasma"
    : failed
    ? "text-ember"
    : "text-ash";

  return (
    <div
      title={`${stage} · ${state}`}
      className={`flex min-w-[7.5rem] flex-col gap-2 rounded-md border px-3 py-2.5 transition-all duration-300 ${nodeCls}`}
    >
      <div className="flex items-center gap-2">
        <span className={`h-1.5 w-1.5 rounded-full ${dotCls}`} />
        <span className={`font-mono text-[11px] ${labelCls}`}>{stage}</span>
      </div>
      <span className="eyebrow text-[9px] text-ash/70">
        {failed ? "failed" : state}
      </span>
    </div>
  );
}

function buildMeta(build) {
  const cls = build.classification || {};
  return {
    stack: build.stack || build.stack_selection?.stack || "auto",
    appType: build.app_type || cls.app_type || "auto",
    engine: build.engine || cls.engine || "auto",
  };
}

export default function Studio({ stream }) {
  const qc = useQueryClient();
  const [brief, setBrief] = useState("");
  // Fan-out stack selection as a set of stack ids (toggleable chips below).
  const [selectedStacks, setSelectedStacks] = useState(
    () => new Set(DEFAULT_STACK_SELECTION)
  );
  const [pendingBuildId, setPendingBuildId] = React.useState(null);
  const briefRef = useRef(null);
  // "Build from a picture": an optional reference image (data URL) attached to
  // the build. No attachment -> the field is omitted from the POST (unchanged).
  const [refImage, setRefImage] = useState(null); // { url, name }
  const fileInputRef = useRef(null);

  const onPickImage = (file) => {
    if (!file || !file.type?.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = () => setRefImage({ url: String(reader.result), name: file.name });
    reader.readAsDataURL(file);
  };
  const clearImage = () => {
    setRefImage(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const { data: builds } = useQuery({
    queryKey: ["builds"],
    queryFn: queryFn("/builds"),
  });

  // Supported real-builder stacks for the fan-out picker. Falls back to the six
  // known ids if the endpoint is empty/unavailable.
  const { data: stacksData } = useQuery({
    queryKey: ["stacks"],
    queryFn: queryFn("/stacks"),
  });
  const stackOptions = useMemo(() => {
    const opts = stacksData?.stacks;
    return Array.isArray(opts) && opts.length > 0 ? opts : FALLBACK_STACKS;
  }, [stacksData]);

  const toggleStack = (id) =>
    setSelectedStacks((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const useExample = (text) => {
    setBrief(text);
    // Focus + scroll the brief box so the user can edit then build.
    const el = briefRef.current;
    if (el) {
      el.focus();
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };

  const submit = useMutation({
    mutationFn: (payload) => apiPost("/builds", payload),
    onSuccess: () => {
      setBrief("");
      clearImage();
      qc.invalidateQueries({ queryKey: ["builds"] });
    },
  });

  const approve = useMutation({
    mutationFn: ({ build_id, approved }) =>
      apiPost("/studio/approve", { build_id, approved, reason: "" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["builds"] }),
    onSettled: () => setPendingBuildId(null),
  });

  // Spec 4: explore the brief across divergent stacks; results stream as
  // FANOUT_* events on the shared socket. The selected stack ids are sent as a
  // comma-joined string (the /studio/fanout contract).
  const fanoutMut = useMutation({
    mutationFn: () =>
      apiPost("/studio/fanout", {
        brief: brief.trim(),
        stacks: [...selectedStacks].join(","),
      }),
  });

  const events = stream?.events || [];
  const pipeline = useMemo(
    () => STAGES.map((s) => ({ stage: s, state: stageState(s, events) })),
    [events]
  );

  const fanout = useMemo(() => {
    const cands = {};
    let done = null;
    let active = false;
    for (const e of events) {
      if (e.type === "fanout.started") active = true;
      else if (e.type === "fanout.candidate") {
        const p = e.payload || {};
        if (p.candidate_id) cands[p.candidate_id] = p;
      } else if (e.type === "fanout.completed") {
        done = e.payload || {};
        active = false;
      }
    }
    return { cands: Object.values(cands), done, active };
  }, [events]);

  const recentBuilds = Array.isArray(builds) ? builds : builds?.builds || [];

  const running = pipeline.filter((p) => p.state === "running").length;
  const done = pipeline.filter((p) => p.state === "done").length;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Build Console"
        title="Studio"
        sub="Forge a brief into running software. Watch the line ignite as the swarm works."
        actions={
          <span className="badge border-hairline text-ash">
            forge line ·{" "}
            <span className={`ml-1 ${running ? "text-ember" : "text-plasma"}`}>
              {running ? "live" : "idle"}
            </span>
          </span>
        }
      />

      <Panel className="mb-6 p-4">
        <form
          className="flex flex-col gap-3 sm:flex-row sm:items-stretch"
          onSubmit={(e) => {
            e.preventDefault();
            if (!brief.trim()) return;
            const payload = { brief: brief.trim() };
            if (refImage?.url) payload.reference_image = refImage.url;
            submit.mutate(payload);
          }}
        >
          <input
            ref={briefRef}
            className="field flex-1"
            placeholder="Describe the app to build…"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
          />
          {/* "Build from a picture": attach one reference image (screenshot,
              drawing, or diagram) that the design/architecture agents match. */}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => onPickImage(e.target.files?.[0])}
          />
          {refImage ? (
            <div className="flex items-center gap-2 rounded border border-hairline px-2 py-1">
              <img
                src={refImage.url}
                alt="reference"
                className="h-9 w-9 rounded object-cover"
              />
              <span className="max-w-[8rem] truncate font-mono text-[11px] text-ash">
                {refImage.name}
              </span>
              <button
                type="button"
                onClick={clearImage}
                title="Remove reference image"
                className="text-ash hover:text-ember"
              >
                ✕
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="btn-ghost disabled:opacity-50"
              title="Attach a reference image — a screenshot, drawing, or diagram to match"
            >
              + Image
            </button>
          )}
          <button
            type="submit"
            disabled={submit.isPending || !brief.trim()}
            className="btn-ember disabled:opacity-50"
          >
            {submit.isPending ? "Forging…" : "Forge build"}
          </button>
        </form>
        {submit.isError ? (
          <p className="mt-3 font-mono text-xs text-ember">
            {String(submit.error.message)}
          </p>
        ) : null}

        {/* Example briefs: clickable starters that fill the brief box. */}
        <div className="mt-3">
          <span className="eyebrow text-[9px] text-ash/70">Need a starting point? Try:</span>
          <div className="mt-2 flex flex-wrap gap-2">
            {EXAMPLE_BRIEFS.map((ex) => (
              <button
                key={ex}
                type="button"
                onClick={() => useExample(ex)}
                title="Fill the brief with this example — then edit and forge"
                className="rounded-full border border-hairline px-3 py-1 text-left text-[11px] text-ash transition-colors hover:border-ember/40 hover:text-bone"
              >
                {ex}
              </button>
            ))}
          </div>
        </div>

        {/* Spec 4: fan the same brief out across divergent stacks, pick a winner */}
        <div className="mt-3 border-t border-hairline pt-3">
          <div className="flex flex-col gap-1">
            <span className="font-mono text-[11px] text-ash">Fan out across stacks:</span>
            <span className="text-[11px] text-ash/70">
              Optional — skyn3t auto-picks a stack from your brief. Use fan-out to
              build across several and compare.
            </span>
          </div>
          <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex flex-wrap gap-2">
              {stackOptions.map((s) => {
                const on = selectedStacks.has(s.id);
                return (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => toggleStack(s.id)}
                    title={s.description}
                    aria-pressed={on}
                    className={`rounded-full border px-3 py-1 font-mono text-[11px] transition-colors ${
                      on
                        ? "border-ember/50 bg-ember/10 text-ember"
                        : "border-hairline text-ash hover:border-ember/30 hover:text-bone"
                    }`}
                  >
                    {s.id}
                  </button>
                );
              })}
            </div>
            <button
              type="button"
              onClick={() => fanoutMut.mutate()}
              disabled={
                fanoutMut.isPending || !brief.trim() || selectedStacks.size < 2
              }
              className="btn-ghost flex-shrink-0 disabled:opacity-50"
              title="Build N divergent stack candidates for this brief and pick the winner"
            >
              {fanoutMut.isPending ? "Exploring…" : "Fan out"}
            </button>
          </div>
        </div>
        {fanoutMut.isError ? (
          <p className="mt-2 font-mono text-[11px] text-ember">
            {String(fanoutMut.error.message)}
          </p>
        ) : null}
        {fanoutMut.data && fanoutMut.data.accepted === false ? (
          <p className="mt-2 font-mono text-[11px] text-ember">
            {fanoutMut.data.reason || "fan-out unavailable"}
          </p>
        ) : null}
      </Panel>

      {fanout.cands.length > 0 || fanout.active ? (
        <Panel className="mb-6 overflow-hidden">
          <PanelHead
            label="Fan-out exploration"
            right={
              fanout.done ? (
                <span className="font-mono text-[11px] text-plasma">
                  winner <span className="text-bone">{fanout.done.winner || "—"}</span> · Δ +
                  {fanout.done.delta ?? 0}
                </span>
              ) : (
                <span className="font-mono text-[11px] text-ember">exploring…</span>
              )
            }
          />
          <div className="divide-y divide-hairline/60">
            {fanout.cands.map((c) => {
              const win = fanout.done && fanout.done.winner === c.candidate_id;
              return (
                <div
                  key={c.candidate_id}
                  className="flex items-center justify-between px-4 py-2"
                >
                  <span className="font-mono text-xs text-bone">
                    {win ? "★ " : ""}
                    {c.candidate_id}
                  </span>
                  <div className="flex items-center gap-3 font-mono text-[11px]">
                    <Pill tone={verdictTone(c.verdict)}>{c.verdict}</Pill>
                    <span className="text-ash">score {c.score ?? "—"}</span>
                    <span className={c.proof_passed ? "text-plasma" : "text-ember"}>
                      {c.proof_passed ? "proof ✓" : "proof ✕"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>
      ) : null}

      <Panel className="mb-6 overflow-hidden">
        <PanelHead
          label="Forge line"
          right={
            <span className="font-mono text-[11px] text-ash">
              {running ? (
                <span className="text-ember">{running} igniting</span>
              ) : (
                <span className="text-plasma">{done}/{STAGES.length} cooled</span>
              )}
            </span>
          }
        />
        <div className="flex items-stretch gap-1 overflow-x-auto p-4">
          {pipeline.map((p, i) => (
            <React.Fragment key={p.stage}>
              <ForgeStage stage={p.stage} state={p.state} />
              {i < pipeline.length - 1 ? (
                <span
                  className={`mx-0.5 flex-shrink-0 self-center font-mono text-sm ${
                    p.state === "done" ? "text-plasma/60" : "text-ash/30"
                  }`}
                >
                  →
                </span>
              ) : null}
            </React.Fragment>
          ))}
        </div>
      </Panel>

      {/* Cockpit: per-stage debug timeline + live artifact (Phase A) */}
      <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel className="overflow-hidden">
          <PanelHead label="Stage debug" />
          <DebugTimeline events={events} />
        </Panel>
        <Panel className="overflow-hidden">
          <PanelHead label="Live preview" />
          <PreviewPanel events={events} />
          <PanelHead label="Files so far" />
          <FilesSoFar events={events} />
        </Panel>
      </div>

      <Panel>
        <PanelHead
          label="Recent builds"
          right={
            <span className="font-mono text-[11px] text-ash">
              {recentBuilds.length} total
            </span>
          }
        />
        {approve.isError ? (
          <p className="px-4 py-3 font-mono text-xs text-ember">
            {String(approve.error?.message || approve.error)}
          </p>
        ) : null}
        {recentBuilds.length === 0 ? (
          <Empty icon="⬡">No builds yet. Submit a brief to fire the forge.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="eyebrow border-b border-hairline text-ash">
                  <th className="px-4 py-2 font-normal">Slug</th>
                  <th className="px-4 py-2 font-normal">Stack</th>
                  <th className="px-4 py-2 font-normal">Type</th>
                  <th className="px-4 py-2 font-normal">Engine</th>
                  <th className="px-4 py-2 font-normal">Status</th>
                  <th className="px-4 py-2 font-normal">Score</th>
                  <th className="px-4 py-2 font-normal">Cost</th>
                  <th className="px-4 py-2 font-normal"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline/60">
                {recentBuilds.map((b) => {
                  const meta = buildMeta(b);
                  return (
                    <tr key={b.build_id || b.slug}>
                      <td className="px-4 py-2 font-mono text-xs text-bone">
                        {b.slug}
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.stack}</td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.appType}</td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.engine}</td>
                      <td className="px-4 py-2">
                        <Pill tone={verdictTone(b.status)}>{b.status}</Pill>
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">
                        {b.score ?? "—"}
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">
                        {b.cost_usd != null
                          ? `$${Number(b.cost_usd).toFixed(4)}`
                          : "—"}
                      </td>
                      <td className="px-4 py-2 text-right">
                        {["running", "queued", "pending", "awaiting_approval"].includes(b.status) ? (
                          <div className="flex items-center justify-end gap-2">
                            <button
                              onClick={() => {
                                setPendingBuildId(b.build_id || b.slug);
                                approve.mutate({
                                  build_id: b.build_id || b.slug,
                                  approved: true,
                                  reason: "",
                                });
                              }}
                              disabled={approve.isPending && pendingBuildId === (b.build_id || b.slug)}
                              className="btn-ember disabled:opacity-50"
                              title="Approve this build"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => {
                                setPendingBuildId(b.build_id || b.slug);
                                approve.mutate({
                                  build_id: b.build_id || b.slug,
                                  approved: false,
                                  reason: "",
                                });
                              }}
                              disabled={approve.isPending && pendingBuildId === (b.build_id || b.slug)}
                              className="btn-ghost disabled:opacity-50"
                              title="Reject this build"
                            >
                              Reject
                            </button>
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
