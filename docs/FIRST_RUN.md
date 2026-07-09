# SkyN3t First Run

This guide is for the first time you run SkyN3t locally.

## Start The Foundry

```bash
python -m skyn3t.cli.main start --web
```

Open the printed local URL. The dashboard works with the offline `stub` backend, so no API key is required for a smoke test.

## Check Settings

Go to **Settings** in the left navigation.

- **Backend**: leave `auto` for normal use, or pick `stub` for fully offline dry runs.
- **Keys**: add an OpenRouter key when you want real model generation.
- **Images**: add a Replicate token only if you want paid generated imagery; web and game builds have offline asset floors.
- **Gates**: leave verification gates on while evaluating build quality.
- **Runtime**: confirm project, data, and log directories match your local machine.

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

## Useful Commands

```bash
python -m skyn3t.cli.main doctor
python -m skyn3t.cli.main studio build "a task tracker with due dates"
python -m pytest -q
```

## Offline Defaults

SkyN3t is designed to start without cloud credentials. Missing keys degrade to deterministic local behavior rather than crashing. Add keys later when you want higher quality generations, live model routing, image generation, or remote deploys.
