---
slug: desktop-editor-layout
title: desktop-editor-layout
stack: generic
tags: authored, design, editor, frontend, layout, sidebar, ui, web
uses: 21
helpful: 13
quality_sum: 16.0887
score: 0.766
source: authored
---

Lay out a desktop code/text editor (or IDE-like app) with the familiar, productive shell users expect. Use when building an editor, IDE, terminal, or any multi-pane desktop tool with a sidebar.

# Desktop Editor Layout

## The shell (match VS Code / GreenCli)
A pro editor has a fixed, recognizable chrome:
- **Title bar** — app name + logo/icon on the left, window/theme controls on the right. Brand-accent colored.
- **Left activity rail** (~48px) — a vertical strip of ICON BUTTONS (Files, Search, Snippets/Transforms, Settings) that switch the sidebar panel. Each is a real, clickable, labeled (tooltip/aria-label) button with an active state in the brand accent.
- **Sidebar** (collapsible, resizable) — shows the panel for the active rail icon (file tree, search results, snippet list…). Collapses to just the rail.
- **Editor area** — tab bar on top, the Monaco editor filling the rest; supports split panes.
- **Status bar** (bottom) — line/col, language, theme toggle, encoding.
- **Optional bottom panel** — find-results / output, toggleable.

## Make the left side easy to modify
- Drive the rail + sidebar from a small declarative config array (`{ id, icon, label, panel }`) so adding/reordering buttons is a one-line change, not a rewrite.
- Each rail button: keyboard-focusable, tooltip, active highlight, toggles its panel.
- The sidebar width is user-resizable (drag handle) and the choice persists.

## Layout mechanics
- CSS grid for the shell: `grid-template: "title title" auto "rail side editor" 1fr "status status status" auto`. Or fl/flex equivalents.
- Resizable splits via a draggable divider (pointer events); persist sizes.
- Everything keyboard-reachable; the command palette can run every rail action too.

## Branding in the chrome
- Show the app icon (SVG logo) in the title bar and as the favicon/window icon.
- Use the brand accent for the active rail icon, active tab underline, and focus rings (see desktop-app-theming).

## Avoid
- A single full-width column with no sidebar, anchor-only nav, or unlabeled mystery icons. The shell should look like a real editor, not a landing page.
