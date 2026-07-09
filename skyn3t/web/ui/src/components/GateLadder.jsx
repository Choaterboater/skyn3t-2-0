import React, { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";
import { latestBuildEvents } from "../buildSignals.js";
import { Panel, PanelHead, Empty } from "./ui.jsx";

// The signature of the Foundry: the VERIFY LADDER. skyn3t doesn't just emit
// code — it builds ANY stack (web, API, mobile, desktop, games, RAG, agents,
// MCP, CLIs) and every build is forged, then must climb a rail of verification
// gates before it ships. Each station reads as heat: armed (ash) → forging
// (ember, pulsing) → proven (plasma, sealed). The molten rail flows while a
// build is live and rests cold when the forge is quiet. This is the one thing
// the control plane is remembered by, and it is TRUE to the product: the
// stations are the real gate registry (/api/gates), ordered by ascending rigor.

// What each gate PROVES, in the user's words. Ordered coarse→fine: it boots, it
// serves, it renders, its own contract holds, it looks right, it plays. Unknown
// gates still render with their raw name (breadth survives new stacks).
const GATE_META = {
  headless_gate: { rung: "Simulates", proves: "sim core runs clean", glyph: "◇", order: 10 },
  liveness: { rung: "Serves", proves: "boots & answers", glyph: "▰", order: 20 },
  seo_check: { rung: "Renders", proves: "a real page, not blank", glyph: "❏", order: 30 },
  mcp_check: { rung: "Speaks", proves: "the MCP contract holds", glyph: "⊚", order: 40 },
  rag_check: { rung: "Retrieves", proves: "ingest → query → answer", glyph: "❋", order: 41 },
  workflow_check: { rung: "Runs", proves: "the /trigger contract holds", glyph: "⇶", order: 42 },
  cli_check: { rung: "Obeys", proves: "the command surface works", glyph: "⌘", order: 43 },
  game_visual: { rung: "Looks right", proves: "vision-judged mid-play", glyph: "✺", order: 50 },
  qa_playtest: { rung: "Plays", proves: "driven end to end", glyph: "▶", order: 60 },
};

// A canonical fallback so the hero is never empty if /api/gates is unreachable.
const FALLBACK = ["headless_gate", "liveness", "seo_check", "rag_check", "qa_playtest"].map(
  (g) => ({ gate: g, enabled: true, stacks: [] })
);

// Read live heat off the event stream: which gate (if any) is running right now,
// and which just cleared. Gate stages surface as events whose type/source name
// the gate. Loose match by design — a resting ladder is the honest default.
function useGateHeat(events) {
  return useMemo(() => {
    const running = new Set();
    const passed = new Set();
    const failed = new Set();
    let sealed = false;

    for (const e of latestBuildEvents(events)) {
      const payload = e.payload || {};
      if (e.type === "build.completed") {
        const verdict = String(payload.verdict || payload.status || "").toLowerCase();
        sealed = ["go", "completed", "applied"].includes(verdict);
      } else if (e.type === "build.failed") {
        sealed = false;
      }

      const tag = [
        e.type,
        e.source,
        payload.stage,
        payload.gate,
        payload.capability,
      ].join(" ").toLowerCase();
      for (const gate of Object.keys(GATE_META)) {
        if (!tag.includes(gate)) continue;
        const starting =
          /(start|run|forg)/.test(tag) &&
          !/(done|complete|pass|fail|ok|kept)/.test(tag);
        const completing = /(done|complete|pass|fail|ok|kept)/.test(tag);
        const status = String(payload.status || payload.verdict || "").toLowerCase();
        const gateFailed =
          payload.passed === false ||
          ["failed", "no_go", "completed_no_go", "rejected"].includes(status);

        if (starting) {
          running.add(gate);
          passed.delete(gate);
          failed.delete(gate);
        } else if (completing) {
          running.delete(gate);
          if (gateFailed) {
            passed.delete(gate);
            failed.add(gate);
          } else {
            failed.delete(gate);
            passed.add(gate);
          }
        }
      }
    }
    return { running, passed, failed, live: running.size > 0, sealed };
  }, [events]);
}

function stationState(gate, enabled, heat) {
  if (heat.running.has(gate)) return "forging";
  if (heat.passed.has(gate)) return "proven";
  if (heat.failed.has(gate)) return "failed";
  if (enabled === false) return "off";
  return "armed";
}

const STATE_STYLES = {
  forging: { node: "border-ember bg-ember/15 text-ember shadow-ember animate-forgepulse", label: "text-ember", tag: "text-ember" },
  proven: { node: "border-plasma/60 bg-plasma/10 text-plasma shadow-plasma", label: "text-plasma", tag: "text-plasma/80" },
  failed: { node: "border-ember/70 bg-ember/10 text-ember", label: "text-ember", tag: "text-ember/80" },
  armed: { node: "border-hairline bg-void/70 text-ash", label: "text-bone", tag: "text-ash" },
  off: { node: "border-dashed border-hairline bg-transparent text-ash/40", label: "text-ash/50", tag: "text-ash/40" },
};

// One column of the climb: fixed-height label block (so the rail always threads
// the node centers, at every breakpoint) → node seated on the rail → gate name.
function Column({ rung, proves, glyph, labelCls, nodeCls, tagCls, name, index }) {
  return (
    <li
      className="relative flex min-w-0 flex-1 flex-col items-center gap-2 animate-risefade"
      style={{ animationDelay: `${index * 60}ms` }}
      title={proves ? `${rung} — ${proves}` : rung}
    >
      <div className="flex h-9 flex-col items-center justify-end gap-0.5 text-center">
        <span className={`font-display text-[13px] font-semibold leading-none ${labelCls}`}>{rung}</span>
        {proves ? <span className="hidden text-[10px] leading-tight text-ash md:block">{proves}</span> : null}
      </div>
      <span className={`relative z-10 grid h-10 w-10 place-items-center rounded-full border text-[15px] transition-all duration-300 ${nodeCls}`}>
        {glyph}
      </span>
      <span className={`font-mono text-[10px] ${tagCls}`}>{name}</span>
    </li>
  );
}

export default function GateLadder({ stream }) {
  const events = stream?.events || [];
  const gatesQ = useQuery({ queryKey: ["gates"], queryFn: queryFn("/gates"), retry: 0 });
  const heat = useGateHeat(events);

  const gates = useMemo(() => {
    const raw = Array.isArray(gatesQ.data?.gates) ? gatesQ.data.gates : null;
    const list = raw && raw.length ? raw : gatesQ.isError ? FALLBACK : raw || [];
    return [...list]
      .map((g) => ({ ...g, meta: GATE_META[g.gate] || { rung: g.gate, proves: "", glyph: "◆", order: 99 } }))
      .sort((a, b) => a.meta.order - b.meta.order);
  }, [gatesQ.data, gatesQ.isError]);

  const armed = gates.filter((g) => g.enabled !== false).length;
  const forging = heat.running.size;
  const proven = gates.filter((g) => heat.passed.has(g.gate)).length;
  const failed = gates.filter((g) => heat.failed.has(g.gate)).length;
  const sealed = heat.sealed;
  const railCls =
    "forge-rail " +
    (heat.live ? "is-live" : failed ? "is-failed" : sealed ? "is-proven" : "is-cold");

  return (
    <Panel glow={heat.live} className="mb-6 overflow-hidden">
      <PanelHead
        label="The Verify Ladder"
        right={
          <span className="font-mono text-[11px]">
            {forging ? (
              <span className="text-ember">forging · {forging} gate{forging > 1 ? "s" : ""} hot</span>
            ) : failed ? (
              <span className="text-ember">{failed} gate{failed > 1 ? "s" : ""} failed</span>
            ) : sealed ? (
              <span className="text-plasma">
                {"build proven" +
                  (proven ? " · " + proven + " gate" + (proven > 1 ? "s" : "") + " cleared" : "")}
              </span>
            ) : (
              <span className="text-ash">{armed} gates armed</span>
            )}
          </span>
        }
      />

      {gates.length === 0 ? (
        <Empty icon="▰">No gates registered. Submit a build to arm the ladder.</Empty>
      ) : (
        <div className="px-5 pb-6 pt-5">
          <div className="overflow-x-auto overscroll-x-contain pb-2 [scrollbar-gutter:stable]">
            <div className="relative min-w-[720px] sm:min-w-0">
              {/* the molten rail — threads the node centers at a fixed offset */}
              <div className={railCls} aria-hidden="true" />
              <ol className="relative flex items-start justify-between gap-1.5">
                {gates.map((g, i) => {
                  const state = stationState(g.gate, g.enabled, heat);
                  const s = STATE_STYLES[state] || STATE_STYLES.armed;
                  return (
                    <Column
                      key={g.gate}
                      rung={g.meta.rung}
                      proves={g.meta.proves}
                      glyph={state === "proven" ? "✓" : g.meta.glyph}
                      labelCls={s.label}
                      nodeCls={s.node}
                      tagCls={s.tag}
                      name={g.gate}
                      index={i}
                    />
                  );
                })}
                {/* the payoff seal at the top of the climb */}
                <Column
                  rung="Proven"
                  proves="ships"
                  glyph="⬢"
                  index={gates.length}
                  labelCls={sealed ? "text-plasma" : "text-ash/60"}
                  nodeCls={sealed ? "border-plasma/60 bg-plasma/15 text-plasma shadow-plasma" : "border-hairline bg-void/70 text-ash/50"}
                  tagCls="text-ash/60"
                  name="verdict"
                />
              </ol>
            </div>
          </div>
          <p className="mt-5 max-w-2xl font-mono text-[11px] leading-relaxed text-ash">
            Any stack — web, API, mobile, desktop, games, RAG, agents, MCP, CLIs — is forged, then
            climbs the ladder: it must pass every gate before it ships. Others emit code; the Foundry
            proves it.
          </p>
        </div>
      )}
    </Panel>
  );
}
