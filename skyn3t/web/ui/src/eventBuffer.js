// Pure helpers for the live event buffer used by useEventStream (api.js).
// Dependency-free so node tests can exercise them without pulling React.

// Append one live websocket frame, dropping duplicates by event id. The
// server re-primes its last 50 frames on every reconnect; without id dedup
// each blip duplicated rows in Activity and the Overview tail.
export function appendEventBounded(prev, evt, maxEvents) {
  if (evt && evt.id && prev.some((e) => e && e.id === evt.id)) return prev;
  const next = [...prev, evt];
  return next.length > maxEvents ? next.slice(next.length - maxEvents) : next;
}

// Merge a REST /trajectory seed with whatever websocket frames already
// arrived (primed/live frames can land before the fetch resolves): dedup by
// id (seed first — same id, same event), order by timestamp ascending to
// match the stream, and keep the newest maxEvents.
export function mergeSeededEvents(prev, seed, maxEvents) {
  const byId = new Map();
  for (const e of [...(Array.isArray(seed) ? seed : []), ...prev]) {
    if (!e) continue;
    const key = e.id ? e.id : `anon:${e.timestamp}:${e.type}`;
    if (!byId.has(key)) byId.set(key, e);
  }
  const merged = [...byId.values()].sort(
    (a, b) => (a?.timestamp || 0) - (b?.timestamp || 0),
  );
  return merged.length > maxEvents
    ? merged.slice(merged.length - maxEvents)
    : merged;
}
