---
slug: gh-huakunshen-wol-web
title: PocketBase-backed Go service: swap hand-rolled REST+ORM for an embeddable BaaS
stack: go
tags: backend, docker, go, pocketbase, reference, self-hosted, stack:sveltekit, github-distilled, hygiene:quarantine, review-held
uses: 34
helpful: 20
quality_sum: 24.5236
score: 0.721
source: github-distilled
name: gh-huakunshen-wol-web
description: "Review hold: Go/PocketBase runtime is outside the supported factory builders, and this LAN-administration recipe needs project-specific networking and privilege review."
license: MIT
compatibility: go
metadata:
  skyn3t-advisory-sha256: sha256:31195680db6be9d55dd5de380b57c15ed53c9fb9fc34df34ed9ea919c694a055
  skyn3t-content-sha256: sha256:8d10e3a7e0709a35003e32bd8d6a77f1b5aa17b382bf71e773d33c8491d5cd1c
  skyn3t-evidence-index: evidence/reviewed/gh-huakunshen-wol-web.receipt.json
  skyn3t-evidence-path: evidence/reviewed/8d10e3a7e0709a35003e32bd8d6a77f1b5aa17b382bf71e773d33c8491d5cd1c.source
  skyn3t-hold-reason: "Go/PocketBase runtime is outside the supported factory builders, and this LAN-administration recipe needs project-specific networking and privilege review."
  skyn3t-pinned-revision: 85480436004a86472601d00e0882e872d4f3ab8b
  skyn3t-previous-body-sha256: sha256:263185e221608d69ca8dd678eb285c39ac31602800eac17bdf150684a2e38cf5
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/huakunshen/wol-web
---

Review hold: Go/PocketBase runtime is outside the supported factory builders, and this LAN-administration recipe needs project-specific networking and privilege review.

Applicability: small self-hosted Go services that need auth plus a database admin UI (user CRUD, sessions) without hand-writing a full REST API layer and an ORM.

Prerequisites/version boundary: PocketBase's Go extension mechanism (custom routes embedded into PocketBase rather than a server built from scratch); Docker for the container deployment path; Bun + Go for local development.

Pattern (source-backed architectural decision + deployment):
1. Design decision: instead of writing a full REST API server plus an ORM (e.g. gorm), embed custom logic (here: a wake-on-lan action) as a PocketBase Go extension. PocketBase supplies the CRUD API, auth, and a built-in DB admin UI for free; only the domain-specific route(s) need writing.
2. Container deployment: run with `--network=host` (needed to reach LAN devices; Docker Desktop on Mac lacks host networking, so run the Go binary directly there instead), with a persisted volume for the PocketBase data directory so state survives recreation. Pass `SUPERUSER_EMAIL`/`SUPERUSER_PASSWORD` to auto-provision an admin on first boot, or use the one-time setup URL PocketBase logs if omitted.
3. User provisioning is intentionally admin-gated: log into the PocketBase admin console with the superuser, then manually create regular users in the `users` collection -- there is no open self-registration endpoint by design.
4. Local dev: `bun install` then `bun run dev` starts both the SvelteKit frontend and the Go/PocketBase backend together (a Bun workspace monorepo); backend-only: `go run main.go serve` (or `air` for hot reload). After changing a PocketBase collection schema, run `go run . migrate collections` to generate the migration file.

Verification: after container start, confirm the superuser URL/login works at `/_/`, create a regular user, then confirm login with that user at the app's own login route.

Failure handling: if the container can't reach target devices, confirm `--network=host` was actually applied (Mac must fall back to running the Go binary natively); if data is lost after a restart, confirm the PocketBase data directory was mounted as a volume rather than left in the ephemeral container filesystem.
