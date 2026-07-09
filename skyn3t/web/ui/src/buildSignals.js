function buildIdForEvent(event) {
  return event?.payload?.build_id || event?.correlation_id || null;
}

export function latestBuildId(events = []) {
  let buildId = null;
  const seen = new Set();
  for (const event of events) {
    if (event?.type !== "build.started") continue;
    const candidate = buildIdForEvent(event);
    if (!candidate || seen.has(candidate)) continue;
    seen.add(candidate);
    buildId = candidate;
  }
  // Once the bounded socket history drops every build.started frame, retain
  // isolation by following the latest identified build event instead of
  // folding unrelated concurrent builds together.
  if (!buildId) {
    for (let i = events.length - 1; i >= 0; i -= 1) {
      buildId = buildIdForEvent(events[i]);
      if (buildId) break;
    }
  }
  return buildId;
}

export function latestBuildEvents(events = []) {
  const buildId = latestBuildId(events);
  if (!buildId) return events;
  return events.filter((event) => buildIdForEvent(event) === buildId);
}
