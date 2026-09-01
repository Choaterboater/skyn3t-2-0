export const DEPLOY_PROVIDERS = [
  "fly",
  "vercel",
  "cloudflare",
  "netlify",
  "railway",
  "render",
];

export function deployProviderConfigured(status, provider) {
  return status?.providers?.[provider] === true;
}

export function withDeployCredentialStatus(status, provider, configured) {
  const previousDetail = status?.provider_details?.[provider] || {};
  const nextConfigured = configured === true;
  return {
    ...(status || {}),
    providers: {
      ...((status && status.providers) || {}),
      [provider]: nextConfigured,
    },
    provider_details: {
      ...((status && status.provider_details) || {}),
      [provider]: {
        ...previousDetail,
        configured: nextConfigured,
        ready: Boolean(
          status?.allow_remote_deploy &&
          nextConfigured &&
          previousDetail.cli_available === true
        ),
      },
    },
  };
}

export function deployProviderDetail(status, provider) {
  const detail = status?.provider_details?.[provider];
  if (detail && typeof detail === "object") return detail;
  const configured = deployProviderConfigured(status, provider);
  const cliAvailable = status?.cli_available?.[provider] === true;
  return {
    configured,
    cli: provider,
    cli_available: cliAvailable,
    ready: Boolean(status?.allow_remote_deploy && configured && cliAvailable),
  };
}
