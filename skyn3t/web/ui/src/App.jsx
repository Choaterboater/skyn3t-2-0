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
  { to: "/overview", label: "Overview", glyph: "◇" },
  { to: "/agents", label: "Swarm", glyph: "⬡" },
  { to: "/studio", label: "Foundry", glyph: "▰" },
  { to: "/cortex", label: "Cortex", glyph: "⊚" },
  { to: "/brain", label: "Brain", glyph: "✺" },
  { to: "/skills", label: "Skills", glyph: "✦" },
  { to: "/activity", label: "Activity", glyph: "≋" },
  { to: "/settings", label: "Settings", glyph: "⚙" },
];

const WS = {
  open: { dot: "bg-plasma", label: "live", cls: "text-plasma" },
  connecting: { dot: "bg-ember animate-forgepulse", label: "linking", cls: "text-ember" },
  closed: { dot: "bg-ash", label: "offline", cls: "text-ash" },
  error: { dot: "bg-ember", label: "error", cls: "text-ember" },
};

export default function App() {
  // One shared event stream for the whole app; pass slices down via props.
  const stream = useEventStream();
  const ws = WS[stream.status] || WS.closed;

  return (
    <div className="flex h-full min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-hairline bg-panel/40 px-4 py-5">
        {/* wordmark */}
        <div className="mb-7 px-2">
          <div className="flex items-baseline gap-2">
            <span className="font-display text-xl font-bold tracking-tight text-bone">
              SKY<span className="text-ember">N3T</span>
            </span>
            <span className="badge border-hairline text-ash">v2.0</span>
          </div>
          <div className="eyebrow mt-1">Autonomous Foundry</div>
        </div>

        <nav className="flex flex-1 flex-col gap-0.5">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `nav-link relative ${isActive ? "nav-link-active" : ""}`
              }
            >
              <span className="w-4 text-center opacity-70">{item.glyph}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>

        {/* forge status */}
        <div className="mt-4 flex items-center justify-between border-t border-hairline px-2 pt-3">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${ws.dot}`} />
            <span className={`font-mono text-[11px] ${ws.cls}`}>ws · {ws.label}</span>
          </div>
          <span className="font-mono text-[11px] text-ash">
            {stream.events?.length || 0} evt
          </span>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl px-8 py-8">
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
        </div>
      </main>
    </div>
  );
}
