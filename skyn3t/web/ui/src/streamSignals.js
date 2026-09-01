// Pure signals derived from the live event-stream hook (useEventStream).
// Dependency-free so `node --test` can exercise them directly.

// A dead stream must not masquerade as live telemetry. When the websocket is
// not open, every panel folding `stream.events` (GateLadder heat, the Studio
// forge line, the Projects serve map) keeps rendering the frozen buffer —
// previously the only visible change was a 2px sidebar dot. This helper is
// the single decision point for labelling that data stale.
//
// `lastFrameAt` (wall-clock ms of the newest websocket frame, or null before
// any frame arrived) guards the initial page load: a fresh "connecting"
// stream with no frames yet is merely loading, not stale — without the guard
// the banner would flash on every load.
export function streamStaleness(status, lastFrameAt) {
  const stale = status !== "open" && lastFrameAt != null;
  return { stale, since: stale ? lastFrameAt : null };
}
