// Shared build-status vocabulary for the dashboard: which lifecycle states
// count as ACTIVE (still running / cancellable / approvable) and which are
// TERMINAL. Extracted from Studio.jsx so app-level surfaces (the pending-
// approvals banner) use the exact same heuristic as the Foundry table.

export const BUILD_ACTIVE_STATUSES = new Set([
  "running",
  "queued",
  "queued_no_studio",
  "pending",
  "awaiting_approval",
]);

export const BUILD_TERMINAL_STATUSES = new Set([
  "completed",
  "completed_no_go",
  "cancelled",
  "failed",
  "approved",
  "rejected",
  "interrupted",
]);

export function normalizeBuildStatus(status) {
  return String(status || "").trim().toLowerCase();
}

export function isActiveBuild(status) {
  return BUILD_ACTIVE_STATUSES.has(normalizeBuildStatus(status));
}

export function isTerminalBuild(status) {
  return BUILD_TERMINAL_STATUSES.has(normalizeBuildStatus(status));
}
