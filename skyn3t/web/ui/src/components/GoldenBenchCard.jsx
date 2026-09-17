import React from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { Panel, PanelHead } from "./ui.jsx";

// Bench builds run in isolated state (never in build memory), so this card
// reads the durable ledgers via /bench/golden — the only honest live view.
// Mounted on both Overview and the Foundry build console.
export default function GoldenBenchCard() {
  const bench = useQuery({
    queryKey: ["bench-golden"],
    queryFn: queryFn("/bench/golden"),
    refetchInterval: 5000,
  });
  const ledgers = Array.isArray(bench.data?.ledgers) ? bench.data.ledgers : [];
  if (ledgers.length === 0) return null;
  return (
    <Panel className="mb-6 overflow-hidden">
      <PanelHead
        label="Benchmark evidence"
        right={<span className="font-mono text-[11px] text-ash">artifacts/golden</span>}
      />
      <p className="border-b border-hairline px-4 py-3 text-xs text-ash">Offline stub benchmarks check the test harness, not live AI product quality. Provider-enabled runs cover only their recorded suite and configuration; neither rate guarantees this build will work.</p>
      <ul className="divide-y divide-hairline/60">
        {ledgers.map((ledger) => {
          const running = ledger.status === "partial";
          const done = ledger.expected
            ? `${ledger.attempts}/${ledger.expected}`
            : `${ledger.attempts}`;
          const rate = ledger.attempts
            ? `${Math.round((ledger.passed / ledger.attempts) * 100)}%`
            : "—";
          const updated = typeof ledger.updated_at === "number" && ledger.updated_at > 0 ? new Date(ledger.updated_at * 1000) : null;
          const completed = ledger.completed_at ? new Date(ledger.completed_at) : null;
          const date = completed && !Number.isNaN(completed.getTime()) ? completed : updated;
          const scope = ledger.live === true ? "Provider-enabled benchmark" : ledger.llm_backend === "stub" ? "Offline stub benchmark" : "Benchmark provider scope not confirmed";
          return (
            <li key={ledger.name} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 text-sm">
              <div className="min-w-0 flex-1 basis-48">
                <span className="break-words font-mono text-bone">{ledger.name}</span>
                <span className={ledger.live ? "badge border-ember/50 text-ember" : "badge border-hairline text-ash"}>{ledger.live ? "provider" : "floor"} · {ledger.llm_backend || "backend not recorded"}</span>
                <p className="mt-1 text-xs text-ash">{scope} · {date && !Number.isNaN(date.getTime()) ? `${completed && !Number.isNaN(completed.getTime()) ? "Completed" : "Ledger updated"} ${date.toLocaleString()}` : "Date not recorded"} · case scope not supplied</p>
              </div>
              <span className={running ? "text-ember-soft" : "text-ash"}>{running ? "running" : ledger.status || "status not recorded"}</span>
              <span className="font-mono text-[12px] text-ash">
                {done} attempts · {ledger.passed} passed · {rate} within this ledger
              </span>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
