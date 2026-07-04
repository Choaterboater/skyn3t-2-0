import React from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Empty, Pill } from "../components/ui.jsx";

// Status -> heat. Busy/running flares ember; ready/healthy cools to plasma;
// everything else stays ash-quiet.
function statusHeat(status) {
  const s = String(status || "idle").toLowerCase();
  if (s === "busy" || s === "running") return "ember";
  if (s === "ready" || s === "healthy") return "plasma";
  if (s === "failed") return "ember";
  return "ash";
}

export default function Agents({ stream }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["agents"],
    queryFn: queryFn("/agents"),
  });

  // Track recent per-agent activity from the live stream.
  const lastBySource = {};
  (stream?.events || []).forEach((e) => {
    if (e.source) lastBySource[e.source] = e.type;
  });

  const agents = Array.isArray(data) ? data : data?.agents || [];

  const forging = agents.filter((a) => statusHeat(a.status) === "ember").length;
  const ready = agents.filter((a) => statusHeat(a.status) === "plasma").length;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Swarm"
        title="Agents"
        sub="Registered worker roles and their live status. This roster is capability coverage, not the number of parallel agents assigned to one build."
        actions={
          <span className="badge border-hairline text-ash">
            roster · <span className="ml-1 text-plasma">{agents.length}</span>
          </span>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          {String(error.message)}
        </Panel>
      ) : null}

      <div className="mb-6 grid grid-cols-3 gap-4">
        <Stat label="Roster size" value={agents.length} hint="available roles" />
        <Stat
          label="Forging"
          value={forging}
          tone={forging ? "ember" : "plasma"}
          hint={forging ? "agents hot" : "all idle"}
        />
        <Stat label="Ready" value={ready} tone="plasma" />
      </div>

      <Panel className="overflow-hidden">
        <PanelHead
          label="The Roster"
          right={
            <span className="font-mono text-[11px] text-ash">
              {forging}/{agents.length} active now
            </span>
          }
        />
        <div className="border-b border-hairline px-4 py-2 font-mono text-[11px] text-ash">
          The roster count is the set of registered specialist roles SkyN3t can call.
          A build only uses the stages, skills, and workers its plan needs.
        </div>

        {isLoading ? (
          <Empty icon="⬡">Loading the swarm…</Empty>
        ) : agents.length === 0 ? (
          <Empty icon="⬡">No agents registered yet.</Empty>
        ) : (
          <div className="grid grid-cols-1 gap-px bg-hairline/40 sm:grid-cols-2 lg:grid-cols-3">
            {agents.map((a) => {
              const status = a.status || "idle";
              const heat = statusHeat(status);
              const hot = heat === "ember";
              const last = lastBySource[a.name];
              return (
                <div
                  key={a.name}
                  className={`relative flex flex-col gap-3 bg-panel p-4 transition-all duration-300 ${
                    hot ? "ring-heat" : ""
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span
                        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                          hot
                            ? "bg-ember animate-forgepulse"
                            : heat === "plasma"
                            ? "bg-plasma"
                            : "bg-ash/40"
                        }`}
                      />
                      <span className="font-display text-sm font-semibold text-bone">
                        {a.name}
                      </span>
                    </div>
                    <Pill tone={heat}>{status}</Pill>
                  </div>

                  <div className="flex items-center gap-2 font-mono text-[11px] text-ash">
                    <span className="text-bone/80">{a.agent_type || a.type}</span>
                    <span className="text-ash/40">·</span>
                    <span>{a.provider || "local"}</span>
                  </div>

                  {last ? (
                    <div className="mt-auto flex items-center gap-2 border-t border-hairline/60 pt-2 font-mono text-[11px]">
                      <span className="eyebrow text-ash/60">last</span>
                      <span className={hot ? "text-ember" : "text-plasma"}>{last}</span>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
