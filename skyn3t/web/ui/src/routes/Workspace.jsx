import React, { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { queryFn, apiFetch, apiPost } from "../api.js";
import { PageHeader, Panel, PanelHead, Pill, Empty } from "../components/ui.jsx";

// ---------------------------------------------------------------------------
// Two-pane live workspace (Spec 3): a running app on the left, an improve chat
// + diff timeline on the right. Wires /api/studio/serve + /api/studio/improve,
// streaming IMPROVE_*/SERVE_* events from the shared WebSocket.
// ---------------------------------------------------------------------------

function eventLine(e) {
  const p = e.payload || {};
  switch (e.type) {
    case "serve.started":
      return { glyph: "●", tone: "text-plasma", text: `serving at ${p.url}` };
    case "serve.stopped":
      return { glyph: "○", tone: "text-ash", text: "preview stopped" };
    case "improve.started":
      return { glyph: "▶", tone: "text-ember", text: `improving — ${p.goal || ""}` };
    case "improve.stage":
      return { glyph: "·", tone: "text-ash", text: `${p.stage || "stage"}…` };
    case "improve.completed": {
      const files = (p.files_changed || []).length;
      const ok = p.proof_passed ? "go" : "no_go";
      return {
        glyph: p.proof_passed ? "✔" : "✕",
        tone: p.proof_passed ? "text-plasma" : "text-ember",
        text: `done · ${files} file${files !== 1 ? "s" : ""} · score ${p.score ?? "—"} · ${ok}`,
      };
    }
    case "improve.failed":
      return { glyph: "✕", tone: "text-ember", text: `failed: ${p.error || "unknown"}` };
    default:
      return null;
  }
}

function ServePane({ slug, stream }) {
  const [served, setServed] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [reserved, setReserved] = useState(false);
  const lastImproveRef = useRef(null);

  // Reflect any already-running preview for this slug on mount / slug change.
  useEffect(() => {
    let alive = true;
    setServed(null);
    setErr(null);
    setReserved(false);
    lastImproveRef.current = null;
    if (!slug) return;
    apiFetch("/studio/serve")
      .then((r) => {
        if (!alive) return;
        const hit = (r.running || []).find((a) => a.slug === slug);
        if (hit) setServed(hit);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [slug]);

  // Identity of the most recent improve.completed for this slug, or "none".
  function latestImproveTag() {
    const evts = stream?.events || [];
    let latest = null;
    for (const e of evts) {
      if (e.type === "improve.completed" &&
          (e.payload?.slug === slug || !e.payload?.slug)) {
        latest = e;
      }
    }
    return latest ? latest.id || latest.timestamp || "x" : "none";
  }

  async function start() {
    setBusy(true);
    setErr(null);
    try {
      const r = await apiPost("/studio/serve", { slug });
      if (r.status === "running") {
        setServed(r);
        // Baseline at serve time so the NEXT improve (even the first one) triggers
        // a re-serve, but a completion that predates this serve does not.
        lastImproveRef.current = latestImproveTag();
      } else {
        setErr(r.detail?.reason || r.detail?.log_tail || `not servable (${r.status})`);
      }
    } catch (e) {
      setErr(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    try {
      await apiPost("/studio/serve/stop", { slug });
      setServed(null);
      setReserved(false);
    } catch (e) {
      setErr(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  const running = served && served.status === "running";

  // Auto re-serve when an improve completes for this slug: python/node servers
  // hold the old code in memory, so a restart is what surfaces the change (and
  // the fresh port re-renders the iframe). Closes the stale-preview dead-end.
  useEffect(() => {
    if (!running) return;
    const tag = latestImproveTag();
    if (tag === "none") return;
    if (lastImproveRef.current === null) {
      lastImproveRef.current = tag; // mount-detected running: set baseline once
      return;
    }
    if (lastImproveRef.current === tag) return;
    lastImproveRef.current = tag;
    setReserved(true);
    start(); // restart so the preview reflects the improved code
  }, [stream?.events, slug, running]);

  return (
    <Panel className="flex h-full flex-col overflow-hidden">
      <PanelHead
        label="Live app"
        right={
          <div className="flex items-center gap-2">
            {reserved && running ? (
              <span className="font-mono text-[10px] text-plasma/70" title="re-served after an improve">
                ↻ updated
              </span>
            ) : null}
            {running ? (
              <a
                href={served.url}
                target="_blank"
                rel="noopener noreferrer"
                className="font-mono text-[11px] text-plasma underline hover:text-plasma/70"
              >
                {served.url} ↗
              </a>
            ) : null}
            {running ? (
              <button onClick={stop} disabled={busy} className="btn-ghost disabled:opacity-50">
                {busy ? "…" : "Stop"}
              </button>
            ) : (
              <button onClick={start} disabled={busy || !slug} className="btn-ember disabled:opacity-50">
                {busy ? "Starting…" : "Serve"}
              </button>
            )}
          </div>
        }
      />
      {err ? <p className="px-4 py-2 font-mono text-[11px] text-ember">{err}</p> : null}
      <div className="flex-1 bg-ink/40">
        {running ? (
          <iframe
            title={`preview-${slug}`}
            src={served.url}
            // Fill the viewport height so a full-page app isn't clipped to a
            // small fixed box; the iframe scrolls internally for taller content.
            className="h-full min-h-[78vh] w-full border-0 bg-white"
          />
        ) : (
          <Empty icon="▢">
            {slug ? "Press Serve to launch a live preview." : "Pick a project to begin."}
          </Empty>
        )}
      </div>
    </Panel>
  );
}

function ImprovePane({ slug, stream }) {
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [note, setNote] = useState(null);
  // Correlation ids of improves dispatched from this pane. ImproveEngine emits
  // events keyed by the *manifest* slug, which can differ from the directory
  // slug the workspace uses — so we also match by correlation_id to keep the
  // timeline intact when those diverge.
  const [cids, setCids] = useState(() => new Set());

  const timeline = useMemo(() => {
    const evts = stream?.events || [];
    return evts
      .filter(
        (e) =>
          (e.type?.startsWith("improve.") || e.type?.startsWith("serve.")) &&
          (e.payload?.slug === slug ||
            (e.correlation_id && cids.has(e.correlation_id))),
      )
      .map((e) => ({ ...e, line: eventLine(e) }))
      .filter((e) => e.line);
  }, [stream?.events, slug, cids]);

  async function submit() {
    if (!goal.trim() || !slug) return;
    setBusy(true);
    setErr(null);
    setNote(null);
    try {
      const r = await apiPost("/studio/improve", { slug, goal });
      if (r.accepted) {
        if (r.correlation_id) {
          setCids((prev) => new Set(prev).add(r.correlation_id));
        }
        setNote(`dispatched · ${r.correlation_id?.slice(0, 8)}`);
        setGoal("");
      } else {
        setErr(r.reason || "improve unavailable");
      }
    } catch (e) {
      setErr(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel className="flex h-full flex-col overflow-hidden">
      <PanelHead
        label="Improve"
        right={note ? <span className="font-mono text-[11px] text-plasma">{note}</span> : null}
      />
      <div className="border-b border-hairline px-4 py-3">
        <textarea
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="Describe what to add or change, in plain English…"
          rows={3}
          className="w-full resize-none rounded border border-hairline bg-ink/60 px-3 py-2 font-mono text-xs text-bone placeholder:text-ash/50 focus:border-ember focus:outline-none"
        />
        <div className="mt-2 flex items-center justify-between">
          {err ? (
            <span className="font-mono text-[11px] text-ember">{err}</span>
          ) : (
            <span className="font-mono text-[11px] text-ash/60">
              runs audit → edit → verify → deliver
            </span>
          )}
          <button
            onClick={submit}
            disabled={busy || !goal.trim() || !slug}
            className="btn-ember disabled:opacity-50"
          >
            {busy ? "Sending…" : "Improve"}
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-3">
        {timeline.length === 0 ? (
          <Empty icon="≋">No activity yet. Serve the app, then request a change.</Empty>
        ) : (
          <ul className="space-y-1.5">
            {timeline.map((e, i) => (
              <li key={`${e.id || e.timestamp || i}-${i}`} className="flex items-start gap-2">
                <span className={`mt-px font-mono text-xs ${e.line.tone}`}>{e.line.glyph}</span>
                <span className="font-mono text-[11px] text-ash">{e.line.text}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Panel>
  );
}

export default function Workspace({ stream }) {
  const [params, setParams] = useSearchParams();
  const slug = params.get("slug") || "";

  const { data } = useQuery({ queryKey: ["projects"], queryFn: queryFn("/projects") });
  const projects = Array.isArray(data) ? data : data?.projects || [];

  function pick(next) {
    const p = new URLSearchParams(params);
    if (next) p.set("slug", next);
    else p.delete("slug");
    setParams(p, { replace: true });
  }

  const current = projects.find((p) => p.slug === slug);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="Foundry · Live Workspace"
        title="Workspace"
        sub="Run a delivered app and refine it in place — serve on the left, improve on the right."
        actions={
          <select
            value={slug}
            onChange={(e) => pick(e.target.value)}
            className="rounded border border-hairline bg-ink/60 px-3 py-1.5 font-mono text-xs text-bone focus:border-ember focus:outline-none"
          >
            <option value="">Select a project…</option>
            {projects.map((p) => (
              <option key={p.slug} value={p.slug}>
                {p.slug}
                {p.stack ? ` · ${p.stack}` : ""}
              </option>
            ))}
          </select>
        }
      />

      {slug && current ? (
        <div className="mb-3 flex items-center gap-2">
          <Pill tone="plasma">{current.stack || "—"}</Pill>
          <span className="font-mono text-[11px] text-ash">
            {current.status || "—"} · score {current.score ?? "—"}
          </span>
        </div>
      ) : null}

      <div className="grid flex-1 grid-cols-1 gap-4 lg:grid-cols-2">
        <ServePane slug={slug} stream={stream} />
        <ImprovePane slug={slug} stream={stream} />
      </div>
    </div>
  );
}
