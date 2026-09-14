# SkyN3t 2.0 Dashboard (web/ui)

SkyN3t's product workspace for building, verifying, running, and improving projects.

Stack: **Vite 8 + React 19 + Tailwind CSS 3 + @tanstack/react-query + react-router 8**. Three.js and `@react-three/fiber` are lazy-loaded only for the Brain view.

## Workspace navigation and appearance

The sidebar groups all ten routes into Work, Intelligence, and System areas. On smaller screens it becomes a labelled navigation drawer so page content keeps the full viewport width. Use **Cmd+K** on macOS or **Ctrl+K** elsewhere to open **Find a page**, then filter and move with the arrow keys or Enter. Escape closes the palette and mobile drawer.

The interface follows the operating system's light/dark preference until the user explicitly selects a theme. Only that explicit selection is stored. If browser preference storage is blocked, the dashboard remains usable and announces that the selection may not persist. Both themes use the self-hosted Inter, Space Grotesk, and JetBrains Mono fonts from `public/fonts`; no remote fonts or artwork are loaded.

Build begins with a labelled multiline app brief and optional reference image; execution, routing, model, advisor, budget, pipeline, and outcome controls retain their backend-backed behavior. Projects can be filtered by identity, stack, status, verdict, delivery state, or import source before continuing in Workspace. Metrics and cleanup controls follow the project collection under an operations disclosure.

The production `dist/` is committed and included in Python wheels so an
installed control plane has a working dashboard without Node. Rebuild it after
changing UI source; `node_modules/` remains local and ignored.

## Build

```bash
cd skyn3t/web/ui
npm ci             # reproduce the locked dependency graph
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

In PowerShell:

```powershell
$env:SKYN3T_API="http://127.0.0.1:6660"
npm run dev
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

Every endpoint is read defensively. The UI keeps loading, request-error,
cached-stale, unavailable, and unverified states explicit instead of presenting
missing API data as an empty or successful result.

### WebSocket frames

`/ws` is expected to push JSON frames matching `skyn3t.core.events.Event.to_dict()`:

```json
{ "type": "BUILD_STAGE_COMPLETED", "source": "studio", "payload": {"stage": "codegen"},
  "id": "…", "timestamp": 1718600000.0, "correlation_id": "…" }
```

The Studio pipeline view and Brain glow are driven from these live events.
