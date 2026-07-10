export function chooseDeployTarget(options, current = "") {
  if (!Array.isArray(options) || options.length === 0) return "";
  if (current && options.some((item) => item?.target === current)) return current;
  return (
    options.find((item) => item?.ready)?.target ||
    options.find((item) => item?.target)?.target ||
    ""
  );
}

export function activeDeploymentIndex(deployments, liveUrl = "") {
  if (!Array.isArray(deployments)) return null;
  for (let index = deployments.length - 1; index >= 0; index -= 1) {
    if (deployments[index]?.manifest_pointer_active) return index;
  }
  if (liveUrl) {
    for (let index = deployments.length - 1; index >= 0; index -= 1) {
      if (deployments[index]?.ok && deployments[index]?.url === liveUrl) return index;
    }
  }
  return null;
}

export function canMoveDeploymentPointerBack(deployments, liveUrl = "") {
  const active = activeDeploymentIndex(deployments, liveUrl);
  if (active == null) return false;
  return deployments.some(
    (item, index) =>
      index < active &&
      item?.ok &&
      item?.url &&
      !item?.manifest_pointer_rolled_back,
  );
}

export function deploymentHealthLabel(check) {
  if (!check || typeof check !== "object" || Object.keys(check).length === 0) {
    return "not checked";
  }
  if (check.ok) return "healthy";
  if (check.skipped) return "check skipped";
  return "unhealthy";
}

export function safeDeploymentUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";
  } catch (_) {
    return "";
  }
}
