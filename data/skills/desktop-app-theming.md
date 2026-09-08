---
slug: desktop-app-theming
title: desktop-app-theming
stack: generic
tags: authored, branding, color, design, desktop, frontend, secrets, security, theming, ui, web
uses: 21
helpful: 13
quality_sum: 16.0887
score: 0.766
source: authored
---

Give a desktop/web app a distinctive, branded look instead of a generic AI gray. Use when output looks bland/unbranded. The brand color is ALWAYS derived from THIS app's name/brief — never a fixed default.

# Desktop App Theming

## Overview
A polished app reads as *intentionally designed*: one brand color used consistently, a real dark theme, and a clear visual hierarchy — not default Tailwind gray on white.

## Pick the accent from THIS app (do NOT default to any color)
Derive the single brand/accent color from the app's own name or brief, and use a sensible neutral (indigo/slate) only when nothing implies a color:
- A name or brief naming a color → that hue ("Green*"/"green" → green; "blue"/"ocean" → blue; "crimson"/"red" → red).
- A logo/brand color in the brief → match it.
- Nothing implied → a tasteful neutral accent (e.g. indigo `#6366f1` or slate). DO NOT apply green (or any one color) to apps that don't ask for it — the green below is just an *example* for a green-named app.

## Design tokens (do this first)
Define tokens once (CSS variables in `:root` + a `[data-theme="dark"]` block, or the tailwind theme) and reference them everywhere — never hard-code hex per component:
- `--accent` (the brand color) + `--accent-hover`, `--accent-muted`
- `--bg`, `--bg-elevated` (panels/sidebars), `--border`, `--text`, `--text-muted`
- Ship BOTH a dark and a light theme; default to dark for a code/editor app.

Example (green brand):
```css
:root {
  --accent: #16a34a; --accent-hover: #15803d; --accent-muted: #16a34a22;
  --bg: #0f1411; --bg-elevated: #161d18; --border: #243029;
  --text: #e6efe9; --text-muted: #8aa194;
}
```

## Apply the brand color with intent
- Active tab/route, focus rings, primary buttons, selection, the logo, links, and the title-bar accent all use `--accent`.
- Don't paint everything green — accent is for emphasis; neutrals carry the surface.
- Status bar / sidebar use `--bg-elevated` so panels read distinct from the editor surface.

## Typography & spacing
- A monospace font for the editor; a clean UI sans for chrome. A small type scale (12/13/14/18/24) used consistently.
- One spacing scale (4/8/12/16/24); consistent border-radius (6–8px) and one shadow token.

## Theme switching
- A theme toggle in the status bar or settings that flips `data-theme` (or the `dark` class); persist the choice.

## CRITICAL: apply the theme BEFORE first paint (avoid the white-flash/stick bug)
Set the theme class/attribute on `<html>` SYNCHRONOUSLY before React renders — in an inline `<script>` in index.html or at the top of main.jsx/main.tsx — reading the saved value (default dark for an editor). Do NOT apply it only inside a React `useEffect`: effects run AFTER the first paint, so the chrome (Tailwind `dark:` variants) renders light and Monaco/CodeMirror (which read the class on init) stick to the light theme. Example:
```js
// main.jsx, before ReactDOM.createRoot(...)
(function () {
  let t = 'dark';
  try { const r = localStorage.getItem('app-theme'); if (r !== null) t = JSON.parse(r); } catch {}
  document.documentElement.classList.toggle('dark', t !== 'light');
})();
```
Also sync the code-editor's own theme to the app theme (Monaco `vs-dark`/custom dark when `dark` is set), not just the surrounding CSS.

## Avoid the AI look
- No giant centered hero, no purple-on-white gradients, no inconsistent paddings. Tight, dense, professional chrome like VS Code / BBEdit / a pro editor.
