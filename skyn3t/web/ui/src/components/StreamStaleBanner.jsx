import React from "react";
import { streamStaleness } from "../streamSignals.js";

// One shared "the live data is frozen" banner, mounted above every panel that
// folds stream.events into live-looking status (Overview's verify ladder, the
// Studio cockpit, the Projects serve column). Without it a dead websocket
// only changed the tiny sidebar dot while every panel kept pulsing "live".
// Kept as a single component so the three routes cannot drift apart.
export default function StreamStaleBanner({ stream, className = "mb-4" }) {
  const { stale, since } = streamStaleness(stream?.status, stream?.lastFrameAt);
  if (!stale) return null;
  const when = new Date(since).toLocaleTimeString();
  return (
    <div
      role="alert"
      className={`rounded-md border border-ember/50 bg-ember/10 px-4 py-2.5 font-mono text-[11px] leading-relaxed text-ember ${className}`}
    >
      event stream disconnected — live data below is stale since {when};
      reconnecting…
    </div>
  );
}
