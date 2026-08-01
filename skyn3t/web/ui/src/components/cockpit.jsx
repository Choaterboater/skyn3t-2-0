import React, { useEffect, useMemo, useState } from "react";
import { latestBuildEvents } from "../buildSignals.js";
import { apiFetch } from "../api.js";
import {
  settleSliceRowsOnBuildTerminal,
  settleStageRowsOnBuildTerminal,
  sliceTaskIdentity,
} from "../agentSignals.js";

// Build per-stage debug rows from the live event stream. Event-driven: we render
// whatever stages appear, so we never drift from the backend stage vocabulary.
export function debugRowsFromEvents(events) {
  const rows = new Map();
  for (const e of latestBuildEvents(events)) {
    const stage = e.payload?.stage;
    if (!stage) continue;
    if (e.type === "build.stage.debug.started") {
      if (!rows.has(stage)) rows.set(stage, { stage, state: "running", attempts: [] });
    } else if (e.type === "build.stage.debug.attempt") {
      const r = rows.get(stage) || { stage, state: "running", attempts: [] };
      r.attempts.push({
        n: e.payload.attempt,
        passed: e.payload.passed,
        fix: e.payload.fix_applied,
        errors: e.payload.errors || [],
      });
      rows.set(stage, r);
    } else if (e.type === "build.stage.debug.resolved") {
      const r = rows.get(stage) || { stage, state: "running", attempts: [] };
      r.state = e.payload.status; // "passed" | "degraded"
      r.reason = e.payload.reason;
      rows.set(stage, r);
    }
  }
  return Array.from(rows.values());
}

export function latestSnapshot(events) {
  let snap = null;
  for (const e of latestBuildEvents(events)) {
    if (e.type === "build.stage.artifact.snapshot") snap = e.payload;
  }
  return snap; // { build_id, stage, files: [...] } | null
}

export function latestRunningSlug(events) {
  let slug = null;
  for (const e of latestBuildEvents(events)) {
    if (e.type === "build.started" && e.payload?.slug) slug = e.payload.slug;
  }
  return slug;
}

// The preview route only serves a DELIVERED project, so a fetch fired while
// the build is still running 409s. Track the terminal build event
// (build.completed / build.failed) so the preview effect re-runs — and the
// mid-build 409 clears — once the build behind the slug settles.
export function latestBuildTerminal(events) {
  let terminal = null;
  for (const e of latestBuildEvents(events)) {
    if (e.type === "build.completed" || e.type === "build.failed") terminal = e.type;
  }
  return terminal; // "build.completed" | "build.failed" | null
}

