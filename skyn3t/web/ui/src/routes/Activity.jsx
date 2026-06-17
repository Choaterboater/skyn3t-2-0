import React, { useState } from "react";

const TYPE_COLOR = (type) => {
  if (type.endsWith("FAILED")) return "text-rose-400";
  if (type.endsWith("COMPLETED")) return "text-emerald-400";
  if (type.startsWith("BUILD")) return "text-brand";
  if (type.includes("KNOWLEDGE") || type.includes("LESSON"))
    return "text-violet-400";
  return "text-slate-300";
};

export default function Activity({ stream }) {
  const [filter, setFilter] = useState("");
  const events = [...(stream?.events || [])].reverse();
  const filtered = filter
    ? events.filter(
        (e) =>
          e.type.toLowerCase().includes(filter.toLowerCase()) ||
          (e.source || "").toLowerCase().includes(filter.toLowerCase())
      )
    : events;

  return (
    <div className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Activity</h1>
          <p className="text-sm text-slate-500">Live event log from /ws.</p>
        </div>
        <input
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm outline-none focus:border-brand"
          placeholder="filter…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </header>

      <div className="card max-h-[70vh] overflow-y-auto p-0">
        {filtered.length === 0 ? (
          <p className="p-4 text-sm text-slate-500">No events.</p>
        ) : (
          <table className="w-full text-left font-mono text-xs">
            <tbody>
              {filtered.map((e) => (
                <tr
                  key={e.id || `${e.timestamp}-${e.type}`}
                  className="border-b border-slate-800/60"
                >
                  <td className="whitespace-nowrap px-3 py-1.5 text-slate-500">
                    {e.timestamp
                      ? new Date(
                          typeof e.timestamp === "number"
                            ? e.timestamp * 1000
                            : e.timestamp
                        ).toLocaleTimeString()
                      : ""}
                  </td>
                  <td className={`px-3 py-1.5 ${TYPE_COLOR(e.type)}`}>
                    {e.type}
                  </td>
                  <td className="px-3 py-1.5 text-slate-400">{e.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
