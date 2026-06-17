import React from "react";
import { Routes, Route, NavLink, Navigate } from "react-router-dom";
import { useEventStream } from "./api.js";

import Overview from "./routes/Overview.jsx";
import Agents from "./routes/Agents.jsx";
import Studio from "./routes/Studio.jsx";
import Cortex from "./routes/Cortex.jsx";
import Brain from "./routes/Brain.jsx";
import Skills from "./routes/Skills.jsx";
import Activity from "./routes/Activity.jsx";
import Settings from "./routes/Settings.jsx";

const NAV = [
  { to: "/overview", label: "Overview" },
  { to: "/agents", label: "Swarm" },
  { to: "/studio", label: "Studio" },
  { to: "/cortex", label: "Cortex" },
  { to: "/brain", label: "Brain" },
  { to: "/skills", label: "Skills" },
  { to: "/activity", label: "Activity" },
  { to: "/settings", label: "Settings" },
];

const STATUS_COLOR = {
  open: "bg-emerald-500",
  connecting: "bg-amber-400 animate-pulseglow",
  closed: "bg-rose-500",
  error: "bg-rose-500",
};

export default function App() {
  // One shared event stream for the whole app; pass slices down via props.
  const stream = useEventStream();

  return (
    <div className="flex h-full min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-slate-800 bg-slate-900/50 p-4">
        <div className="mb-6 flex items-center gap-2">
          <span className="text-lg font-bold tracking-tight text-brand">
            SkyN3t
          </span>
          <span className="badge bg-slate-800 text-slate-400">2.0</span>
        </div>
        <nav className="flex flex-1 flex-col gap-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `nav-link ${isActive ? "nav-link-active" : ""}`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-4 flex items-center gap-2 text-xs text-slate-500">
          <span
            className={`h-2.5 w-2.5 rounded-full ${
              STATUS_COLOR[stream.status] || "bg-slate-600"
            }`}
          />
          <span>ws: {stream.status}</span>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto p-6">
        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={<Overview stream={stream} />} />
          <Route path="/agents" element={<Agents stream={stream} />} />
          <Route path="/studio" element={<Studio stream={stream} />} />
          <Route path="/cortex" element={<Cortex stream={stream} />} />
          <Route path="/brain" element={<Brain stream={stream} />} />
          <Route path="/skills" element={<Skills />} />
          <Route path="/activity" element={<Activity stream={stream} />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/overview" replace />} />
        </Routes>
      </main>
    </div>
  );
}
