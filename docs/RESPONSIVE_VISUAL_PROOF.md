# Responsive Visual Proof

The end-of-build liveness path now records objective browser evidence for every
reachable page. It uses one Chromium process per route set and captures two fixed
viewports:

- desktop: `1440x900`
- mobile: `390x844`

Each viewport checks uncaught page errors, console errors, horizontal overflow,
blank or near-empty main content, broken visible images, and conservative
high-confidence element overlaps. A configured vision provider adds subjective
review, but it cannot override a deterministic failure.

When a delivered project contains `.skyn3t/visual-design-contract.json`, the same
proof also verifies the shared visual baseline: required root design tokens,
contract heading family when a visible heading exists, and a 40px minimum size
for buttons and form controls on the mobile viewport. The contract records the
photo policy too: prefer supplied or licensed real assets; generated imagery is
only appropriate when the brief explicitly requests it.

## Run It

Install the optional browser tooling once:

```bash
pip install -e ".[visual]"
playwright install chromium
```

Then audit a delivered project:

```bash
skyn3t studio liveness my-project --require-visual
```

Evidence defaults to `.skyn3t/visual-proof/` in the delivered project. CI can
choose a collection directory:

```bash
skyn3t studio liveness my-project \
  --evidence-dir artifacts/visual-proof \
  --require-visual
```

`visual-proof.json` contains the schema-versioned responsive results and each
route directory contains its screenshots and `report.json`.
`liveness-report.json` combines HTTP and visual status for manifest or CI use.

Missing Playwright or Chromium is recorded as `skipped`, with
`visual_health: null`; it is never serialized as a pass. `--require-visual`
returns exit code 3 for that condition.

Canvas-heavy pages still receive runtime, console, image, and screenshot checks.
Phaser canvases suppress only ambiguous blank-DOM and canvas-only mobile overflow
findings; the dedicated game visual and playtest gates remain authoritative for
gameplay content.
