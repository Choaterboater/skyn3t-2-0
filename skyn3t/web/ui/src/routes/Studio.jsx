import React, { useMemo, useState } from "react";
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

export default function Studio({ stream }) {
  const qc = useQueryClient();
  const [brief, setBrief] = useState("");

  const { data: builds } = useQuery({
    queryKey: ["builds"],
    queryFn: queryFn("/builds"),
  });

  const submit = useMutation({
    mutationFn: (payload) => apiPost("/builds", payload),
    onSuccess: () => {
      setBrief("");
      qc.invalidateQueries({ queryKey: ["builds"] });
    },
  });

  const events = stream?.events || [];
  const pipeline = useMemo(
    () => STAGES.map((s) => ({ stage: s, state: stageState(s, events) })),
    [events]
  );

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
            if (brief.trim()) submit.mutate({ brief: brief.trim() });
          }}
        >
          <input
            className="field flex-1"
            placeholder="Describe the app to build…"
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
          />
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
      </Panel>

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
        {recentBuilds.length === 0 ? (
          <Empty icon="⬡">No builds yet. Submit a brief to fire the forge.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="eyebrow border-b border-hairline text-ash">
                  <th className="px-4 py-2 font-normal">Slug</th>
                  <th className="px-4 py-2 font-normal">Status</th>
                  <th className="px-4 py-2 font-normal">Score</th>
                  <th className="px-4 py-2 font-normal">Cost</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline/60">
                {recentBuilds.map((b) => (
                  <tr key={b.build_id || b.slug}>
                    <td className="px-4 py-2 font-mono text-xs text-bone">
                      {b.slug}
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
