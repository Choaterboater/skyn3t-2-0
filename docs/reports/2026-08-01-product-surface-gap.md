# Product-surface gap: why SkyN3t still loses non-technical users

_Date: 2026-08-01. Status: decision document — no code change implied.
Source: the adversarial swarm's Q2 analysis (2026-07-31), verified against
`skyn3t/cli/main.py`, `skyn3t/web/`, and the delivery flow._

## The question

The stated goal is "the best app builder." The de-slop arc (July 31 – Aug 1)
closed the *output-quality* gap: generated apps now get brief-derived design
systems, the golden-design suite is 5/5 live, and zero builds trip the AI-look
detectors. But for a **non-technical user** — the audience v0, Lovable, Bolt,
Replit, and Spark actually sell to — output quality is not the bottleneck.
Five structural gaps remain, in the order a new user hits them.

## The five gaps

1. **Step 1 is a developer environment.** Python 3.11 venv,
   `pip install -e ".[dev]"`, then a *signed-in* codex/claude/kimi CLI or an
   OpenRouter key (`README.md:113-146`, `config/settings.py:211-226`).
   v0/Lovable: open a browser tab. The target user is eliminated before the
   first brief.
2. **The output is a folder, not a URL.** Delivery is `Projects/<slug>/`.
   Deploy is a *plan*; `--now` requires the user's own Cloudflare/Vercel/Fly
   credentials (`README.md:71-80`). The competitors hand you a live,
   shareable URL by default — for this audience, that *is* the product.
3. **Iteration is a CLI re-verification, not a chat.**
   `skyn3t studio improve` runs audit → edit → verify → deliver and exits
   non-zero on failure (`cli/main.py:1852-1873`). No in-browser
   "make the header bigger" loop with instant visual refresh. The dashboard
   has the cockpit/serve pieces, but the fast loop is not the product's
   center of gravity.
4. **Minutes to first pixels.** An 11-stage pipeline, the MoA council awaited
   inline (up to ~2 min), npm installs, proof-runs, gates, optional best-of-N.
   v0 shows a draft in ~30 seconds. Our moat (deterministic verification)
   is paid for in latency exactly where the audience is most impatient.
5. **Host-environment roulette.** Previews and gates depend on
   Docker/Playwright/npm on the machine; the last three commits before this
   arc (`f485e33`, `8fe6c1f`, `1f590e7`) are literally patches for preview
   dependency prep, empty serve logs, and ACL-broken deliveries. When it
   fails, the user gets a manifest of gate findings — not guidance a
   non-dev can act on.

## What is genuinely ours (do not trade away)

- **Verification is the moat.** No competitor has deterministic proof-runs,
  blocking gates, evidence-bound go/no_go, or a measured design bench.
  Their "verification" is a human looking at a preview.
- **Offline/$0 posture.** Every competitor feature is a metered cloud call.
- **Design determinism.** As of this week, tokens/fonts/archetypes are
  derived, not model-invented; competitors still rely on model taste or
  paid design-system tiers.

## Options

**A. Stay a power tool (recommended for now).** The audience is technical
operators who want proof, not pixels. Invest in what no one else can copy:
the gate ladder, the benches, the repair machinery. Close gap 4 at the
margin (frontend-first fast lane from the research wave: UI stack +
design tokens first, backend proof later) and gap 5 incrementally.
Cost: small. Risk: none to the moat.

**B. Hosted-delivery wedge.** Attack gap 2 directly: one-command managed
preview (e.g. a `skyn3t serve --share` tunnel or a bundled single-host
deploy target), so delivery produces a URL, not just a folder. This is the
highest-leverage single gap for shareability and does not require becoming
a SaaS. Cost: medium (tunnel infra or one blessed deploy target + token
management). Risk: low-moderate (new operational surface).
_Status: wedge shipped (cloudflared/localhost.run), see `skyn3t studio share`._

**C. Guided product track.** Gaps 1+3+4: a one-click installer, a
chat-first iteration loop in the dashboard, and the frontend-first fast
lane as the DEFAULT for web briefs. This is chasing v0/Lovable head-on.
Cost: large (quarters, not weeks). Risk: high — it competes with the moat
for attention and complexity is already the codebase's biggest risk
(the same adversarial review recommends freezing Cortex, not adding
product surfaces).

## Recommendation

A now, B next, C only if B proves demand. The de-slop arc just made the
*output* best-in-class for its actual audience; the next quality-of-life
win for that audience is a shareable URL, not a new user segment.

## Metrics that would prove the direction

- Golden-design trend over `--repeats 3` (quality does not regress as
  surface features land).
- Time-to-first-preview per web build (gap 4) — currently minutes;
  the frontend-first lane should cut it to under a minute.
- Share-URL usage if/when B ships (is anyone actually sending links?).
