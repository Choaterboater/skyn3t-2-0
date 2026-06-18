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
    </div>
  );
}
