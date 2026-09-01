import { latestBuildEvents } from "./buildSignals.js";

// Pure heat fold for the Verify Ladder, extracted from GateLadder.jsx so the
// terminal-state behavior is testable in plain node (the JSX component only
// memoizes this). `gateNames` is the station vocabulary (the ladder's
// GATE_META keys) — passed in so this module stays free of presentation.
//
// Live heat is read off the event stream: which gate (if any) is running
// right now, and which just cleared. Gate stages surface as events whose
// type/source name the gate. Loose match by design — a resting ladder is the
// honest default. Terminal build events additionally settle the ladder: a
// dead build must never keep pulsing "forging", and the gate_findings ledger
// on build.completed is the authoritative record of WHICH gate blocked the
// verdict — event-name sniffing never sees it.
export function gateHeatFromEvents(events, gateNames) {
  const running = new Set();
  const passed = new Set();
  const failed = new Set();
  let sealed = false;
  let blockedBy = null;

  for (const e of latestBuildEvents(events)) {
    const payload = e.payload || {};
    if (e.type === "build.completed") {
      const verdict = String(payload.verdict || payload.status || "").toLowerCase();
      sealed = ["go", "completed", "applied"].includes(verdict);
      // The build is over: leftover "running" gates died mid-flight
      // (cancel/exception) — return them to armed rather than pulsing
      // forever. Never fabricate a pass.
      running.clear();
      const findings = Array.isArray(payload.quality_scorecard?.gate_findings)
        ? payload.quality_scorecard.gate_findings
        : [];
      for (const finding of findings) {
        if (!finding || typeof finding !== "object") continue;
        const gate = String(finding.gate || "").trim();
        if (finding.status === "failed" && gateNames.includes(gate)) {
          passed.delete(gate);
          failed.add(gate);
        }
        if (!blockedBy && finding.blocked === true && gate) {
          blockedBy = { gate, reason: String(finding.reason || "").trim() };
        }
      }
    } else if (e.type === "build.failed") {
      sealed = false;
      // The build died: anything still forging did not finish — surface it
      // as failed instead of leaving a dead build "running".
      running.forEach((gate) => failed.add(gate));
      running.clear();
    }

    // Gate repair dispatches nest the gate name at payload.metadata.stage
    // (the orchestrator wraps TaskRequest.metadata), not payload.stage.
    const tag = [
      e.type,
      e.source,
      payload.stage,
      payload.gate,
      payload.capability,
      payload.metadata?.stage,
    ].join(" ").toLowerCase();
    for (const gate of gateNames) {
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
  return { running, passed, failed, live: running.size > 0, sealed, blockedBy };
}
