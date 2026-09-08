---
slug: gh-auth0-developer-hub-auth0-b2b-saas-starter
title: Auth0 B2B SaaS bootstrap pattern: isolated tenant + CLI-scripted provisioning
stack: nextjs
tags: auth, nextjs, reference, security, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-auth0-developer-hub-auth0-b2b-saas-starter
description: "Review hold: Bootstrap changes tenant-wide Auth0 settings and can overwrite configuration; requires a separately approved fresh-tenant operation, not default skill activation."
license: MIT
compatibility: nextjs
metadata:
  skyn3t-advisory-sha256: sha256:daafcf905e7da91a812f138a56e624a33f707ad4c6088cf6298bd2aef49034f6
  skyn3t-content-sha256: sha256:051761f79b67d9f7122c93b3af472e747ccf5e9a6b5df65a6114d36c233349bb
  skyn3t-evidence-index: evidence/reviewed/gh-auth0-developer-hub-auth0-b2b-saas-starter.receipt.json
  skyn3t-evidence-path: evidence/reviewed/051761f79b67d9f7122c93b3af472e747ccf5e9a6b5df65a6114d36c233349bb.source
  skyn3t-hold-reason: "Bootstrap changes tenant-wide Auth0 settings and can overwrite configuration; requires a separately approved fresh-tenant operation, not default skill activation."
  skyn3t-pinned-revision: 12ad50e980658da7b5893491d19138def6e92aa4
  skyn3t-previous-body-sha256: sha256:d35edd8d0d541ef47cc1ea2acfbe5daae85161a3a2947aa2a1d79053007574f2
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/auth0-developer-hub/auth0-b2b-saas-starter
---

Review hold: Bootstrap changes tenant-wide Auth0 settings and can overwrite configuration; requires a separately approved fresh-tenant operation, not default skill activation.

Applicability: bootstrapping a multi-tenant B2B SaaS application's identity layer (organizations, invitations, RBAC roles, SSO via OIDC/SAML, MFA) on Auth0, using a scripted setup instead of manually clicking through the Auth0 dashboard.

Prerequisites/version boundary: Node.js v20+ (verify with `node -v` before proceeding), `npm`, the Auth0 CLI, and -- critically -- a newly created, otherwise-empty Auth0 tenant (the source repeatedly warns not to reuse an existing tenant, since the bootstrap script both creates and can overwrite tenant-wide settings like email templates and MFA factors).

Pattern (source-backed):
1. Create a fresh Auth0 tenant specifically for this project (free tier available via Auth0 sign-up).
2. Clone the repo and `cd` into it, confirm `node -v` is 20+, then `npm install`.
3. Install the Auth0 CLI (e.g. via Homebrew on Mac/Linux: `brew tap auth0/auth0-cli && brew install auth0`; via Scoop on Windows), then authenticate scoped to only what the bootstrap needs: `auth0 login --scopes "update:tenant_settings,create:connections,create:client_grants,create:email_templates,update:guardian_factors"` -- selecting the new empty tenant, not any existing one, when prompted.
4. Run `npm run auth0:bootstrap`. This single script provisions Auth0 applications/clients, admin+member roles, actions for role/security-policy enforcement, email/login templates, and MFA factors, then writes the resulting environment variables to a generated `.env.local`.
5. Run `npm run dev` and open the local app to exercise the actual product surface: organization sign-up, user invitation/role management, SSO connection setup (OIDC/SAML) with optional SCIM, and per-user MFA/profile self-service.

Verification: a correct bootstrap produces a working sign-up flow that prompts for an organization name, and an admin who can invite a second user and see them appear with the correct role -- confirm both before treating the tenant as ready.

Failure handling: if the bootstrap step behaves unexpectedly, the most common cause documented by the source is running it against a tenant that already had prior configuration -- start over with a genuinely new, empty tenant rather than debugging in place.
