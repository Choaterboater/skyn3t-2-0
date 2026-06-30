import React from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill, Empty } from "../components/ui.jsx";

export default function Skills() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: queryFn("/skills"),
  });

  const skills = Array.isArray(data) ? data : data?.skills || [];
  const patterns = Array.isArray(data?.patterns) ? data.patterns : [];

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Capability Library"
        title="Skills"
        sub="Reusable capabilities the swarm has learned and registered in the hub."
        actions={
          <span className="badge border-hairline text-ash">
            learned · <span className="ml-1 text-plasma">{skills.length}</span>
          </span>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Skills hub unreachable: {String(error.message)}
        </Panel>
      ) : null}

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-3">
        <Stat label="Registered" value={skills.length} tone="plasma" hint="capabilities" />
        <Stat
          label="Tagged"
          value={skills.filter((s) => Array.isArray(s.tags) && s.tags.length).length}
          hint="carry tags"
        />
        <Stat
          label="Scored"
          value={skills.filter((s) => s.score != null).length}
          hint="have a score"
        />
        <Stat
          label="Patterns"
          value={patterns.length}
          tone={patterns.length ? "ember" : "bone"}
          hint="build shapes"
        />
      </div>

      <Panel>
        <PanelHead
          label="Skill index"
          right={<span className="font-mono text-[11px] text-ash">/skills</span>}
        />
        {isLoading ? (
          <Empty icon="≋">Loading the library…</Empty>
        ) : skills.length === 0 ? (
          <Empty icon="◇">No skills registered. The swarm has nothing learned yet.</Empty>
        ) : (
          <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
            {skills.map((s) => (
              <div
                key={s.name || s.id}
                className="panel flex flex-col gap-3 border-hairline bg-void/40 p-4 transition-all duration-300 hover:border-plasma/40"
              >
                <div className="flex items-start justify-between gap-2">
                  <h3 className="font-display text-base font-bold tracking-tight text-bone">
                    {s.name}
                  </h3>
                  {s.score != null ? (
                    <span className="font-mono text-sm text-plasma">{s.score}</span>
                  ) : null}
                </div>

                {s.description ? (
                  <p className="text-sm text-ash">{s.description}</p>
                ) : null}

                {Array.isArray(s.tags) && s.tags.length ? (
                  <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
                    {s.tags.map((t) => (
                      <Pill key={t} tone="plasma">
                        {t}
                      </Pill>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel className="mt-6">
        <PanelHead
          label="Build-pattern reuse"
          right={<span className="font-mono text-[11px] text-ash">/skills · patterns</span>}
        />
        {patterns.length === 0 ? (
          <Empty icon="◇">
            No build patterns recorded yet. Successful builds fill this scoreboard and promote repeat winners into skills.
          </Empty>
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-left font-mono text-xs">
              <thead>
                <tr className="border-b border-hairline">
                  <th className="eyebrow px-4 py-3 font-normal">Stack</th>
                  <th className="eyebrow px-4 py-3 font-normal">Uses</th>
                  <th className="eyebrow px-4 py-3 font-normal">Win rate</th>
                  <th className="eyebrow px-4 py-3 font-normal">Mean score</th>
                  <th className="eyebrow px-4 py-3 font-normal">Shape</th>
                </tr>
              </thead>
              <tbody>
                {patterns.slice(0, 20).map((p, i) => (
                  <tr key={p.fp || `${p.stack || "pattern"}-${i}`} className="border-b border-hairline/60">
                    <td className="px-4 py-2 text-bone">{p.stack || "generic"}</td>
                    <td className="px-4 py-2 text-ash">{p.uses ?? 0}</td>
                    <td className="px-4 py-2 text-plasma">
                      {Math.round((p.win_rate ?? 0) * 100)}%
                    </td>
                    <td className="px-4 py-2 text-ash">
                      {Number(p.mean_score ?? 0).toFixed(1)}
                    </td>
                    <td className="max-w-xl truncate px-4 py-2 text-ash/80">
                      {JSON.stringify(p.shape || {})}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
