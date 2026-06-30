import React, { useState } from "react";
import { apiFetch } from "../api.js";
import { PageHeader, Panel, PanelHead, Empty, Pill } from "../components/ui.jsx";

// Heat palette: failed=ember, completed=plasma, build=ember-soft, else ash.
// Undefined-safe — `type` may be missing on raw stream events.
const TYPE_COLOR = (type) => {
  const t = (type || "").toUpperCase();
  if (t.includes("FAIL")) return "text-ember";
  if (t.includes("COMPLETED")) return "text-plasma";
  if (t.startsWith("BUILD")) return "text-ember-soft";
  return "text-ash";
};

function toEpochSeconds(value) {
  if (value == null || value === "") return "";
  if (typeof value === "number") return value > 1e12 ? value / 1000 : value;
  const numeric = Number(value);
  if (!Number.isNaN(numeric) && String(value).trim() !== "") {
    return numeric > 1e12 ? numeric / 1000 : numeric;
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? "" : parsed / 1000;
}

function fmtTime(value) {
  const seconds = toEpochSeconds(value);
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleTimeString();
}

function eventKey(e, i) {
  return e.id || `${e.timestamp}-${e.type}-${i}`;
}

export default function Activity({ stream }) {
  const [filter, setFilter] = useState("");
  const [mode, setMode] = useState("live");
  const [replayLimit, setReplayLimit] = useState("200");
  const [replayType, setReplayType] = useState("");
  const [replayCorrelation, setReplayCorrelation] = useState("");
  const [replayUntil, setReplayUntil] = useState("");
  const [replayEvents, setReplayEvents] = useState([]);
  const [replayCount, setReplayCount] = useState(null);
  const [replayLoading, setReplayLoading] = useState(false);
  const [replayError, setReplayError] = useState("");
  const [selected, setSelected] = useState(null);

  const liveEvents = [...(stream?.events || [])].reverse();
  const replayView = [...replayEvents].reverse();
  const events = mode === "replay" ? replayView : liveEvents;
  const filtered = filter
    ? events.filter(
        (e) =>
          (e.type || "").toLowerCase().includes(filter.toLowerCase()) ||
          (e.source || "").toLowerCase().includes(filter.toLowerCase()) ||
          (e.correlation_id || "").toLowerCase().includes(filter.toLowerCase())
      )
    : events;

  async function loadReplay(overrides = {}) {
    setReplayLoading(true);
    setReplayError("");
    const params = new URLSearchParams();
    const limit = Math.min(2000, Math.max(1, Number(replayLimit) || 200));
    params.set("limit", String(limit));
    if (replayType.trim()) params.set("type", replayType.trim());
    if (replayCorrelation.trim()) {
      params.set("correlation_id", replayCorrelation.trim());
    }
    const until = overrides.until ?? replayUntil;
    if (until) params.set("until", String(until));
    try {
      const data = await apiFetch(`/trajectory?${params.toString()}`);
      const next = Array.isArray(data.events) ? data.events : [];
      setReplayEvents(next);
      setReplayCount(data.count ?? next.length);
      setMode("replay");
      setSelected(next.at(-1) || null);
    } catch (err) {
      setReplayError(err.message || "Failed to load trajectory.");
    } finally {
      setReplayLoading(false);
    }
  }

  function freezeAtLatest() {
    const live = stream?.events || [];
    const latest = live[live.length - 1];
    const until = toEpochSeconds(latest?.timestamp);
    if (!until) {
      setReplayError("No live event timestamp available to freeze.");
      return;
    }
    setReplayUntil(String(until));
    loadReplay({ until });
  }

  function resetReplay() {
    setMode("live");
    setReplayUntil("");
    setSelected(null);
    setReplayError("");
  }

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Telemetry"
        title="Activity"
        sub="Live event stream plus replayable trajectory slices for time-travel debugging."
        actions={
          <>
            <Pill tone={mode === "replay" ? "synapse" : "plasma"}>
              {mode === "replay" ? "replay" : "live"}
            </Pill>
            <input
              className="field w-64"
              placeholder="filter type, source, correlation…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </>
        }
      />

      <Panel className="mb-4 overflow-hidden">
        <PanelHead
          label="Trajectory replay / time travel"
          right={
            <span className="font-mono text-[11px] text-ash">
              {mode === "replay"
                ? `${replayCount ?? replayEvents.length} replay event(s)`
                : `${stream?.events?.length || 0} live event(s)`}
            </span>
          }
        />
        <div className="grid gap-3 p-4 lg:grid-cols-[110px_1fr_1fr_1fr_auto_auto_auto]">
          <label className="block">
            <span className="eyebrow mb-1 block">Limit</span>
            <input
              className="field"
              min="1"
              max="2000"
              type="number"
              value={replayLimit}
              onChange={(e) => setReplayLimit(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="eyebrow mb-1 block">Type</span>
            <input
              className="field"
              placeholder="build.completed"
              value={replayType}
              onChange={(e) => setReplayType(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="eyebrow mb-1 block">Correlation</span>
            <input
              className="field"
              placeholder="build id / correlation id"
              value={replayCorrelation}
              onChange={(e) => setReplayCorrelation(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="eyebrow mb-1 block">Until epoch</span>
            <input
              className="field"
              placeholder="seek cutoff"
              value={replayUntil}
              onChange={(e) => setReplayUntil(e.target.value)}
            />
          </label>
          <button
            className="btn-ember self-end"
            disabled={replayLoading}
            onClick={() => loadReplay()}
            type="button"
          >
            {replayLoading ? "Loading..." : "Load replay"}
          </button>
          <button className="btn-ghost self-end" onClick={freezeAtLatest} type="button">
            Freeze latest
          </button>
          <button className="btn-ghost self-end" onClick={resetReplay} type="button">
            Live
          </button>
        </div>
        {replayError ? (
          <div className="border-t border-hairline px-4 py-2 font-mono text-xs text-ember">
            {replayError}
          </div>
        ) : null}
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
        <Panel className="overflow-hidden">
        <div className="max-h-[70vh] overflow-auto">
          {filtered.length === 0 ? (
            <Empty icon="≋">
              {filter ? "No events match this filter." : "Quiet forge. No events yet."}
            </Empty>
          ) : (
            <table className="w-full text-left font-mono text-xs">
              <thead className="sticky top-0 z-10 bg-panel">
                <tr className="border-b border-hairline">
                  <th className="eyebrow px-4 py-3 font-normal">Time</th>
                  <th className="eyebrow px-4 py-3 font-normal">Type</th>
                  <th className="eyebrow px-4 py-3 font-normal">Source</th>
                  <th className="eyebrow px-4 py-3 font-normal">Correlation</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((e, i) => (
                  <tr
                    key={eventKey(e, i)}
                    onClick={() => setSelected(e)}
                    className="cursor-pointer border-b border-hairline/60 transition-colors hover:bg-void/40"
                  >
                    <td className="whitespace-nowrap px-4 py-1.5 text-ash/70">
                      {fmtTime(e.timestamp)}
                    </td>
                    <td className={`px-4 py-1.5 ${TYPE_COLOR(e.type)}`}>
                      {e.type}
                    </td>
                    <td className="px-4 py-1.5 text-ash">{e.source}</td>
                    <td className="max-w-[180px] truncate px-4 py-1.5 text-ash/70">
                      {e.correlation_id || ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Panel>
        <Panel className="overflow-hidden">
          <PanelHead label="Selected event" right={selected?.id ? <Pill>{selected.id}</Pill> : null} />
          {selected ? (
            <pre className="max-h-[70vh] overflow-auto p-4 font-mono text-xs leading-relaxed text-ash">
              {JSON.stringify(selected, null, 2)}
            </pre>
          ) : (
            <Empty icon="⌁">Select an event to inspect its full payload.</Empty>
          )}
        </Panel>
      </div>
    </div>
  );
}
