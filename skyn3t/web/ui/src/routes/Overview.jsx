import React from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { agentActivity, agentIsBusy } from "../agentSignals.js";
import { PageHeader, Panel, PanelHead, Empty } from "../components/ui.jsx";
import GateLadder from "../components/GateLadder.jsx";
import StreamStaleBanner from "../components/StreamStaleBanner.jsx";

// A quiet telemetry reading — demoted from the old 4-up hero grid so the boldness
// lives in the Verify Ladder above. Label + mono number, inline.
function Telem({ label, value, tone = "bone" }) {
  const cls = tone === "ember" ? "heat-hot" : tone === "plasma" ? "heat-cool" : "text-bone";
  return (
    <div className="flex items-baseline gap-2">
      <span className="eyebrow">{label}</span>
      <span className={`font-mono text-lg font-semibold tabular-nums ${cls}`}>{value}</span>
    </div>
  );
}

// The signature: the swarm rendered as a heat constellation. Each agent is a
// node that flares ember while it forges, and cools to plasma when idle.
function SwarmConstellation({ agents, heat }) {
  if (!agents.length) return <Empty icon="⬡">No agents registered yet.</Empty>;
  return (
    <div className="flex flex-wrap gap-2 p-4">
      {agents.map((a) => {
        const name = a.name || a.agent_type || String(a);
        const hot = agentIsBusy(a, heat);
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
  const heat = agentActivity(events);

  const agents = Array.isArray(agentsQ.data) ? agentsQ.data : agentsQ.data?.agents || [];
  const d = health.data || {};
  const recent = [...events].slice(-9).reverse();
  const forging = agents.filter((agent) => agentIsBusy(agent, heat)).length;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Mission Control"
        title="Overview"
        sub="An autonomous factory that builds any kind of app from a brief — and proves each one before it ships."
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

      {/* a dead stream freezes the ladder's heat — say so instead of pulsing */}
      <StreamStaleBanner stream={stream} />

      {/* the signature: every build climbs the verify ladder before it ships */}
      <GateLadder stream={stream} />

      {/* demoted telemetry — quiet strip, not the hero */}
      <Panel className="mb-6">
        <div className="flex flex-wrap items-center gap-x-10 gap-y-3 px-4 py-3">
          <Telem label="Agents" value={d.agents ?? d.agent_count ?? agents.length} />
          <Telem label="Forging" value={forging} tone={forging ? "ember" : "plasma"} />
          <Telem label="Active builds" value={d.active_builds ?? 0} tone={d.active_builds ? "ember" : "bone"} />
          <Telem label="Events" value={events.length} />
        </div>
      </Panel>

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
