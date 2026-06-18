import React, { useState } from "react";
import { PageHeader, Panel, Empty } from "../components/ui.jsx";

// Heat palette: failed=ember, completed=plasma, build=ember-soft, else ash.
// Undefined-safe — `type` may be missing on raw stream events.
const TYPE_COLOR = (type) => {
  const t = (type || "").toUpperCase();
  if (t.includes("FAIL")) return "text-ember";
  if (t.includes("COMPLETED")) return "text-plasma";
  if (t.startsWith("BUILD")) return "text-ember-soft";
  return "text-ash";
};

export default function Activity({ stream }) {
  const [filter, setFilter] = useState("");
  const events = [...(stream?.events || [])].reverse();
  const filtered = filter
    ? events.filter(
        (e) =>
          (e.type || "").toLowerCase().includes(filter.toLowerCase()) ||
          (e.source || "").toLowerCase().includes(filter.toLowerCase())
      )
    : events;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Telemetry"
        title="Activity"
        sub="Live event log streamed from the forge. Heat reads the work."
        actions={
          <input
            className="field w-56"
            placeholder="filter type or source…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        }
      />

      <Panel className="overflow-hidden">
        <div className="max-h-[70vh] overflow-y-auto">
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
                </tr>
              </thead>
              <tbody>
                {filtered.map((e) => (
                  <tr
                    key={e.id || `${e.timestamp}-${e.type}`}
                    className="border-b border-hairline/60 transition-colors hover:bg-void/40"
                  >
                    <td className="whitespace-nowrap px-4 py-1.5 text-ash/70">
                      {e.timestamp
                        ? new Date(
                            typeof e.timestamp === "number"
                              ? e.timestamp * 1000
                              : e.timestamp
                          ).toLocaleTimeString()
                        : ""}
                    </td>
                    <td className={`px-4 py-1.5 ${TYPE_COLOR(e.type)}`}>
                      {e.type}
                    </td>
                    <td className="px-4 py-1.5 text-ash">{e.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Panel>
    </div>
  );
}
