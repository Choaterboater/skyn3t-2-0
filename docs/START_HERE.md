# Start Here

If you get disconnected or return after a while, read these in order:

1. [`STATUS.md`](../STATUS.md) — what works, what is missing, and the latest state.
2. [`docs/WORKFLOW.md`](WORKFLOW.md) — how to resume and continue work.
3. [`docs/FILE_MAP.md`](FILE_MAP.md) — where the important code lives.
4. [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — the package map and dataflow.
5. [`docs/ROADMAP.md`](ROADMAP.md) — what is done vs planned.
6. [`docs/APP_TYPES.md`](APP_TYPES.md) — which UI/style pattern to use by app type.
7. [`docs/ENGINE_OPTIONS.md`](ENGINE_OPTIONS.md) — which engine fits the job.
8. [`docs/archive/game-capability-roadmap.md`](archive/game-capability-roadmap.md) — game-specific work.

## Session rule

When you finish a task, leave one short note in `STATUS.md` with:

- what changed
- what still needs work
- the next file to open

## Default rule

Prefer inferred defaults plus UI/env overrides. Avoid hardcoding a stack,
theme, or engine unless the project already fixes it.

That makes it much easier to resume without searching the whole tree.
