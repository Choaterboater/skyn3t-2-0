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
        label="Golden bench"
        right={<span className="font-mono text-[11px] text-ash">artifacts/golden</span>}
      />
      <ul className="divide-y divide-hairline/60">
        {ledgers.map((ledger) => {
          const running = ledger.status === "partial";
          const done = ledger.expected
            ? `${ledger.attempts}/${ledger.expected}`
            : `${ledger.attempts}`;
          const rate = ledger.attempts
            ? `${Math.round((ledger.passed / ledger.attempts) * 100)}%`
            : "—";
          return (
            <li key={ledger.name} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 px-4 py-2 text-sm sm:flex sm:gap-4">
              <span className="font-mono text-bone">{ledger.name}</span>
              {ledger.live ? (
                <span className="badge border-ember/50 text-ember">provider · {ledger.llm_backend}</span>
              ) : (
                <span className="badge border-hairline text-ash">floor · {ledger.llm_backend}</span>
              )}
              <span className={`col-span-2 ${running ? "text-ember-soft" : "text-ash"}`}>
                {running ? "running" : ledger.status}
              </span>
              <span className="col-span-2 font-mono text-[12px] text-ash sm:ml-auto">
                {done} attempts · <span className="text-plasma">{ledger.passed} passed</span> · {rate}
              </span>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
