import React, { Suspense, lazy } from "react";
import { Routes, Route, NavLink, Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { queryFn, useEventStream } from "./api.js";

// Lazy-load each route so its code (and heavy deps) ships in its own chunk and
// loads on demand. In particular the Brain page pulls in three.js / r3f (~800KB)
// — lazy-loading keeps it out of the initial bundle entirely.
const Overview = lazy(() => import("./routes/Overview.jsx"));
const Agents = lazy(() => import("./routes/Agents.jsx"));
const Studio = lazy(() => import("./routes/Studio.jsx"));
const Cortex = lazy(() => import("./routes/Cortex.jsx"));
const Brain = lazy(() => import("./routes/Brain.jsx"));
const Skills = lazy(() => import("./routes/Skills.jsx"));
const Activity = lazy(() => import("./routes/Activity.jsx"));
const Settings = lazy(() => import("./routes/Settings.jsx"));
const Projects = lazy(() => import("./routes/Projects.jsx"));
const Workspace = lazy(() => import("./routes/Workspace.jsx"));

const NAV = [
  { to: "/overview", label: "Overview", glyph: "◇" },
  { to: "/agents", label: "Swarm", glyph: "⬡" },
  { to: "/studio", label: "Foundry", glyph: "▰" },
  { to: "/cortex", label: "Cortex", glyph: "⊚" },
  { to: "/brain", label: "Brain", glyph: "✺" },
  { to: "/skills", label: "Skills", glyph: "✦" },
  { to: "/activity", label: "Activity", glyph: "≋" },
  { to: "/settings", label: "Settings", glyph: "⚙" },
  { to: "/projects", label: "Projects", glyph: "▤" },
  { to: "/workspace", label: "Workspace", glyph: "⧉" },
];

// A long-running server keeps serving the code it imported at boot; when the
// working tree moves on, features look broken while the fix sits unloaded on
// disk. /api/health reports `stale_code` — surface it everywhere, loudly.
function StaleCodeBanner() {
  const health = useQuery({
    queryKey: ["health-stale"],
    queryFn: queryFn("/health"),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
  if (!health.data?.stale_code) return null;
  const started = health.data.started_at
    ? new Date(health.data.started_at * 1000).toLocaleString()
    : "unknown";
  return (
    <div className="mb-6 rounded-md border border-ember/50 bg-ember/10 px-4 py-3 font-mono text-[12px] leading-relaxed text-ember">
      ⚠ This server is running <span className="font-bold">stale code</span> — source
      files changed after it started ({started}). Restart it (
      <span className="font-bold">skyn3t start --web</span>) to load the latest fixes;
      until then builds and improves run the old code.
    </div>
  );
}

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
      {/* Collapses to an icon rail below lg so data-dense pages (Projects) get
          back ~176px instead of being cramped by a fixed 240px sidebar. The
          nav glyphs carry the rail; labels return when there's room. */}
      <aside className="flex w-16 shrink-0 flex-col border-r border-hairline bg-panel/40 px-2 py-5 lg:w-60 lg:px-4">
        {/* wordmark — full on wide, monogram on the rail */}
        <div className="mb-7 lg:px-2">
          <div className="hidden items-baseline gap-2 lg:flex">
            <span className="font-display text-xl font-bold tracking-tight text-bone">
              SKY<span className="text-ember">N3T</span>
            </span>
            <span className="badge border-hairline text-ash">v2.0</span>
          </div>
          <div className="text-center font-display text-lg font-bold tracking-tight text-bone lg:hidden">
            S<span className="text-ember">N</span>
          </div>
          <div className="eyebrow mt-1 hidden lg:block">Autonomous Foundry</div>
        </div>

        <nav className="flex flex-1 flex-col gap-0.5">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              title={item.label}
              className={({ isActive }) =>
                `nav-link relative justify-center lg:justify-start ${
                  isActive ? "nav-link-active" : ""
                }`
              }
            >
              <span className="w-4 text-center opacity-70">{item.glyph}</span>
              <span className="hidden lg:inline">{item.label}</span>
            </NavLink>
          ))}
        </nav>

        {/* forge status — dot always; details when there's room */}
        <div className="mt-4 flex items-center justify-center gap-2 border-t border-hairline pt-3 lg:justify-between lg:px-2">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${ws.dot}`} title={`ws · ${ws.label}`} />
            <span className={`hidden font-mono text-[11px] lg:inline ${ws.cls}`}>
              ws · {ws.label}
            </span>
          </div>
          <span className="hidden font-mono text-[11px] text-ash lg:inline">
            {stream.events?.length || 0} evt
          </span>
        </div>
      </aside>

      {/* min-w-0: without it a flex child keeps min-width:auto and refuses to
          shrink below its content, so a wide table (e.g. Projects) pushes the
          whole page sideways instead of scrolling inside its own container. */}
      <main className="min-w-0 flex-1 overflow-y-auto">
        {/* Wide enough for data-dense tables (Projects' 10 cols) to use the
            screen instead of stranding a big empty gutter and clipping the last
            column; still capped so prose pages don't sprawl on ultrawide. */}
        <div className="mx-auto max-w-screen-2xl px-8 py-8">
          <StaleCodeBanner />
          <Suspense
            fallback={
              <div className="flex items-center gap-2 px-1 py-8 font-mono text-[11px] text-ash">
                <span className="h-2 w-2 animate-forgepulse rounded-full bg-ember" />
                loading…
              </div>
            }
          >
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
              <Route path="/projects" element={<Projects stream={stream} />} />
              <Route path="/workspace" element={<Workspace stream={stream} />} />
              <Route path="*" element={<Navigate to="/overview" replace />} />
            </Routes>
          </Suspense>
        </div>
      </main>
    </div>
  );
}
