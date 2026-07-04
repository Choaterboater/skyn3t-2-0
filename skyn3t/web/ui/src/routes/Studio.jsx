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
import {
  DebugTimeline,
  FilesSoFar,
  PreviewPanel,
  StageLedger,
  pipelineFromEvents,
} from "../components/cockpit.jsx";

// Fallback rail shown before the first `build.started` arrives. Once a build is
// live, the rail is driven off the REAL emitted plan (build.started.stages) — so
// it always matches the backend pipeline instead of this fixed guess.
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
  "A golf website for adult beginners with lesson paths, drills, equipment basics, tutorial resources, and tee-time CTAs",
  "A local HVAC company website with service pages, financing calls-to-action, reviews, emergency contact, and generated service photos",
  "A client portal web app for a small marketing agency: projects, approvals, invoices, messages, and admin settings",
  "An AI paper-trading dashboard using OpenRouter models, Alpaca paper mode, risk profiles, backtests, and audit logs",
  "A course marketplace for woodworking classes with instructor profiles, schedule filters, checkout mockup, and learner dashboard",
  "A rebuild of a GitHub repo as a polished web UI with the same core features, better onboarding, and editable settings",
  "A mobile habit tracker with streaks, reminders, charts, and offline-first local storage",
  "An MCP server for filesystem search and safe read-only project inspection",
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

// Fan-out is optional; start empty so highlighted chips never look like the
// normal build route. A regular Forge build auto-picks from the brief.
const DEFAULT_STACK_SELECTION = [];

const BUILD_PROFILES = [
  {
    id: "cheap_learned",
    label: "Cheap + learned",
    hint: "Uses learned routing, skills, and recall without pinning an expensive model.",
  },
  { id: "fast", label: "Fast", hint: "Shortest path with fewer debug retries." },
  { id: "best_quality", label: "Best quality", hint: "Runs best-of-N, richer assets when configured, and visual repair." },
  { id: "manual", label: "Manual model", hint: "Pin one OpenRouter model for this build." },
];

