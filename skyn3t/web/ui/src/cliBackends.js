export const CLI_BACKENDS = [
  {
    id: "codex_cli",
    provider: "codex",
    command: "codex",
    label: "Codex CLI",
    account: "OpenAI account",
  },
  {
    id: "claude_cli",
    provider: "claude",
    command: "claude",
    label: "Claude CLI",
    account: "Anthropic account",
  },
  {
    id: "copilot_cli",
    provider: "copilot",
    command: "copilot",
    label: "Copilot CLI",
    account: "GitHub account",
  },
  {
    id: "kimi_cli",
    provider: "kimi",
    command: "kimi",
    label: "Kimi CLI",
    account: "Kimi account",
  },
];

export const BACKEND_OPTIONS = [
  { id: "auto", label: "Auto", kind: "policy" },
  ...CLI_BACKENDS.map((option) => ({ ...option, kind: "cli" })),
  { id: "openrouter", label: "OpenRouter", kind: "api" },
  { id: "stub", label: "Offline stub", kind: "offline" },
];

export const CODEGEN_CLI_OPTIONS = [
  { id: "", provider: "", label: "None (follow global backend)", kind: "policy" },
  ...CLI_BACKENDS.map((option) => ({
    ...option,
    id: option.provider,
    kind: "cli",
  })),
];

export function backendOption(backend) {
  return BACKEND_OPTIONS.find((option) => option.id === backend) || null;
}

export function cliBackendStatus(payload, backend) {
  const option = backendOption(backend);
  if (!option || option.kind !== "cli") {
    return {
      option,
      checked: true,
      available: true,
      availabilityLabel: "available",
      detail: null,
    };
  }

  const status = payload?.status || payload?.routing || {};
  const detail = status?.cli_details?.[option.provider] || null;
  const detailAvailable = detail?.available;
  const legacyAvailable = status?.cli_available?.[option.provider];
  const checked = typeof detailAvailable === "boolean" || typeof legacyAvailable === "boolean";
  const available = typeof detailAvailable === "boolean"
    ? detailAvailable
    : typeof legacyAvailable === "boolean"
      ? legacyAvailable
      : null;

  return {
    option,
    checked,
    available,
    availabilityLabel: checked ? (available ? "available" : "not found") : "checking",
    detail,
  };
}

export function backendOptionLabel(option, payload) {
  if (option.kind !== "cli" && !option.provider) return option.label;
  const status = cliBackendStatus(payload, option.id);
  return `${option.label} - ${status.availabilityLabel}`;
}

export function cliAccountBillingText(backend) {
  const option = backendOption(backend);
  if (!option || option.kind !== "cli") return "";
  return `${option.label} uses its signed-in ${option.account}. Usage limits and billing follow that account's plan or subscription.`;
}

export function cliProviderStatus(payload, provider) {
  const option = CLI_BACKENDS.find((candidate) => candidate.provider === provider);
  return option
    ? cliBackendStatus(payload, option.id)
    : {
        option: null,
        checked: true,
        available: provider === "" ? true : null,
        availabilityLabel: provider === "" ? "available" : "checking",
        detail: null,
      };
}
