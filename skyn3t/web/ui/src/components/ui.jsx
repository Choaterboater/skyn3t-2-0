import React from "react";

// Shared "Foundry" primitives. Pages compose these so the visual language stays
// consistent. Heat = activity: ember when working, plasma when cool/idle.

export function PageHeader({ eyebrow, title, sub, actions }) {
  return (
    <header className="mb-7 flex items-end justify-between gap-4 animate-risefade">
      <div>
        {eyebrow ? <div className="eyebrow mb-2">{eyebrow}</div> : null}
        <h1 className="font-display text-2xl font-bold tracking-tight text-bone">{title}</h1>
        {sub ? <p className="mt-1 max-w-xl text-sm text-ash">{sub}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Panel({ children, className = "", glow = false }) {
  return (
    <section className={`panel ${glow ? "ring-heat" : ""} ${className}`}>{children}</section>
  );
}

export function PanelHead({ label, right }) {
  return (
    <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
      <span className="eyebrow">{label}</span>
      {right}
    </div>
  );
}

// A big telemetry number with a mono label. The unit of the dashboard.
export function Stat({ label, value, tone = "bone", hint }) {
  const toneCls =
    tone === "ember" ? "heat-hot" : tone === "plasma" ? "heat-cool" : "text-bone";
  return (
    <Panel className="relative overflow-hidden p-4">
      <div className="eyebrow">{label}</div>
      <div className={`metric mt-2 ${toneCls}`}>{value}</div>
      {hint ? <div className="mt-1 font-mono text-[11px] text-ash">{hint}</div> : null}
      <div className="pointer-events-none absolute -right-6 -top-6 h-16 w-16 rounded-full bg-ember/5 blur-2xl" />
    </Panel>
  );
}

export function Pill({ children, tone = "ash" }) {
  const map = {
    ember: "border-ember/40 text-ember bg-ember/10",
    plasma: "border-plasma/40 text-plasma bg-plasma/10",
    synapse: "border-synapse/40 text-synapse bg-synapse/10",
    ash: "border-hairline text-ash",
  };
  return <span className={`badge ${map[tone] || map.ash}`}>{children}</span>;
}

export function Empty({ icon = "◇", children }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
      <span className="text-2xl text-ash/50">{icon}</span>
      <p className="font-mono text-sm text-ash">{children}</p>
    </div>
  );
}

// Verdict / status -> tone.
export function verdictTone(v) {
  const s = String(v || "").toLowerCase();
  if (s === "go" || s === "completed" || s === "applied") return "plasma";
  if (s === "no_go" || s === "failed" || s === "rejected") return "ember";
  return "ash";
}
