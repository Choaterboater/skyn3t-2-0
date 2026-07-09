function eventAgentKeys(event) {
  const payload = event?.payload || {};
  return new Set(
    [
      payload.agent_name,
      payload.agent_type,
      payload.stage,
      payload.capability,
      event?.source,
    ]
      .map((value) => String(value || "").trim())
      .filter(Boolean),
  );
}

export function agentActivity(events = []) {
  const busy = new Set();
  const lastByKey = new Map();

  for (const event of events) {
    const type = String(event?.type || "").toLowerCase();
    const keys = eventAgentKeys(event);
    for (const key of keys) lastByKey.set(key, event.type);

    if (type.includes("task.started") || type.includes("stage.started")) {
      for (const key of keys) busy.add(key);
    } else if (type.includes("completed") || type.includes("failed")) {
      for (const key of keys) busy.delete(key);
    }
  }

  return { busy, lastByKey };
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
