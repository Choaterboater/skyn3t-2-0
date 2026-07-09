export function sliceTaskIdentity(event) {
  const payload = event?.payload || {};
  const metadata = payload.metadata || {};
  const slice = String(metadata.slice || payload.slice || "").trim();
  if (!slice) return null;
  const stage = String(metadata.stage || payload.stage || payload.type || "code").trim();
  return { stage, slice, label: `${stage}/${slice}` };
}

export function settleSliceRowsOnBuildTerminal(rows, event) {
  const type = String(event?.type || "").toLowerCase();
  if (type !== "build.completed" && type !== "build.failed") return;

  const failed = type === "build.failed";
  const payload = event?.payload || {};
  const status = String(payload.status || (failed ? "failed" : "completed")).trim();
  const gap = String(payload.error || payload.reason || "").trim();

  for (const row of rows || []) {
    if (row?.capability !== "slice" || !["pending", "running"].includes(row.state)) continue;
    row.state = failed ? "failed" : "done";
    row.status = status;
    if (failed && gap) row.gaps = [gap];
  }
}

function eventAgentKeys(event) {
  const payload = event?.payload || {};
  const metadata = payload.metadata || {};
  const slice = sliceTaskIdentity(event);
  return new Set(
    [
      payload.agent_name,
      payload.agent_type,
      payload.stage,
      payload.capability,
      metadata.stage,
      slice?.label,
      event?.source,
    ]
      .map((value) => String(value || "").trim())
      .filter(Boolean),
  );
}

function stageIdentity(event) {
  const payload = event?.payload || {};
  const stage = [
    payload.stage,
    payload.agent_type,
    payload.capability,
    payload.agent_name,
  ].find((value) => String(value || "").trim());
  return `${event?.correlation_id || ""}:${String(stage || "stage").trim()}`;
}

function taskIdentity(event) {
  const taskId = String(event?.payload?.task_id || "").trim();
  return taskId ? `${event?.correlation_id || ""}:${taskId}` : "";
}

export function agentActivity(events = []) {
  const activeCounts = new Map();
  const activeTasks = new Map();
  const activeStages = new Map();
  const lastByKey = new Map();

  const bump = (keys, delta) => {
    for (const key of keys) {
      const count = Math.max(0, (activeCounts.get(key) || 0) + delta);
      if (count) activeCounts.set(key, count);
      else activeCounts.delete(key);
    }
  };

  for (const event of events) {
    const type = String(event?.type || "").toLowerCase();
    const keys = eventAgentKeys(event);
    for (const key of keys) lastByKey.set(key, event.type);

    if (type.includes("task.started")) {
      const taskId = taskIdentity(event);
      const prior = taskId ? activeTasks.get(taskId) : null;
      if (prior) bump(prior.keys, -1);
      bump(keys, 1);
      if (taskId) activeTasks.set(taskId, { keys, correlationId: event?.correlation_id });
    } else if (type.includes("task.completed") || type.includes("task.failed")) {
      const taskId = taskIdentity(event);
      const started = taskId ? activeTasks.get(taskId) : null;
      bump(started?.keys || keys, -1);
      if (taskId) activeTasks.delete(taskId);
    } else if (type.includes("stage.started")) {
      const stageId = stageIdentity(event);
      const prior = activeStages.get(stageId);
      if (prior) bump(prior.keys, -1);
      bump(keys, 1);
      activeStages.set(stageId, { keys, correlationId: event?.correlation_id });
    } else if (type.includes("stage.completed")) {
      const stageId = stageIdentity(event);
      const started = activeStages.get(stageId);
      bump(started?.keys || keys, -1);
      activeStages.delete(stageId);
    } else if (type === "build.completed" || type === "build.failed") {
      const correlationId = event?.correlation_id;
      for (const [taskId, active] of activeTasks) {
        if (active.correlationId !== correlationId) continue;
        bump(active.keys, -1);
        activeTasks.delete(taskId);
      }
      for (const [stageId, active] of activeStages) {
        if (active.correlationId !== correlationId) continue;
        bump(active.keys, -1);
        activeStages.delete(stageId);
      }
    }
  }

  return { busy: new Set(activeCounts.keys()), lastByKey };
}

export function agentKeys(agent) {
  return [agent?.name, agent?.agent_type, agent?.type]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
}

export function agentIsBusy(agent, activity) {
  return agentKeys(agent).some((key) => activity.busy.has(key));
}

export function agentLastEvent(agent, activity) {
  for (const key of agentKeys(agent)) {
    const eventType = activity.lastByKey.get(key);
    if (eventType) return eventType;
  }
  return null;
}
