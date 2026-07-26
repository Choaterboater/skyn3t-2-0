# Organic layout profiles and visual QA

## Problem

Generated web products can be technically correct while looking mechanically
generated: a narrow centered column leaves most desktop space unused, every
section becomes an identical card, and dashboard/data applications inherit a
marketing-page rhythm. This is a generator-wide quality defect, not a single
template defect.

## Decision

Add a deterministic `layout_profile` beside the existing app-type
classification. It becomes part of the design direction passed from the
Studio runner through `DesignerAgent` and every frontend-writing CodeAgent
path. Profiles are a small contract, not a new visual theme or a rigid
template.

### Profiles

| Profile | App types | Desktop contract |
| --- | --- | --- |
| `workspace` | `dashboard`, `data_viz`, `crud_app`, `saas_product`, `product_app`, `rag_app`, `agent_workflow`, `agent_pack` | Use the full working area on wide screens; organize primary work in an asymmetric grid or split pane; combine a few purposeful surfaces with tables, charts, lists, or detail panes; reserve cards for groups that need containment rather than wrapping every element. |
| `editorial` | `landing_page`, `portfolio`, marketing-like briefs | Keep the existing intentional hero/section rhythm. A constrained reading column or large negative space is allowed. |
| `immersive` | `game` and canvas-first browser work | Preserve the canvas/playfield as the dominant surface; HUD and overlays remain game-specific. |
| `compact` | `developer_tool`, `utility`, API/server, native/mobile, and unknown non-web outputs | Do not impose a desktop web shell. Prefer the smallest interface that suits the task. |

`workspace` is the default only for visible DOM web products classified as
product/workspace applications. The app-type override remains authoritative;
the selected profile is frozen with the build submission so queued work cannot
drift if Settings changes.

### Workspace layout grammar

The prompt contract requires a responsive app shell with a meaningful primary
work area, a hierarchy of at least two visual surface types, and a desktop
layout that changes composition rather than merely stretching cards. At large
widths it should use a fluid content cap appropriate to the app (normally
roughly 1200–1600px), balanced gutters, and an explicit wide-screen grid or
split pane. At narrow widths it must collapse to one readable column without
horizontal overflow.

The contract explicitly forbids a page made only of uniformly styled cards in
a narrow max-width container when the brief asks for operational data,
configuration, analytics, records, or a multi-step workspace. It asks for
domain-appropriate alternatives: a table/list plus detail area, dense toolbar
and filters, summary strip plus chart, timeline, inspector, or form workflow.

## Data flow

1. `classify_build()` derives the frozen app type; a new pure resolver maps it
   and the stack/engine to a profile and a compact layout contract.
2. The runner records the profile in build metadata and passes it to the
   design task. `DesignerAgent` supplies a deterministic profile-specific
   fallback and asks an LLM to refine it, never to remove the contract.
3. `CodeAgent` includes the contract in normal, sliced, retry, and direct
   frontend prompts. The Product Contract/proof payload keeps the selected
   profile visible to later Improve calls.
4. Improve uses the delivered build's stored profile rather than reclassifying
   from mutable live settings.

## Visual audit

Add an optional, advisory `layout_audit` to the existing browser visual-check
path. When Playwright can capture a desktop viewport, it returns compact DOM
and screenshot-derived evidence:

- viewport and largest main-workspace width;
- workspace fill ratio after excluding a deliberate navigation rail;
- count/share of repeated same-style card surfaces;
- visible data-bearing controls (table rows, charts, lists, or form fields);
- exemption/profile reason and whether the viewport is mobile.

For `workspace` on a desktop viewport, the audit emits an actionable fix hint
when the primary workspace is materially under-filled, when visible data is
presented only as a monoculture of repeated cards, or when the composition is
effectively a single narrow column. It never blocks a build on unavailable
Playwright, screenshot failure, a mobile viewport, or an exempt profile. Its
findings feed the existing visual self-improve loop and build evidence, while
the existing vision review remains available as a complementary subjective
check.

The initial thresholds are deliberately conservative and configurable in code:
desktop starts at 1024px; a workspace warning needs both a content-fill ratio
below 0.62 and no deliberate wide navigation/reading profile; a card warning
needs at least four repeated, similarly sized surfaces that occupy the
majority of the primary workspace. Tests use injected metrics rather than a
browser.

## Compatibility and failure posture

- No profile changes the selected stack, provider/model route, or game/mobile
  behavior.
- Existing explicit design/reference-image instructions have precedence for
  theme and art; the profile still supplies responsive composition constraints.
- Classification and audit parsing fail safely to `compact`/`skipped` with
  provenance, never to a false build failure.
- The audit contains only measurements and selector-neutral summaries; it does
  not upload source or screenshots beyond the existing optional visual-review
  path.

## Test plan

1. Unit-test profile resolution, override handling, and exemptions.
2. Assert profile propagation through runner, DesignerAgent, all CodeAgent
   frontend prompt variants, retry, and Improve routing.
3. Unit-test DOM-metric normalization and workspace warnings with injected
   browser evidence, including mobile/editorial/game exemptions and malformed
   input soft skips.
4. Verify visual-loop findings are surfaced as advisory repair context without
   changing proof/build verdict semantics.
5. Run targeted Python suites, the full test suite, and the dashboard
   production build; perform a rendered desktop screenshot check for a
   workspace profile and an editorial exemption.
