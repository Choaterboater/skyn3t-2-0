import React, { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiFetch, apiPost } from "../api.js";
import {
  PageHeader,
  Panel,
  PanelHead,
  Empty,
  SignalGrid,
} from "../components/ui.jsx";
import {
  countWorkspaceActivity,
  workspaceEventMatches,
} from "../workspaceSignals.js";
import {
  addPin,
  canSubmit,
  elementLabel,
  pinsToPayload,
  removePin,
  updatePinComment,
  MAX_PINS,
} from "../annotations.js";

// ---------------------------------------------------------------------------
// Two-pane live workspace (Spec 3): a running app on the left, an improve chat
// + diff timeline on the right. Wires /api/studio/serve + /api/studio/improve,
// streaming IMPROVE_*/SERVE_* events from the shared WebSocket.
// ---------------------------------------------------------------------------

function formatElapsed(ms) {
  const seconds = Math.max(0, Math.floor(Number(ms || 0) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

function eventLine(e) {
  const p = e.payload || {};
  switch (e.type) {
    case "serve.starting":
      return {
        glyph: "·",
        tone: "text-ash",
        text: `preview ${p.phase || "preparing"} — ${p.message || "working"}`,
      };
    case "serve.failed":
      return { glyph: "✕", tone: "text-ember", text: `preview failed: ${p.reason || "unknown"}` };
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
      // A zero-change run must never render as a green success: score/proof
      // describe the UNCHANGED tree, not the goal (8 silent no-ops shipped as
      // "done · score 100 · go" before this). Say what happened instead.
      if (files === 0) {
        const d = p.detail || {};
        const reasons = Object.values(d.skipped || {});
        const why = reasons.includes("already_satisfied")
          ? "the model says this is already implemented"
          : d.no_targets_found
            ? "no files matched the goal"
            : "the model did not produce a usable change — try a more specific goal";
        return { glyph: "△", tone: "text-ember", text: `finished, but NO files were changed — ${why}` };
      }
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

function ServePane({
  slug,
  stream,
  editorMode = false,
  onElementSelected,
  refreshRevision = 0,
}) {
  const [served, setServed] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [reserved, setReserved] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const historyQuery = useQuery({
    queryKey: ["serve-history", slug],
    queryFn: () => apiFetch(`/studio/serve/history?slug=${encodeURIComponent(slug)}`),
    enabled: Boolean(slug),
    refetchInterval: served?.status === "starting" ? 1000 : false,
    refetchOnWindowFocus: false,
  });
  const lastImproveRef = useRef(null);
  const iframeRef = useRef(null);

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
      if (r.status === "running" || r.status === "starting") {
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
  const starting = served && served.status === "starting";
  const active = running || starting;
  const latestLaunch = historyQuery.data?.launches?.[0] || null;
  const currentMessage = served?.detail?.message || latestLaunch?.message || "Preparing an isolated Docker preview";
  const elapsedMs = starting
    ? Math.max(Number(latestLaunch?.elapsed_ms || served?.detail?.elapsed_ms || 0), nowMs - Number(latestLaunch?.started_at_ms || nowMs))
    : Number(latestLaunch?.elapsed_ms || 0);

  useEffect(() => {
    if (!starting) return undefined;
    setNowMs(Date.now());
    const interval = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [starting]);

  // Streamed terminal events update this pane without waiting for the 15-second
  // status poll after Docker finishes a long first-time dependency prepare.
  useEffect(() => {
    const events = stream?.events || [];
    const latest = [...events].reverse().find((event) =>
      event.payload?.slug === slug && ["serve.started", "serve.starting", "serve.failed", "serve.stopped"].includes(event.type)
    );
    if (!latest) return;
    void historyQuery.refetch();
    if (latest.type === "serve.started") {
      setErr(null);
      setServed({ slug, url: latest.payload.url, port: latest.payload.port, status: "running" });
    } else if (latest.type === "serve.starting") {
      setErr(null);
      setServed((previous) => ({
        ...(previous || { slug, url: "", port: 0 }),
        status: "starting",
        detail: {
          ...(previous?.detail || {}),
          phase: latest.payload?.phase,
          message: latest.payload?.message,
          elapsed_ms: latest.payload?.elapsed_ms,
          launch_id: latest.payload?.launch_id,
        },
      }));
    } else if (latest.type === "serve.failed") {
      setServed(null);
      setErr(latest.payload?.reason || "Docker preview failed");
    } else if (latest.type === "serve.stopped") {
      setServed(null);
    }
  }, [stream?.events, slug]);

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

  useEffect(() => {
    function receiveSelection(event) {
      if (
        !editorMode ||
        event.source !== iframeRef.current?.contentWindow ||
        event.data?.type !== "skyn3t-element-selected" ||
        !event.data?.signature
      ) {
        return;
      }
      onElementSelected?.(event.data.signature);
    }
    window.addEventListener("message", receiveSelection);
    return () => window.removeEventListener("message", receiveSelection);
  }, [editorMode, onElementSelected]);

  const iframeUrl = useMemo(() => {
    if (!running) return "";
    try {
      const url = new URL(served.url);
      if (editorMode) url.searchParams.set("skyn3t_editor", "1");
      url.searchParams.set("skyn3t_refresh", String(refreshRevision));
      return url.toString();
    } catch (_) {
      return served.url;
    }
  }, [running, served?.url, editorMode, refreshRevision]);

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
            {active ? (
              <button onClick={stop} disabled={busy} className="btn-ghost disabled:opacity-50">
                {busy ? "…" : starting ? "Cancel" : "Stop"}
              </button>
            ) : (
              <button onClick={start} disabled={busy || !slug} className="btn-ember disabled:opacity-50">
                {busy ? "Starting…" : err ? "Retry Serve" : "Serve"}
              </button>
            )}
          </div>
        }
      />
      {err ? <p className="px-4 py-2 font-mono text-[11px] text-ember">{err}</p> : null}
      {latestLaunch ? (
        <div className="border-b border-hairline bg-ink/20 px-4 py-3">
          <div className="mb-2 flex items-center justify-between gap-3 font-mono text-[10px] uppercase tracking-wider text-ash/70">
            <span>Launch timeline</span>
            <span className={starting ? "text-ember" : latestLaunch.status === "running" ? "text-plasma" : "text-ash"}>
              {starting ? `live · ${formatElapsed(elapsedMs)}` : latestLaunch.status}
            </span>
          </div>
          <ol className="space-y-1">
            {(latestLaunch.timeline || []).slice(-6).map((step, index) => (
              <li key={`${step.at || index}-${step.phase || "phase"}`} className="flex items-start gap-2 font-mono text-[11px] text-ash">
                <span className={index === (latestLaunch.timeline || []).slice(-6).length - 1 ? "text-ember" : "text-ash/40"}>●</span>
                <span>{step.message || step.phase || "working"}</span>
                <span className="ml-auto text-ash/50">{formatElapsed(step.elapsed_ms)}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      <div className="flex-1 bg-ink/40">
        {running ? (
          <iframe
            ref={iframeRef}
            key={`${iframeUrl}-${editorMode ? "edit" : "run"}`}
            title={`preview-${slug}`}
            src={iframeUrl}
            // Fill the viewport height so a full-page app isn't clipped to a
            // small fixed box; the iframe scrolls internally for taller content.
            className="h-full min-h-[78vh] w-full border-0 bg-white"
          />
        ) : (
          <Empty icon="▢">
            {starting
              ? `${currentMessage} · ${formatElapsed(elapsedMs)}. You can cancel it at any time.`
              : slug ? "Press Serve to launch a live preview." : "Pick a project to begin."}
          </Empty>
        )}
      </div>
    </Panel>
  );
}

function ImprovePane({ slug, stream, cids = new Set(), onDispatched }) {
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [note, setNote] = useState(null);

  const timeline = useMemo(() => {
    const evts = stream?.events || [];
    return evts
      .filter((e) => workspaceEventMatches(e, slug, cids))
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
          onDispatched?.(slug, r.correlation_id);
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
          aria-label={"Improvement goal for " + (slug || "selected project")}
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

function VisualEditorPane({ slug, signature, onEdited }) {
  const qc = useQueryClient();
  const [occurrences, setOccurrences] = useState([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [textValue, setTextValue] = useState("");
  const [imageValue, setImageValue] = useState("");
  const [tokenName, setTokenName] = useState("--accent");
  const [tokenValue, setTokenValue] = useState("#ff6b35");
  const [selector, setSelector] = useState("");
  const [propertyName, setPropertyName] = useState("gap");
  const [layoutValue, setLayoutValue] = useState("1rem");
  const [breakpoint, setBreakpoint] = useState("base");

  const style = useQuery({
    queryKey: ["visual-editor-style", slug],
    queryFn: queryFn(`/projects/${encodeURIComponent(slug)}/visual-editor/style`),
    enabled: Boolean(slug),
    retry: 0,
  });

  useEffect(() => {
    setOccurrences([]);
    setSelectedIndex(0);
    setErr("");
    setNote("");
    if (!slug || !signature) return;
    setTextValue(signature.text || "");
    setImageValue(signature.image_src || "");
    setSelector(
      signature.element_id
        ? `#${signature.element_id}`
        : signature.classes?.[0]
          ? `.${signature.classes[0]}`
          : "",
    );
    let active = true;
    setBusy(true);
    apiPost(`/projects/${encodeURIComponent(slug)}/visual-editor/inspect`, {
      signature,
      limit: 20,
    })
      .then((result) => {
        if (!active) return;
        setOccurrences(result.occurrences || []);
        setSelectedIndex(0);
        if (!(result.occurrences || []).length) {
          setNote("No safe static source match. Try a more specific element.");
        }
      })
      .catch((error) => {
        if (active) setErr(error.message);
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, [slug, signature]);

  const selected = occurrences[selectedIndex] || null;
  const editable = new Set(selected?.editable || []);
  const styleSha = style.data?.style?.current_sha || "";

  async function apply(body) {
    if (!slug) return;
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const result = await apiPost(
        `/projects/${encodeURIComponent(slug)}/visual-editor/edit`,
        body,
      );
      const verification = result.verification || {};
      setNote(
        verification.passed
          ? "Saved and verified."
          : `Saved, but proof is ${verification.proof_ladder?.status || "not passing"}.`,
      );
      await qc.invalidateQueries({ queryKey: ["visual-editor-style", slug] });
      await qc.invalidateQueries({ queryKey: ["projects"] });
      onEdited?.(result);
    } catch (error) {
      setErr(error.message);
    } finally {
      setBusy(false);
    }
  }

  function applySource(kind, value) {
    if (!selected || !signature) return;
    void apply({
      kind,
      value,
      signature,
      relative_path: selected.relative_path,
      base_sha: selected.current_sha,
      occurrence_id: selected.occurrence_id,
      line: selected.line,
    });
  }

  return (
    <Panel className="flex h-full flex-col overflow-hidden">
      <PanelHead
        label="Visual editor"
        right={
          <span className="font-mono text-[10px] text-plasma">
            {busy ? "working…" : "click an element"}
          </span>
        }
      />
      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {!signature ? (
          <Empty icon="⌖">
            Click an element in the live app. Selection mode intercepts clicks without
            executing edits.
          </Empty>
        ) : (
          <>
            <div className="rounded border border-hairline bg-void/40 p-3">
              <div className="eyebrow">Selected element</div>
              <div className="mt-2 font-mono text-xs text-bone">
                &lt;{signature.tag || "element"}&gt;
                {signature.element_id ? ` #${signature.element_id}` : ""}
                {(signature.classes || []).map((name) => ` .${name}`).join("")}
              </div>
              <p className="mt-1 line-clamp-3 text-xs text-ash">
                {signature.text || signature.image_src || "No static text"}
              </p>
            </div>

            {occurrences.length ? (
              <label className="block">
                <span className="eyebrow">Source match</span>
                <select
                  className="field mt-2 w-full"
                  value={selectedIndex}
                  onChange={(event) => setSelectedIndex(Number(event.target.value))}
                >
                  {occurrences.map((item, index) => (
                    <option key={item.occurrence_id} value={index}>
                      {item.relative_path}:{item.line} · {Math.round(item.score * 100)}%
                    </option>
                  ))}
                </select>
              </label>
            ) : null}

            {editable.has("text") ? (
              <div>
                <div className="eyebrow">Text</div>
                <textarea
                  aria-label="Selected element text"
                  className="field mt-2 min-h-20 w-full"
                  value={textValue}
                  onChange={(event) => setTextValue(event.target.value)}
                />
                <button
                  className="btn-ember mt-2"
                  disabled={busy}
                  onClick={() => applySource("text", textValue)}
                >
                  Save text
                </button>
              </div>
            ) : null}

            {editable.has("image_src") ? (
              <div>
                <div className="eyebrow">Image source</div>
                <input
                  aria-label="Selected image source"
                  className="field mt-2 w-full"
                  value={imageValue}
                  onChange={(event) => setImageValue(event.target.value)}
                />
                <button
                  className="btn-ember mt-2"
                  disabled={busy}
                  onClick={() => applySource("image_src", imageValue)}
                >
                  Save image
                </button>
              </div>
            ) : null}

            <div className="border-t border-hairline pt-4">
              <div className="eyebrow">Design token</div>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <input
                  aria-label="CSS design token"
                  className="field"
                  value={tokenName}
                  onChange={(event) => setTokenName(event.target.value)}
                />
                <input
                  aria-label="CSS design token value"
                  className="field"
                  value={tokenValue}
                  onChange={(event) => setTokenValue(event.target.value)}
                />
              </div>
              <button
                className="btn-ghost mt-2"
                disabled={busy || !styleSha}
                onClick={() =>
                  apply({
                    kind: "design_token",
                    base_sha: styleSha,
                    css_property: tokenName,
                    value: tokenValue,
                  })
                }
              >
                Set token
              </button>
            </div>

            <div className="border-t border-hairline pt-4">
              <div className="eyebrow">Responsive layout</div>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <input
                  aria-label="CSS selector"
                  className="field"
                  placeholder=".card or #hero"
                  value={selector}
                  onChange={(event) => setSelector(event.target.value)}
                />
                <select
                  aria-label="Responsive breakpoint"
                  className="field"
                  value={breakpoint}
                  onChange={(event) => setBreakpoint(event.target.value)}
                >
                  {["base", "sm", "md", "lg", "xl"].map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
                <select
                  aria-label="Layout property"
                  className="field"
                  value={propertyName}
                  onChange={(event) => setPropertyName(event.target.value)}
                >
                  {[
                    "display",
                    "gap",
                    "padding",
                    "margin",
                    "width",
                    "max-width",
                    "height",
                    "align-items",
                    "justify-content",
                    "flex-direction",
                    "flex-wrap",
                    "grid-template-columns",
                  ].map((value) => (
                    <option key={value} value={value}>{value}</option>
                  ))}
                </select>
                <input
                  aria-label="Layout value"
                  className="field"
                  value={layoutValue}
                  onChange={(event) => setLayoutValue(event.target.value)}
                />
              </div>
              <button
                className="btn-ghost mt-2"
                disabled={busy || !styleSha || !selector}
                onClick={() =>
                  apply({
                    kind: "layout",
                    base_sha: styleSha,
                    selector,
                    css_property: propertyName,
                    value: layoutValue,
                    breakpoint,
                  })
                }
              >
                Apply layout
              </button>
            </div>
          </>
        )}
        {style.error ? (
          <p className="font-mono text-[11px] text-ember">{style.error.message}</p>
        ) : null}
        {err ? <p className="font-mono text-[11px] text-ember">{err}</p> : null}
        {note ? <p className="font-mono text-[11px] text-plasma">{note}</p> : null}
      </div>
    </Panel>
  );
}

function AnnotatePane({ slug, pins, onPinsChange, onDispatched, onSubmitted }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    if (!slug || !canSubmit(pins)) return;
    setBusy(true);
    setErr("");
    try {
      const result = await apiPost(
        `/projects/${encodeURIComponent(slug)}/annotations/improve`,
        pinsToPayload(pins),
      );
      if (result.accepted) {
        if (result.correlation_id) onDispatched?.(slug, result.correlation_id);
        onPinsChange?.([]);
        // Hand off to the AI improve pane so the run's progress shows in the
        // existing timeline.
        onSubmitted?.(result);
      } else {
        setErr(result.reason || "improve unavailable");
      }
    } catch (error) {
      setErr(String(error.message));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel className="flex h-full flex-col overflow-hidden">
      <PanelHead
        label="Annotate"
        right={
          <span className="font-mono text-[10px] text-plasma">
            {busy ? "sending…" : `${pins.length}/${MAX_PINS} pins`}
          </span>
        }
      />
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {!pins.length ? (
          <Empty icon="✎">
            Click elements in the live app to drop numbered pins, write one
            comment per pin, then send them all as a single improve goal. Pins
            create goals, never direct edits.
          </Empty>
        ) : (
          <ol className="space-y-3">
            {pins.map((pin, index) => (
              <li
                key={pin.id}
                className="rounded border border-hairline bg-void/40 p-3"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs text-bone">
                    <span className="text-ember">#{index + 1}</span>{" "}
                    {elementLabel(pin.signature)}
                  </span>
                  <button
                    className="btn-ghost"
                    aria-label={`Delete pin ${index + 1}`}
                    onClick={() => onPinsChange?.(removePin(pins, pin.id))}
                  >
                    Delete
                  </button>
                </div>
                <textarea
                  aria-label={`Comment for pin ${index + 1}`}
                  className="field mt-2 min-h-14 w-full"
                  placeholder="What should change here?"
                  value={pin.comment}
                  onChange={(event) =>
                    onPinsChange?.(
                      updatePinComment(pins, pin.id, event.target.value),
                    )
                  }
                />
              </li>
            ))}
          </ol>
        )}
        {err ? <p className="font-mono text-[11px] text-ember">{err}</p> : null}
      </div>
      <div className="flex items-center justify-between border-t border-hairline px-4 py-3">
        <span className="font-mono text-[11px] text-ash/60">
          one goal · per-element source evidence
        </span>
        <button
          className="btn-ember disabled:opacity-50"
          disabled={busy || !slug || !canSubmit(pins)}
          onClick={submit}
        >
          {busy ? "Sending…" : "Send annotations"}
        </button>
      </div>
    </Panel>
  );
}

function ProductPane({ slug, buildId }) {
  const qc = useQueryClient();
  const actionInFlightRef = useRef(false);
  const [goal, setGoal] = useState("");
  const [personas, setPersonas] = useState("");
  const [nonGoals, setNonGoals] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  const productQuery = useQuery({
    queryKey: ["project-product", slug],
    queryFn: queryFn(`/projects/${encodeURIComponent(slug)}/product`),
    enabled: Boolean(slug),
    retry: 0,
  });
  const product = productQuery.data?.product || null;

  useEffect(() => {
    setGoal(product?.goal || "");
    setPersonas((product?.personas || []).join(", "));
    setNonGoals((product?.non_goals || []).join("\n"));
  }, [slug, product?.version]);

  useEffect(() => {
    setErr("");
    setNote("");
  }, [slug]);

  function splitList(value, separator) {
    return value
      .split(separator)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function productPatch() {
    const next = {
      goal: goal.trim(),
      personas: splitList(personas, ","),
      non_goals: splitList(nonGoals, /\r?\n/),
    };
    return Object.fromEntries(
      Object.entries(next).filter(
        ([key, value]) => JSON.stringify(value) !== JSON.stringify(product[key]),
      ),
    );
  }

  async function persistProductPatch(patch) {
    const result = await apiFetch(
      `/projects/${encodeURIComponent(slug)}/product`,
      {
        method: "PATCH",
        body: JSON.stringify({
          base_version: product.version,
          patch,
          reason: "Product contract refined in Workspace",
        }),
      },
    );
    qc.setQueryData(["project-product", slug], result);
    return result.product;
  }

  function beginAction(action) {
    if (actionInFlightRef.current) return false;
    actionInFlightRef.current = true;
    setBusy(action);
    setErr("");
    setNote("");
    return true;
  }

  function endAction() {
    actionInFlightRef.current = false;
    setBusy("");
  }

  async function save() {
    if (!product) return;
    const patch = productPatch();
    if (!Object.keys(patch).length) {
      setNote("No product-contract changes to save.");
      return;
    }
    if (!beginAction("save")) return;
    try {
      const savedProduct = await persistProductPatch(patch);
      setNote(`Saved product contract v${savedProduct.version}.`);
    } catch (error) {
      setErr(error.message);
      await qc.invalidateQueries({ queryKey: ["project-product", slug] });
    } finally {
      endAction();
    }
  }

  async function research() {
    if (!product || !beginAction("research")) return;
    try {
      const result = await apiPost(
        `/projects/${encodeURIComponent(slug)}/product/research`,
        {
          base_version: product.version,
          force_refresh: true,
        },
      );
      qc.setQueryData(["project-product", slug], {
        slug,
        available: true,
        product: result.product,
      });
      const sources = result.research?.sources?.length || 0;
      const ideas = result.research?.backlog?.length || 0;
      setNote(
        result.research?.status === "unavailable"
          ? `Research unavailable: ${result.research?.error || "GitHub did not respond"}.`
          : `Research recorded ${sources} source${sources === 1 ? "" : "s"} and ${ideas} optional idea${ideas === 1 ? "" : "s"}.`,
      );
    } catch (error) {
      setErr(error.message);
      await qc.invalidateQueries({ queryKey: ["project-product", slug] });
    } finally {
      endAction();
    }
  }

  async function buildFromContract() {
    if (!product || !buildId || !beginAction("rebuild")) return;
    let contract = product;
    try {
      const patch = productPatch();
      if (Object.keys(patch).length) {
        try {
          contract = await persistProductPatch(patch);
        } catch (error) {
          setErr(error.message);
          await qc.invalidateQueries({ queryKey: ["project-product", slug] });
          return;
        }
      }
      const result = await apiPost("/builds/rebuild", {
        build_id: buildId,
        reuse_slug: false,
      });
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["builds"] }),
        qc.invalidateQueries({ queryKey: ["projects"] }),
      ]);
      setNote(
        `Queued build ${result.build_id} from product contract v${contract.version}.`,
      );
    } catch (error) {
      setErr(error.message);
    } finally {
      endAction();
    }
  }

  const contractDirty = Boolean(
    product && Object.keys(productPatch()).length,
  );

  return (
    <Panel className="flex h-full flex-col overflow-hidden">
      <PanelHead
        label="Product intelligence"
        right={
          product ? (
            <span className="font-mono text-[10px] text-plasma">
              contract v{product.version}
            </span>
          ) : null
        }
      />
      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {!slug ? (
          <Empty icon="◎">Pick a project to inspect its product contract.</Empty>
        ) : productQuery.isLoading ? (
          <Empty icon="◌">Loading product contract…</Empty>
        ) : productQuery.error ? (
          <p className="font-mono text-[11px] text-ember">
            {productQuery.error.message}
          </p>
        ) : !productQuery.data?.available ? (
          <Empty icon="△">
            This project predates product intelligence. Rebuild it to create a
            versioned contract.
          </Empty>
        ) : (
          <>
            <label className="block">
              <span className="eyebrow">Product goal</span>
              <textarea
                aria-label="Product goal"
                className="field mt-2 min-h-20 w-full"
                value={goal}
                onChange={(event) => setGoal(event.target.value)}
              />
            </label>
            <label className="block">
              <span className="eyebrow">Personas</span>
              <input
                aria-label="Product personas"
                className="field mt-2 w-full"
                value={personas}
                placeholder="developer, designer"
                onChange={(event) => setPersonas(event.target.value)}
              />
            </label>
            <label className="block">
              <span className="eyebrow">Non-goals · one per line</span>
              <textarea
                aria-label="Product non-goals"
                className="field mt-2 min-h-16 w-full"
                value={nonGoals}
                onChange={(event) => setNonGoals(event.target.value)}
              />
            </label>
            <div className="flex flex-wrap gap-2">
              <button
                className="btn-ember"
                disabled={Boolean(busy) || !buildId}
                onClick={buildFromContract}
                title={
                  buildId
                    ? "Queue a new project from this contract; the current app stays untouched."
                    : "No source build is available for this project."
                }
              >
                {busy === "rebuild"
                  ? "Queueing new version…"
                  : contractDirty
                    ? "Save & build new version"
                    : "Build new version from contract"}
              </button>
              <button
                className="btn-ghost"
                disabled={Boolean(busy)}
                onClick={save}
              >
                {busy === "save" ? "Saving…" : "Save contract"}
              </button>
              <button
                className="btn-ghost"
                disabled={Boolean(busy)}
                onClick={research}
              >
                {busy === "research" ? "Researching…" : "Research similar apps"}
              </button>
            </div>
            <div className="border-t border-hairline pt-4">
              <div className="eyebrow">
                Current requirements · {product.requirements?.length || 0}
              </div>
              <ul className="mt-2 space-y-2">
                {(product.requirements || []).map((item) => (
                  <li
                    key={item.id}
                    className="rounded border border-hairline bg-void/40 p-2 text-xs text-bone"
                  >
                    {item.text}
                  </li>
                ))}
              </ul>
            </div>
            <div className="border-t border-hairline pt-4">
              <div className="eyebrow">
                Optional backlog · {product.backlog?.length || 0}
              </div>
              {(product.backlog || []).length ? (
                <ul className="mt-2 space-y-2">
                  {product.backlog.slice(0, 12).map((item) => (
                    <li key={item.id} className="text-xs text-ash">
                      <span className="text-bone">{item.title}</span>
                      {item.source ? ` · ${item.source}` : ""}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-xs text-ash">No optional ideas recorded.</p>
              )}
            </div>
            {(product.research_sources || []).length ? (
              <div className="border-t border-hairline pt-4">
                <div className="eyebrow">
                  Research sources · {product.research_sources.length}
                </div>
                <ul className="mt-2 space-y-2">
                  {product.research_sources.slice(0, 8).map((source) => (
                    <li key={source.id}>
                      <a
                        className="font-mono text-[11px] text-plasma underline"
                        href={source.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {source.repository || source.url} ↗
                      </a>
                      <div className="font-mono text-[10px] text-ash">
                        {source.license} · {source.usage_policy} · source copy disabled
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </>
        )}
        {err ? <p className="font-mono text-[11px] text-ember">{err}</p> : null}
        {note ? <p className="font-mono text-[11px] text-plasma">{note}</p> : null}
      </div>
    </Panel>
  );
}

function visualQualityArtifactUrl(slug, runId, screenshot) {
  const raw = String(screenshot || "").replaceAll("\\", "/");
  const prefix = `.skyn3t/visual-quality/${runId}/`;
  if (!slug || !runId || !raw.startsWith(prefix)) return "";
  const artifact = raw.slice(prefix.length);
  return `/api/projects/${encodeURIComponent(slug)}/visual-quality/runs/${encodeURIComponent(runId)}/artifacts/${artifact.split("/").map(encodeURIComponent).join("/")}`;
}

function visualQualityIssues(run) {
  const issues = [];
  for (const round of run?.visual_loop?.rounds || []) {
    for (const issue of round?.issues || []) issues.push(String(issue));
    for (const issue of round?.layout_audit?.issues || []) issues.push(String(issue));
  }
  for (const viewport of run?.after?.viewports || run?.before?.viewports || []) {
    for (const issue of viewport?.issues || []) {
      issues.push(String(issue?.message || issue?.code || "visual proof issue"));
    }
  }
  return [...new Set(issues)].slice(0, 8);
}

function VisualQualityPane({ slug, onCompleted }) {
  const qc = useQueryClient();
  const visual = useQuery({
    queryKey: ["visual-quality", slug],
    queryFn: queryFn(`/projects/${encodeURIComponent(slug)}/visual-quality`),
    enabled: Boolean(slug),
    retry: 0,
  });
  const run = useMutation({
    mutationFn: () => apiPost(`/projects/${encodeURIComponent(slug)}/visual-quality/run`, {}),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["visual-quality", slug] });
    },
  });

  useEffect(() => {
    if (!visual.data?.running) return undefined;
    const timer = window.setInterval(() => {
      void visual.refetch().then((result) => {
        if (!result.data?.running) onCompleted?.();
      });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [visual.data?.running, visual.refetch, onCompleted]);

  const latest = visual.data?.runs?.[0] || null;
  const viewports = latest?.after?.viewports?.length
    ? latest.after.viewports
    : latest?.before?.viewports || [];
  const issues = visualQualityIssues(latest);
  const state = visual.data?.running
    ? "reviewing and repairing locally…"
    : latest?.status === "completed"
      ? latest?.visual_loop?.passed
        ? "passed visual review"
        : "finished with remaining visual findings"
      : latest?.status === "skipped"
        ? latest?.reason || "visual tooling was unavailable"
        : latest?.status === "failed"
          ? latest?.reason || "visual review failed"
          : "no visual-quality run yet";

  return (
    <Panel className="flex h-full flex-col overflow-hidden">
      <PanelHead
        label="Visual quality"
        right={
          <button
            type="button"
            className="btn-ember"
            disabled={!slug || run.isPending || visual.data?.running}
            onClick={() => run.mutate()}
          >
            {run.isPending || visual.data?.running ? "Running…" : "Run review"}
          </button>
        }
      />
      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {!slug ? (
          <Empty icon="◌">Pick a delivered web project to review its visual quality.</Empty>
        ) : (
          <>
            <div className="rounded border border-hairline bg-void/40 p-3">
              <div className="eyebrow">Local visual loop</div>
              <p className="mt-2 text-sm text-bone" aria-live="polite">{state}</p>
              <p className="mt-1 text-xs text-ash">
                Captures desktop and mobile evidence, checks rendering, then uses the proof-backed Improve path for clear fixes.
              </p>
            </div>

            {issues.length ? (
              <div>
                <div className="eyebrow mb-2">Findings</div>
                <ul className="space-y-1.5">
                  {issues.map((issue) => (
                    <li key={issue} className="rounded border border-ember/30 bg-ember/5 px-3 py-2 text-xs text-ash">
                      {issue}
                    </li>
                  ))}
                </ul>
              </div>
            ) : latest ? (
              <p className="text-xs text-ash">No specific findings were retained for this run.</p>
            ) : null}

            {viewports.length ? (
              <div>
                <div className="eyebrow mb-2">Latest evidence</div>
                <div className="grid gap-3 sm:grid-cols-2">
                  {viewports.map((viewport) => {
                    const src = visualQualityArtifactUrl(slug, latest?.run_id, viewport?.screenshot);
                    return (
                      <div key={viewport?.name || viewport?.width} className="overflow-hidden rounded border border-hairline bg-ink/40">
                        <div className="flex items-center justify-between border-b border-hairline px-3 py-2 font-mono text-[10px] text-ash">
                          <span>{viewport?.name || "viewport"}</span>
                          <span>{viewport?.width} × {viewport?.height}</span>
                        </div>
                        {src ? (
                          <img src={src} alt={`${viewport?.name || "Visual"} evidence for ${slug}`} className="block h-44 w-full object-cover object-top" />
                        ) : (
                          <div className="flex h-44 items-center justify-center p-3 text-center text-xs text-ash">Screenshot evidence was unavailable.</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}

            {latest?.assets?.length ? (
              <div>
                <div className="eyebrow mb-2">Real-image sources</div>
                {latest.assets.map((asset) => (
                  <p key={asset.local_path || asset.url} className="rounded border border-hairline px-3 py-2 text-xs text-ash">
                    {asset.attribution || asset.title || "Recorded image source"}
                  </p>
                ))}
              </div>
            ) : null}

            {run.isError ? <p className="font-mono text-xs text-ember">{String(run.error?.message || run.error)}</p> : null}
            {visual.isError ? <p className="font-mono text-xs text-ember">{String(visual.error?.message || visual.error)}</p> : null}
          </>
        )}
      </div>
    </Panel>
  );
}
export default function Workspace({ stream }) {
  const [params, setParams] = useSearchParams();
  const slug = params.get("slug") || "";
  const [improveCidsBySlug, setImproveCidsBySlug] = useState({});
  const [rightMode, setRightMode] = useState("improve");
  const [selectedSignature, setSelectedSignature] = useState(null);
  const [previewRevision, setPreviewRevision] = useState(0);
  const [annotationPins, setAnnotationPins] = useState([]);

  const { data } = useQuery({ queryKey: ["projects"], queryFn: queryFn("/projects") });
  const projects = Array.isArray(data) ? data : data?.projects || [];

  function pick(next) {
    const p = new URLSearchParams(params);
    if (next) p.set("slug", next);
    else p.delete("slug");
    setParams(p, { replace: true });
    setSelectedSignature(null);
    setAnnotationPins([]);
  }

  const current = projects.find((p) => p.slug === slug);
  const improveCids = useMemo(
    () => new Set(improveCidsBySlug[slug] || []),
    [improveCidsBySlug, slug],
  );
  function rememberImproveCid(activeSlug, cid) {
    if (!activeSlug || !cid) return;
    setImproveCidsBySlug((prev) => {
      const existing = prev[activeSlug] || [];
      if (existing.includes(cid)) return prev;
      return { ...prev, [activeSlug]: [...existing, cid] };
    });
  }

  const workspaceActivity = countWorkspaceActivity(
    stream?.events || [],
    slug,
    improveCids,
  );
  const workspaceSignals = [
    { label: "selected", value: slug || "none" },
    {
      label: "stack",
      value: current?.stack || (slug ? "unknown stack" : "pick project"),
    },
    {
      label: "status",
      value: current
        ? `${current.status || current.verdict || "—"} · score ${current.score ?? "—"}`
        : "idle",
    },
    {
      label: "activity",
      value: slug
        ? `${workspaceActivity} event${workspaceActivity === 1 ? "" : "s"}`
        : "none",
    },
  ];

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="Foundry · Live Workspace"
        title="Workspace"
        sub="Run a delivered app and refine it in place — serve on the left, improve on the right."
        actions={
          <select
            aria-label="Workspace project"
            value={slug}
            onChange={(e) => pick(e.target.value)}
            className="max-w-full rounded border border-hairline bg-ink/60 px-3 py-1.5 font-mono text-xs text-bone focus:border-ember focus:outline-none"
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

      <Panel className="mb-3 p-3">
        <SignalGrid label="Workspace signals" items={workspaceSignals} />
      </Panel>

      <div className="grid flex-1 grid-cols-1 gap-4 lg:grid-cols-2">
        <ServePane
          slug={slug}
          stream={stream}
          editorMode={rightMode === "visual" || rightMode === "annotate"}
          onElementSelected={
            rightMode === "annotate"
              ? (signature) => setAnnotationPins((prev) => addPin(prev, signature))
              : setSelectedSignature
          }
          refreshRevision={previewRevision}
        />
        <div className="flex min-h-0 flex-col gap-2">
          <div className="flex gap-2">
            <button
              className={rightMode === "improve" ? "btn-ember" : "btn-ghost"}
              onClick={() => setRightMode("improve")}
            >
              AI improve
            </button>
            <button
              className={rightMode === "visual" ? "btn-ember" : "btn-ghost"}
              onClick={() => setRightMode("visual")}
            >
              Visual edit
            </button>
            <button
              className={rightMode === "annotate" ? "btn-ember" : "btn-ghost"}
              onClick={() => setRightMode("annotate")}
            >
              Annotate
            </button>
            <button
              className={rightMode === "quality" ? "btn-ember" : "btn-ghost"}
              onClick={() => setRightMode("quality")}
            >
              Visual quality
            </button>            <button
              className={rightMode === "product" ? "btn-ember" : "btn-ghost"}
              onClick={() => setRightMode("product")}
            >
              Product
            </button>
          </div>
          <div className="min-h-0 flex-1">
            {rightMode === "quality" ? (
              <VisualQualityPane
                slug={slug}
                onCompleted={() => setPreviewRevision((value) => value + 1)}
              />
            ) : rightMode === "product" ? (
              <ProductPane
                slug={slug}
                buildId={current?.build_id || ""}
              />
            ) : rightMode === "visual" ? (
              <VisualEditorPane
                slug={slug}
                signature={selectedSignature}
                onEdited={() => setPreviewRevision((value) => value + 1)}
              />
            ) : rightMode === "annotate" ? (
              <AnnotatePane
                slug={slug}
                pins={annotationPins}
                onPinsChange={setAnnotationPins}
                onDispatched={rememberImproveCid}
                onSubmitted={() => setRightMode("improve")}
              />
            ) : (
              <ImprovePane
                slug={slug}
                stream={stream}
                cids={improveCids}
                onDispatched={rememberImproveCid}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
