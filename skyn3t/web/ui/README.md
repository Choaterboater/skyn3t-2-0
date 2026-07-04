# SkyN3t 2.0 Dashboard (web/ui)

Live swarm / pipeline / brain visualization for SkyN3t 2.0.

Stack: **Vite + React 18 + Tailwind CSS + @tanstack/react-query + react-router-dom + three / @react-three/fiber**.

This directory contains **source only** — there is no `node_modules/` and no build
output committed. You build it once and the backend serves the static `dist/`.

## Build

```bash
cd skyn3t/web/ui
npm install        # fetches deps into node_modules/
npm run build      # emits a production bundle into ./dist
```

The FastAPI control plane is expected to mount `dist/` as static files
(e.g. at `/`) and expose the JSON + WebSocket endpoints the UI consumes.

## Develop

```bash
npm run dev        # Vite dev server on http://localhost:5173
```

In dev, `/api/*` and `/ws` are proxied to the backend. Point the proxy at a
non-default host with:

```bash
SKYN3T_API=http://127.0.0.1:6660 npm run dev
```

## Endpoints consumed

All under the same origin as the served bundle (or proxied in dev):

| Route        | Endpoint                              | Method |
|--------------|---------------------------------------|--------|
| Overview     | `GET  /api/health`                    | poll   |
| Swarm        | `GET  /api/agents`                    | poll   |
| Studio       | `GET  /api/builds`, `POST /api/builds`| poll/submit |
| Cortex       | `GET  /api/cortex/proposals`, `POST /api/cortex/proposals/:id/decide` | poll/decide |
| Brain        | `GET  /api/brain`                     | poll   |
| Skills       | `GET  /api/skills`                    | poll   |
| Settings     | `GET  /api/settings`                  | poll   |
| (all pages)  | `WS   /ws`                            | live event stream |

Every endpoint is read defensively: the UI degrades to empty/placeholder
state when an endpoint is missing or the API is unreachable, so it remains
buildable and renderable even before the backend routes exist.

### WebSocket frames

`/ws` is expected to push JSON frames matching `skyn3t.core.events.Event.to_dict()`:

```json
{ "type": "BUILD_STAGE_COMPLETED", "source": "studio", "payload": {"stage": "codegen"},
  "id": "…", "timestamp": 1718600000.0, "correlation_id": "…" }
```

The Studio pipeline view and Brain glow are driven from these live events.
