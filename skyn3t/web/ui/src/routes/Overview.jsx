import React, { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Empty } from "../components/ui.jsx";

// Derive which agents are "hot" (mid-task) from the live event stream.
function useHeat(events) {
  return useMemo(() => {
    const busy = new Set();
    const lastSeen = {};
    for (const e of events || []) {
      const t = (e.type || "").toLowerCase();
      const src = e.source || "";
      lastSeen[src] = e.timestamp || 0;
      if (t.includes("task.started") || t.includes("stage.started")) busy.add(src);
      else if (t.includes("completed") || t.includes("failed")) busy.delete(src);
    }
    return { busy, lastSeen };
  }, [events]);
}

// The signature: the swarm rendered as a heat constellation. Each agent is a
// node that flares ember while it forges, and cools to plasma when idle.
function SwarmConstellation({ agents, heat }) {
  if (!agents.length) return <Empty icon="⬡">No agents registered yet.</Empty>;
  return (
    <div className="flex flex-wrap gap-2 p-4">
      {agents.map((a) => {
        const name = a.name || a.agent_type || String(a);
        const hot = heat.busy.has(name);
        return (
          <div
            key={name}
            title={`${name} · ${hot ? "forging" : "idle"}`}
            className={`group relative flex items-center gap-2 rounded-md border px-3 py-2 transition-all duration-300 ${
              hot
                ? "border-ember/60 bg-ember/10 ring-heat animate-emberflare"
                : "border-hairline bg-void/60"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                hot ? "bg-ember animate-forgepulse" : "bg-plasma/50"
              }`}
            />
            <span className={`font-mono text-[11px] ${hot ? "text-ember" : "text-ash"}`}>
              {name}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default function Overview({ stream }) {
  const events = stream?.events || [];
  const health = useQuery({ queryKey: ["health"], queryFn: queryFn("/health") });
  const agentsQ = useQuery({ queryKey: ["agents"], queryFn: queryFn("/agents") });
  const heat = useHeat(events);

  const agents = Array.isArray(agentsQ.data) ? agentsQ.data : agentsQ.data?.agents || [];
  const d = health.data || {};
  const recent = [...events].slice(-9).reverse();
  const forging = heat.busy.size;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Mission Control"
        title="Overview"
        sub="A swarm of agents forging briefs into running software. Heat is work."
        actions={
          <span className="badge border-hairline text-ash">
            backend · <span className="ml-1 text-ember">{d.backend || d.llm_backend || "stub"}</span>
          </span>
        }
      />

      {health.error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Control plane unreachable: {String(health.error.message)}
        </Panel>
      ) : null}

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Agents" value={d.agents ?? d.agent_count ?? agents.length} />
        <Stat label="Forging now" value={forging} tone={forging ? "ember" : "plasma"} hint={forging ? "agents hot" : "all idle"} />
        <Stat label="Active builds" value={d.active_builds ?? 0} tone={d.active_builds ? "ember" : "bone"} />
        <Stat label="Events" value={events.length} hint="this session" />
      </div>

      <Panel className="mb-6 overflow-hidden">
        <PanelHead
          label="The Swarm"
          right={<span className="font-mono text-[11px] text-ash">{forging}/{agents.length} forging</span>}
        />
        <SwarmConstellation agents={agents} heat={heat} />
      </Panel>

      <Panel>
        <PanelHead label="Live event tail" right={<span className="font-mono text-[11px] text-ash">/ws</span>} />
        {recent.length === 0 ? (
          <Empty icon="≋">Quiet forge. Submit a build to see the swarm light up.</Empty>
        ) : (
          <ul className="divide-y divide-hairline/60">
            {recent.map((e) => {
              const t = (e.type || "").toLowerCase();
              const tone = t.includes("failed")
                ? "text-ember"
                : t.includes("completed")
                ? "text-plasma"
                : t.includes("build")
                ? "text-ember-soft"
                : "text-ash";
              return (
                <li key={e.id || e.timestamp} className="flex items-center gap-3 px-4 py-1.5 font-mono text-xs">
                  <span className={tone}>{e.type}</span>
                  <span className="text-ash/70">{e.source}</span>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>
    </div>
  );
}