// The REAL build pipeline, folded from the live event stream — never a hardcoded
// stage list. `build.started` carries the planner's actual stage names
// (payload.stages); each `build.stage.started` / `build.stage.completed` carries
// the agent, status, score, cost, gaps, and duration that are ALREADY on the wire
// but were previously dropped by the UI. Stages observed but not in the plan
// (gated/optional/retry sub-stages like `fix#2`) are appended so nothing is
// hidden. Falls back to `fallback` before the first `build.started` is seen.
export function pipelineFromEvents(events, fallback = []) {
  let planned = null;
  const rec = new Map();
  const ensure = (name) => {
    let r = rec.get(name);
    if (!r) {
      r = { stage: name, state: "pending" };
      rec.set(name, r);
    }
    return r;
  };
  const buildEvents = latestBuildEvents(events);
  const correlations = new Set(buildEvents.map((event) => event.correlation_id).filter(Boolean));
  const scopedEvents = events.filter(
    (event) => buildEvents.includes(event) || correlations.has(event.correlation_id),
  );
  for (const e of scopedEvents) {
    const p = e.payload || {};
    const eventType = String(e.type || "");
    if (eventType === "build.started" && Array.isArray(p.stages) && p.stages.length) {
      planned = p.stages;
    }
    settleSliceRowsOnBuildTerminal(rec.values(), e);
    // Terminal build events carry no stage name, so the name check below used
    // to drop them and every mid-flight stage row pulsed "running" forever
    // after a cancel/exception. Settle running stage rows here as well.
    settleStageRowsOnBuildTerminal(rec.values(), e);
    const slice = sliceTaskIdentity(e);
    if (slice && (eventType.startsWith("task.") || eventType === "build.stage.artifact.snapshot")) {
      const r = ensure(slice.label);
      r.capability = "slice";
      r.agentType = p.type || "code";
      r.agentName = p.agent || e.source;
      if (eventType === "task.started") r.state = "running";
      else if (eventType === "task.completed") {
        r.state = "done";
        r.status = p.success === false ? "failed" : "completed";
      } else if (eventType === "task.failed") {
        r.state = "failed";
        r.status = "failed";
        if (p.error) r.gaps = [p.error];
      } else if (p.slice_complete) {
        r.state = p.success === false ? "failed" : "done";
        r.status = p.success === false ? "failed" : "completed";
      }
      continue;
    }
    const name = p.stage || p.capability;
    if (!name) continue;
    const r = ensure(name);
    if (p.capability) r.capability = p.capability;
    if (e.type === "build.stage.started") {
      if (r.state !== "done" && r.state !== "failed") r.state = "running";
      if (p.agent_type) r.agentType = p.agent_type;
    } else if (e.type === "build.stage.completed") {
      r.state = "done";
      if (p.status) r.status = p.status;
      if (p.score != null) r.score = p.score;
      if (p.cost_usd != null) r.cost = p.cost_usd;
      if (p.tokens != null) r.tokens = p.tokens;
      if (p.agent_type) r.agentType = p.agent_type;
      if (p.agent_name) r.agentName = p.agent_name;
      if (p.duration_ms != null) r.durationMs = p.duration_ms;
      if (p.gaps && p.gaps.length) r.gaps = p.gaps;
    } else if (e.type === "build.failed" || e.type === "task.failed") {
      r.state = "failed";
    }
  }
  const order = (planned && planned.length ? planned : fallback).slice();
  order.forEach((n) => ensure(n));
  const seen = new Set(order);
  for (const name of rec.keys()) if (!seen.has(name)) order.push(name);
  return order.map((n) => rec.get(n));
}

export function stageTone(state) {
  return state === "running"
    ? "text-ember"
    : state === "done"
    ? "text-plasma"
    : state === "failed"
    ? "text-ember"
    : "text-ash";
}

