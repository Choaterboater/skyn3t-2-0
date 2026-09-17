import React from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { agentActivity, agentIsBusy } from "../agentSignals.js";
import { PageHeader, Panel, PanelHead, Empty } from "../components/ui.jsx";
import GateLadder from "../components/GateLadder.jsx";
import GoldenBenchCard from "../components/GoldenBenchCard.jsx";
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
  const projectsQ = useQuery({ queryKey: ["projects"], queryFn: queryFn("/projects") });
  const heat = agentActivity(events);

  const agents = Array.isArray(agentsQ.data) ? agentsQ.data : agentsQ.data?.agents || [];
  const d = health.data || {};
  const recent = [...events].slice(-9).reverse();
  const forging = agents.filter((agent) => agentIsBusy(agent, heat)).length;
  const projects = Array.isArray(projectsQ.data) ? projectsQ.data : projectsQ.data?.projects || [];
  const recentProjects = [...projects].sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0)).slice(0, 4);

  return (
    <div>
      <PageHeader
        eyebrow="Product workspace"
        title="Good to see you. What will you make?"
        sub="Start a new build, continue a project, or refine a running app. SkyN3t keeps the technical proof available without putting it in your way."
        actions={
          <span className="badge border-hairline text-ash">
            {health.isLoading ? "Dashboard connection: checking" : health.error ? "Dashboard disconnected" : "Dashboard connected"} · {d.backend === "stub" || d.llm_backend === "stub" ? "Offline demo generation" : `Generation engine: ${d.backend || d.llm_backend || "not checked"}`}
          </span>
        }
      />

      <div className="mb-6 grid gap-4 lg:grid-cols-[minmax(0,1.5fr)_minmax(280px,.8fr)]">
        <Panel className="p-5 sm:p-6">
          <p className="text-sm font-semibold text-bone">Start with a brief</p>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ash">Describe the outcome you need. The build workspace will keep model, budget, attachment, approval, and verification controls within reach.</p>
          <div className="mt-5 flex flex-wrap gap-2">
            <Link to="/studio" className="btn-ember">New build <span aria-hidden="true">→</span></Link>
            <Link to="/projects" className="btn-ghost">Browse projects</Link>
            <Link to="/workspace" className="btn-ghost">Open workspace</Link>
          </div>
        </Panel>
        <Panel className="overflow-hidden">
          <PanelHead label="Recent work" right={<Link to="/projects" className="text-xs font-medium text-ember">View all</Link>} />
          {projectsQ.isLoading ? <div role="status" className="p-4 text-sm text-ash">Loading recent projects…</div> : projectsQ.error ? <div role="alert" className="p-4 text-sm text-ember">Recent projects unavailable.</div> : recentProjects.length ? <ul className="divide-y divide-hairline">{recentProjects.map((project) => <li key={project.slug}><Link to={`/workspace?slug=${encodeURIComponent(project.slug)}`} className="flex items-center justify-between gap-3 px-4 py-3 hover:bg-panel-2/60"><span className="min-w-0"><strong className="block truncate text-sm text-bone">{project.name || project.slug}</strong><span className="mt-0.5 block text-xs text-ash">{project.stack || "Stack unavailable"}</span></span><span className="text-xs font-medium text-ember">Continue</span></Link></li>)}</ul> : <Empty icon="◇">No project work is available yet. Start a build or import a project.</Empty>}
        </Panel>
      </div>

      {health.error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Control plane unreachable: {String(health.error.message)}
        </Panel>
      ) : null}

      {/* a dead stream freezes the ladder's heat — say so instead of pulsing */}
      <StreamStaleBanner stream={stream} />

      <div className="mb-3 mt-8"><h2 className="font-display text-xl font-semibold text-bone">Connection and verification</h2><p className="mt-1 text-sm text-ash">Dashboard connectivity does not prove generation availability or product behavior. Offline demo output is not live AI output; proof applies only to the build that was checked.</p></div>

      <details className="mb-6">
        <summary className="cursor-pointer py-3 text-sm font-semibold text-bone">Technical verification stages</summary>
      {/* the signature: every build climbs the verify ladder before it ships */}
      <GateLadder stream={stream} />
      </details>

      {/* golden bench runs are isolated from build memory — surface them here */}
      <GoldenBenchCard />

      {/* demoted telemetry — quiet strip, not the hero */}
      <Panel className="mb-6">
        <div className="flex flex-wrap items-center gap-x-10 gap-y-3 px-4 py-3">
          <Telem label="Forging" value={forging} tone={forging ? "ember" : "plasma"} />
          <Telem label="Agents" value={d.agents ?? d.agent_count ?? agents.length} />
          <Telem label="Active builds" value={d.active_builds ?? 0} tone={d.active_builds ? "ember" : "bone"} />
          <Telem label="Events" value={events.length} />
        </div>
      </Panel>

      <Panel className="mb-6 overflow-hidden">
        <PanelHead
          label="Agents"
          right={<span className="font-mono text-[11px] text-ash">{forging}/{agents.length} working</span>}
        />
        <SwarmConstellation agents={agents} heat={heat} />
      </Panel>

      <Panel>
        <PanelHead label="Recent activity" right={<span className="font-mono text-[11px] text-ash">/ws</span>} />
        {recent.length === 0 ? (
          <Empty icon="≋">No activity yet. Start a build to see progress.</Empty>
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
