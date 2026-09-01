# SkyN3t First Run

This guide is for the first time you run SkyN3t locally.

## Start The Foundry

```bash
# From a source checkout, install the optional dashboard dependencies first.
pip install -e ".[web]"
python -m skyn3t.cli.main start --web
```

Open the printed local URL (normally `http://127.0.0.1:6660`). Confirm `/` loads
the Foundry SPA — or the intentional fallback status page when `ui/dist` is absent
— and confirm `/api/status` responds. The dashboard works with the offline `stub`
backend, so no API key is required for a smoke test.

## Check Settings

Go to **Settings** in the left navigation.

- **Backend**: leave `auto` for normal use; it tries signed-in local CLIs in the
  default `codex,kimi` order. Pick `stub` for fully offline dry runs.
- **Keys**: a signed-in local CLI is the keyless real-generation path. Add an
  OpenRouter key only when you explicitly select OpenRouter or enable its
  `auto` fallback; a key alone is not consent to spend.
- **Claude**: `no_claude` is on by default. Disable it only when you intend to
  select Claude in the backend, codegen route, priority chain, or MoA slots.
- **Images**: add a Replicate token only if you want paid generated imagery; web and game builds have offline asset floors.
- **Gates**: leave verification gates on while evaluating build quality.
- **Runtime**: confirm project, data, and log directories match your local machine.

## Lab, Gates, and Security Evidence

The default `lab` build posture records and scores heuristic, policy, and
environment-dependent findings without treating them as delivery blockers.
`release` makes applicable completed gate findings blocking; a probe that cannot
run because a local prerequisite is absent remains a recorded skip.

`lab_autonomy` is off by default. When enabled for a personal lab it removes
routine local build approvals and budget guards, but proof still runs and remote
deploys, secret writes, destructive host actions, releases, and protected-branch
merges remain explicitly gated.

The web security check reports only `ok`, `skipped`, `issues`, `warnings`, and
`checked`. It does not return or execute arbitrary actions. The runner may
perform one conservative repair for simple literal secrets, rechecks afterward,
and records the result under `manifest.extra.security_secret_rewrite`; `eval` /
`Function` findings and SQL-interpolation findings remain findings for the build
to fix rather than being rewritten automatically.

## Build One App

Go to **Foundry**, enter a short brief, and run a build. A good first brief is:

```text
A small client portal with projects, messages, invoices, login, and admin settings.
```

When the build finishes, open **Projects** to inspect files, proof results, cost, and deploy planning.

### Build Profiles

- **Fast** builds one complete candidate and, for sufficiently large file plans, uses concurrent
  frontend/backend/test specialists in isolated worktrees. It does not shorten active generation;
  **Full app** still keeps its full content and asset scope.
- **Balanced** spends additional verification effort without paid asset generation by default.
- **Best quality** keeps best-of-N selection, richer configured assets, and visual repair.

Settings > Runtime shows `parallel_code_slices` and its file-count threshold. A Fast build can
enable the same specialist mode for that build even when the global setting is off.

### Failed Preview Recovery

A failed build can retain a substantial `.preview` tree for inspection. That tree is not a
delivered project and should not be renamed or copied over the project root: downstream
contract, reviewer, and final proof gates may not have run. Older failed manifests may also
lack the architect's complete planned-file contract, so SkyN3t cannot safely determine what is
still missing. After fixing the cause (for example a daily token budget), use the build's replay
or rebuild-variant action. The fresh run preserves the original brief/profile while executing
the complete verification lifecycle.

## Useful Commands

```bash
python -m skyn3t.cli.main doctor
python -m skyn3t.cli.main studio build "a task tracker with due dates"
python -m skyn3t.cli.main studio liveness <project> --require-visual
python -m pytest -q
```

The liveness command writes desktop/mobile browser evidence as described in
[Responsive Visual Proof](RESPONSIVE_VISUAL_PROOF.md).

## Offline Defaults

SkyN3t is designed to start without cloud credentials. Missing keys degrade to deterministic local behavior rather than crashing. A signed-in local CLI enables real generation without an API key; add hosted keys later only when you explicitly want OpenRouter, paid imagery, or remote deploys.