export function DebugTimeline({ events }) {
  const rows = useMemo(() => debugRowsFromEvents(events), [events]);
  if (rows.length === 0) {
    return <p className="px-4 py-3 font-mono text-[11px] text-ash/70">No debug activity yet.</p>;
  }
  return (
    <div className="flex flex-col divide-y divide-hairline/60">
      {rows.map((r) => (
        <div key={r.stage} className="px-4 py-2">
          <div className="flex items-center justify-between">
            <span className="font-mono text-[12px] text-bone">{r.stage}</span>
            <span
              className={`eyebrow text-[10px] ${
                r.state === "passed"
                  ? "text-plasma"
                  : r.state === "degraded"
                  ? "text-ember"
                  : "text-ash"
              }`}
            >
              {r.state}
            </span>
          </div>
          {r.attempts.map((a) => (
            <div key={a.n} className="pl-3 font-mono text-[10px] text-ash/80">
              #{a.n} {a.fix ? "fix→" : ""}
              {a.passed ? "✓" : "✗"}
              {a.errors.length ? ` · ${a.errors[0]}` : ""}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export function FilesSoFar({ events }) {
  const snap = useMemo(() => latestSnapshot(events), [events]);
  const files = snap?.files || [];
  if (files.length === 0) {
    return <p className="px-4 py-3 font-mono text-[11px] text-ash/70">No files yet.</p>;
  }
  return (
    <ul
      data-visual-overlap-ok
      className="max-h-64 overflow-y-auto px-4 py-2 font-mono text-[11px] text-ash"
    >
      {files.map((f) => (
        <li key={f} className="truncate">
          {f}
        </li>
      ))}
    </ul>
  );
}

export function PreviewPanel({ events }) {
  const slug = useMemo(() => latestRunningSlug(events), [events]);
  const snap = useMemo(() => latestSnapshot(events), [events]);
  const hasIndex = (snap?.files || []).includes("index.html");
  const settled = useMemo(() => latestBuildTerminal(events), [events]);
  const [preview, setPreview] = useState({ slug: "", url: "", error: "" });
  useEffect(() => {
    let alive = true;
    if (!slug || !hasIndex) {
      setPreview({ slug: "", url: "", error: "" });
      return () => { alive = false; };
    }
    setPreview({ slug, url: "", error: "" });
    apiFetch(`/preview/${encodeURIComponent(slug)}`)
      .then((payload) => {
        if (alive) setPreview({ slug, url: payload.preview_url || "", error: "" });
      })
      .catch((error) => {
        if (!alive) return;
        const message = String(error?.message || error);
        // apiFetch prefixes errors with the HTTP status. A mid-build 409 is
        // "not ready yet", not a failure — stay pending; the `settled` dep
        // re-fires this effect once the build finishes and the fetch succeeds.
        if (message.startsWith("409")) setPreview({ slug, url: "", error: "" });
        else setPreview({ slug, url: "", error: message });
      });
    return () => { alive = false; };
  }, [slug, hasIndex, settled]);
  if (!slug) {
    return <p className="px-4 py-3 font-mono text-[11px] text-ash/70">Submit a brief to preview.</p>;
  }
  if (!hasIndex) {
    return (
      <p className="px-4 py-3 font-mono text-[11px] text-ash/70">
        No rendered preview for this stack — see Files + Debug. ({slug})
      </p>
    );
  }
  if (preview.slug !== slug || !preview.url) {
    return (
      <p className="px-4 py-3 font-mono text-[11px] text-ash/70">
        {preview.error ? `Preview unavailable: ${preview.error}` : "Authorizing preview..."}
      </p>
    );
  }
  return (
    <iframe
      title="live preview"
      src={preview.url}
      sandbox="allow-scripts allow-modals allow-downloads allow-pointer-lock"
      referrerPolicy="no-referrer"
      className="h-72 w-full rounded-md border border-hairline bg-white"
    />
  );
}

// The legible per-stage breakdown: which agent ran each stage, its state, score,
// cost, gaps, and wall-clock — all read from data the backend already emits.
export function StageLedger({ events, fallback = [] }) {
  const rows = useMemo(() => pipelineFromEvents(events, fallback), [events, fallback]);
  // "failed" counts as activity: a build that dies during its first stage
  // settles its only row to failed, and the ledger must show that error
  // instead of collapsing to "No stage activity yet".
  const active = rows.some(
    (r) => r.state === "running" || r.state === "done" || r.state === "failed",
  );
  if (!active) {
    return (
      <p className="px-4 py-3 font-mono text-[11px] text-ash/70">
        No stage activity yet — forge a build to watch the swarm work.
      </p>
    );
  }
  const fmtCost = (c) => (c != null ? `$${Number(c).toFixed(4)}` : "—");
  const fmtTime = (ms) => (ms != null ? `${(ms / 1000).toFixed(1)}s` : "—");
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left">
        <thead>
          <tr className="eyebrow border-b border-hairline text-ash">
            <th className="px-4 py-2 font-normal">Stage</th>
            <th className="px-4 py-2 font-normal">Agent</th>
            <th className="px-4 py-2 font-normal">State</th>
            <th className="px-4 py-2 font-normal">Score</th>
            <th className="px-4 py-2 font-normal">Cost</th>
            <th className="px-4 py-2 font-normal">Time</th>
            <th className="px-4 py-2 font-normal">Gaps</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-hairline/60">
          {rows.map((r) => (
            <tr key={r.stage}>
              <td className="px-4 py-2 font-mono text-xs text-bone">{r.stage}</td>
              <td className="px-4 py-2 font-mono text-[11px] text-ash">
                {r.agentName || r.agentType || "—"}
                {r.agentName && r.agentType && r.agentName !== r.agentType ? (
                  <span className="text-ash/50"> · {r.agentType}</span>
                ) : null}
              </td>
              <td className="px-4 py-2">
                <span className={`eyebrow text-[10px] ${stageTone(r.state)}`}>{r.state}</span>
              </td>
              <td className="px-4 py-2 font-mono text-xs text-ash">
                {r.score != null ? r.score : "—"}
              </td>
              <td className="px-4 py-2 font-mono text-xs text-ash">{fmtCost(r.cost)}</td>
              <td className="px-4 py-2 font-mono text-xs text-ash">{fmtTime(r.durationMs)}</td>
              <td className="px-4 py-2 font-mono text-[11px]">
                {r.gaps && r.gaps.length ? (
                  <span className="text-ember" title={r.gaps.join("\n")}>
                    {r.gaps.length} ⚑
                  </span>
                ) : r.state === "done" ? (
                  <span className="text-plasma">clean</span>
                ) : (
                  <span className="text-ash">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
