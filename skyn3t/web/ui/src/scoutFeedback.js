function reasonText(reason) {
  const code = typeof reason === "string" ? reason : "";
  const status = /^http_([1-5]\d\d)$/.exec(code);
  if (status) return `GitHub HTTP ${status[1]}`;
  switch (code) {
    case "no_httpx": return "HTTP client unavailable";
    case "timeout": return "request timed out";
    case "http_error": return "network request failed";
    case "malformed_json": return "malformed GitHub response";
    case "malformed_items": return "invalid repository data";
    default: return "reason unavailable";
  }
}

export function scoutFeedback(response) {
  if (response == null) return null;
  const unknown = {
    tone: "warning",
    message: "The scout outcome could not be verified. No live-research success is assumed.",
  };
  if (typeof response !== "object" || Array.isArray(response)) return unknown;
  if (response.error) {
    return {
      tone: "error",
      message: `Scout failed: ${typeof response.error === "string"
        ? response.error.slice(0, 240) : "unexpected response error"}`,
    };
  }

  const receipt = response.receipt;
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) return unknown;
  const count = receipt.items_count;
  if (!Number.isSafeInteger(count) || count < 0) return unknown;
  const plural = count === 1 ? "candidate" : "candidates";
  if (receipt.source === "github" && receipt.outcome === "live") {
    const page = Number.isSafeInteger(receipt.page) && receipt.page > 0
      ? ` on page ${receipt.page}` : "";
    const message = count === 0
      ? "GitHub returned no matches for this page."
      : `GitHub returned ${count} ${plural}${page}.`;
    if (receipt.reason === "cursor_persist_failed") {
      return {
        tone: "warning",
        message: `${message} The next-page cursor could not be saved; a restart may revisit this page.`,
      };
    }
    return { tone: count === 0 ? "neutral" : "success", message };
  }

  const seeds = `${count} offline seed ${plural}`;
  if (receipt.source === "offline_seed" && receipt.outcome === "offline") {
    return {
      tone: "warning",
      message: `${seeds} returned. No live GitHub research was performed (${reasonText(receipt.reason)}).`,
    };
  }
  if (receipt.source === "offline_seed" && receipt.outcome === "degraded") {
    return {
      tone: "warning",
      message: `Live GitHub research failed (${reasonText(receipt.reason)}). ${seeds} returned; these are not live discoveries.`,
    };
  }
  return unknown;
}
