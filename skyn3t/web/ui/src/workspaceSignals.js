export function workspaceEventMatches(event, slug, correlationIds = new Set()) {
  if (!slug || !event) return false;
  const type = String(event.type || "");
  if (!type.startsWith("serve.") && !type.startsWith("improve.")) return false;
  if (event.payload?.slug === slug) return true;
  const cid = event.correlation_id;
  return Boolean(cid && correlationIds?.has?.(cid));
}

export function countWorkspaceActivity(events, slug, correlationIds = new Set()) {
  return (events || []).filter((event) =>
    workspaceEventMatches(event, slug, correlationIds),
  ).length;
}
