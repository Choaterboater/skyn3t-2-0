# App Types

SkyN3t builds more than games. Each app type should get a different default
layout, interaction style, and UI strategy.

## Default groups

| App type | UI/style default | Notes |
| --- | --- | --- |
| Dashboard / admin | Dense panels, stats, tables, filters | Favor information density and clear state |
| Landing / marketing | Big hero, strong typography, limited sections | Optimize for clarity and conversion |
| CRUD app | Forms, lists, sidebars, modal dialogs | Prefer predictable navigation and validation |
| SaaS product | Nav shell, settings pages, empty states, onboarding | Needs a durable design system |
| CLI / developer tool | Minimal web UI or terminal-first output | Keep friction low, emphasize speed |
| Game | Canvas/engine surface plus HUD and overlays | Experimental track; run explicit game/all-stack bench suites |
| Data viz / analytics | Charts, legends, drilldowns, export actions | Surface scale and filtering clearly |
| Utility / one-off tool | Single-purpose form and result area | Reduce clutter; one screen is often enough |

## How the app type is chosen

- Prefer **auto-detection from the brief and code context**.
- Let the user override the choice in Settings or with `SKYN3T_APP_TYPE_OVERRIDE`.
- Only fall back to a default if the brief is ambiguous.
- Day-to-day reliability work should use the app-focused bench. Use the explicit
  `games` or `all` bench suite when changing game routing, assets, or playability.

## Layout profiles

Classification also selects one deterministic, versioned layout profile at build
start. The complete profile is serialized in the build manifest and remains
frozen for generation and Improve; Improve restores that delivered profile
rather than classifying the app again. An explicit app-type override therefore
determines the frozen profile for the build.

| Profile | App-type mapping | Layout treatment |
| --- | --- | --- |
| `workspace` | `dashboard`, `data_viz`, `crud_app`, `saas_product`, `product_app`, `rag_app`, `agent_workflow`, and `agent_pack` | At desktop widths, compose a meaningful work area using a split pane or asymmetric wide arrangement (for example, table/detail, chart/summary, timeline, inspector, or form workflow). The 1200–1600px range is fluid guidance, not a hard CSS rule; narrower screens must collapse responsively. |
| `editorial` | `landing_page`, `portfolio`, and `marketing` | Content-led layouts are exempt from the workspace split-pane and wide-composition rule. |
| `immersive` | `game`, plus canvas-first Phaser/static experiences | Game and canvas-first layouts are exempt from the workspace rule. |
| `compact` | Developer, native, mobile, unknown, and all other app types | Compact experiences are exempt from the workspace rule; backend, configuration, and test paths do not receive a dashboard workspace directive. |

The optional desktop browser audit applies only to `workspace`. It checks only
conservative aggregate layout measurements for under-filled work areas or
card-monoculture and offers a repair hint when it finds either. It is advisory
evidence, not a template library or a blocking visual gate.

## Style rules

- Use the same design-token system everywhere: one accent, fixed high-contrast
  neutrals, consistent spacing, and readable type.
- Do not reuse a game HUD for a dashboard, or a dashboard layout for a game.
- Keep configuration in a dedicated settings surface when the app needs keys or
  toggles.
- If the app type is unclear, default to a simple product shell: header, main
  content, optional sidebar, and one obvious primary action.

## What the agent should infer from the brief

1. **What kind of app is this?**
2. **Who is the user?**
3. **What is the primary action on screen?**
4. **Does it need a settings/config screen?**
5. **Does it need charts, tables, a canvas, or forms?**

If those answers are missing, the safest fallback is a clean, minimal shell with
the generated design tokens applied consistently.
