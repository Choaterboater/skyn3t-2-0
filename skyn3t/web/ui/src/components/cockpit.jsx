import React, { useMemo } from "react";

// Build per-stage debug rows from the live event stream. Event-driven: we render
// whatever stages appear, so we never drift from the backend stage vocabulary.
export function debugRowsFromEvents(events) {
  const rows = new Map();
  for (const e of events) {
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
  for (const e of events) {
    if (e.type === "build.stage.artifact.snapshot") snap = e.payload;
  }
  return snap; // { build_id, stage, files: [...] } | null
}

export function latestRunningSlug(events) {
  let slug = null;
  for (const e of events) {
    if (e.type === "build.started" && e.payload?.slug) slug = e.payload.slug;
  }
  return slug;
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
    <ul className="max-h-64 overflow-y-auto px-4 py-2 font-mono text-[11px] text-ash">
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
  // Rendered preview works for relative-asset apps (e.g. static_html) and in
  // loopback (no-token) mode. With a token set, the iframe may not authenticate.
  return (
    <iframe
      title="live preview"
      src={`/api/projects/${slug}/index.html`}
      className="h-72 w-full rounded-md border border-hairline bg-white"
    />
  );
}