// The forge line. Each stage is a node on a horizontal rail: it ignites EMBER
// while running, cools to PLASMA when done, sits ASH while pending, flares the
// hot ember on failure. It also shows WHICH agent ran the stage and its score —
// data the backend already emits — so the line is legible, not just decorative.
function ForgeStage({ s }) {
  const state = s.state;
  const hot = state === "running";
  const done = state === "done";
  const failed = state === "failed";
  const agent = s.agentName || s.agentType;

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

  const title =
    `${s.stage}${agent ? " · " + agent : ""} · ${failed ? "failed" : state}` +
    (s.score != null ? ` · score ${s.score}` : "");

  return (
    <div
      title={title}
      className={`flex min-w-[7.5rem] flex-col gap-2 rounded-md border px-3 py-2.5 transition-all duration-300 ${nodeCls}`}
    >
      <div className="flex items-center gap-2">
        <span className={`h-1.5 w-1.5 rounded-full ${dotCls}`} />
        <span className={`font-mono text-[11px] ${labelCls}`}>{s.stage}</span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="eyebrow truncate text-[9px] text-ash/70">
          {failed ? "failed" : agent || state}
        </span>
        {done && s.score != null ? (
          <span className="font-mono text-[9px] text-plasma">{s.score}</span>
        ) : null}
      </div>
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

function aiMeta(build) {
  const trace = build.model_trace || {};
  const scorecard = build.quality_scorecard || {};
  return {
    profile: build.build_profile || trace.profile || "cheap_learned",
    model: trace.model_override || trace.codegen_model || "auto",
    skills: Array.isArray(build.skills_used)
      ? build.skills_used.length
      : scorecard.skills_count || 0,
    recall: Array.isArray(build.recall_used)
      ? build.recall_used.length
      : scorecard.recall_count || 0,
    proof: scorecard.proof_passed,
    build: scorecard.build,
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
  const [buildProfile, setBuildProfile] = useState("cheap_learned");
  const [fullApp, setFullApp] = useState(false);
  const [modelOverride, setModelOverride] = useState("");

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

  const models = useQuery({
    queryKey: ["models"],
    queryFn: queryFn("/models"),
    retry: 0,
  });
  const modelOptions = models.data?.models || [];
  const manualModelChoices =
    modelOverride && !modelOptions.includes(modelOverride)
      ? [modelOverride, ...modelOptions]
      : modelOptions;

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
  const cancelBuild = useMutation({
    mutationFn: ({ build_id }) =>
      apiPost("/builds/cancel", {
        build_id,
        reason: "cancelled from Studio",
      }),
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
  // Driven off the REAL emitted plan (build.started.stages) + the per-stage
  // agent/score/cost/gaps the backend already streams — not a hardcoded list.
  const pipeline = useMemo(() => pipelineFromEvents(events, STAGES), [events]);

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
            const payload = {
              brief: brief.trim(),
              build_profile: buildProfile,
              full_app: fullApp,
            };
            if (buildProfile === "manual" && modelOverride.trim()) {
              payload.model_override = modelOverride.trim();
            }
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
            disabled={
              submit.isPending ||
              !brief.trim() ||
              (buildProfile === "manual" && !modelOverride.trim())
            }
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

        <div className="mt-3 border-t border-hairline pt-3">
          <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex flex-wrap gap-2">
              {BUILD_PROFILES.map((p) => {
                const on = buildProfile === p.id;
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => setBuildProfile(p.id)}
                    title={p.hint}
                    aria-pressed={on}
                    className={`rounded-full border px-3 py-1 font-mono text-[11px] transition-colors ${
                      on
                        ? "border-plasma/50 bg-plasma/10 text-plasma"
                        : "border-hairline text-ash hover:border-plasma/30 hover:text-bone"
                    }`}
                  >
                    {p.label}
                  </button>
                );
              })}
            </div>
            <div className="flex min-w-0 flex-1 flex-col gap-1 lg:max-w-[28rem]">
              <select
                value={modelOverride}
                onChange={(e) => {
                  setModelOverride(e.target.value);
                  if (buildProfile !== "manual") setBuildProfile("manual");
                }}
                disabled={buildProfile !== "manual"}
                className="field"
                title="Pin one AI model for this build"
              >
                <option value="">
                  {buildProfile === "manual"
                    ? "Select OpenRouter model"
                    : "Manual model disabled"}
                </option>
                {manualModelChoices.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
              <span className="font-mono text-[10px] text-ash/60">
                {buildProfile === "manual"
                  ? models.data?.note || `${(models.data?.models || []).length} OpenRouter models`
                  : BUILD_PROFILES.find((p) => p.id === buildProfile)?.hint}
              </span>
            </div>
            <label
              className="flex items-center gap-2 rounded-md border border-hairline px-3 py-2 font-mono text-[11px] text-ash"
              title="Build a fuller product with richer content, assets when configured, and more repair budget"
            >
              <input
                type="checkbox"
                checked={fullApp}
                onChange={(e) => setFullApp(e.target.checked)}
                className="accent-plasma"
              />
              <span className={fullApp ? "text-plasma" : ""}>Full app</span>
            </label>
          </div>
        </div>

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
            <div className="flex flex-shrink-0 gap-2">
              <button
                type="button"
                onClick={() => setSelectedStacks(new Set(["react", "nextjs", "static"]))}
                className="btn-ghost disabled:opacity-50"
                title="Select common website/UI stacks for fan-out"
              >
                Web set
              </button>
              <button
                type="button"
                onClick={() => setSelectedStacks(new Set())}
                disabled={selectedStacks.size === 0}
                className="btn-ghost disabled:opacity-50"
                title="Clear fan-out stack selection"
              >
                Clear
              </button>
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
                <span className="text-plasma">{done}/{pipeline.length} cooled</span>
              )}
            </span>
          }
        />
        <div className="flex items-stretch gap-1 overflow-x-auto p-4">
          {pipeline.map((p, i) => (
            <React.Fragment key={p.stage}>
              <ForgeStage s={p} />
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

      {/* The legible per-stage breakdown: agent, state, score, cost, gaps, time —
          all read from data the backend already emits on the event stream. */}
      <Panel className="mb-6 overflow-hidden">
        <PanelHead
          label="Stage ledger"
          right={
            <span className="font-mono text-[11px] text-ash">
              agent · score · cost · gaps
            </span>
          }
        />
        <StageLedger events={events} fallback={STAGES} />
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
        {cancelBuild.isError ? (
          <p className="px-4 py-3 font-mono text-xs text-ember">
            {String(cancelBuild.error?.message || cancelBuild.error)}
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
                  <th className="px-4 py-2 font-normal">AI</th>
                  <th className="px-4 py-2 font-normal">Status</th>
                  <th className="px-4 py-2 font-normal">Score</th>
                  <th className="px-4 py-2 font-normal">Cost</th>
                  <th className="px-4 py-2 font-normal"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline/60">
                {recentBuilds.map((b) => {
                  const meta = buildMeta(b);
                  const ai = aiMeta(b);
                  return (
                    <tr key={b.build_id || b.slug}>
                      <td className="px-4 py-2 font-mono text-xs text-bone">
                        {b.slug}
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.stack}</td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.appType}</td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.engine}</td>
                      <td className="px-4 py-2 font-mono text-[11px] text-ash">
                        <div className="max-w-[12rem] truncate text-bone">{ai.profile}</div>
                        <div
                          className="max-w-[12rem] truncate text-ash/70"
                          title={`${ai.model} · skills ${ai.skills} · recall ${ai.recall}`}
                        >
                          {ai.model} · skills {ai.skills}
                        </div>
                      </td>
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
                            <button
                              onClick={() => {
                                setPendingBuildId(b.build_id || b.slug);
                                cancelBuild.mutate({ build_id: b.build_id || b.slug });
                              }}
                              disabled={
                                cancelBuild.isPending &&
                                pendingBuildId === (b.build_id || b.slug)
                              }
                              className="btn-ghost disabled:opacity-50"
                              title="Cancel this build"
                            >
                              Cancel
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
