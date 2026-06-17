import React from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";

export default function Skills() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: queryFn("/skills"),
  });

  const skills = Array.isArray(data) ? data : data?.skills || [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Skills</h1>
        <p className="text-sm text-slate-500">
          Reusable capabilities registered in the skills hub.
        </p>
      </header>

      {error ? (
        <div className="card border-rose-800 text-sm text-rose-300">
          {String(error.message)}
        </div>
      ) : null}

      {isLoading ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : skills.length === 0 ? (
        <p className="text-sm text-slate-500">No skills registered.</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {skills.map((s) => (
            <div key={s.name || s.id} className="card">
              <div className="font-medium text-slate-100">{s.name}</div>
              <p className="mt-1 text-sm text-slate-400">{s.description}</p>
              {Array.isArray(s.tags) && s.tags.length ? (
                <div className="mt-2 flex flex-wrap gap-1">
                  {s.tags.map((t) => (
                    <span key={t} className="badge bg-slate-800 text-slate-400">
                      {t}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
