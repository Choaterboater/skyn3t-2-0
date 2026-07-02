# feat: rag + workflow app types, deterministic HTTP gates, learning-loop seals

> Ready-to-use PR body for branch `worktree-rag-stack` → push as `feat/rag-stack`.
> Written while the GitHub remote was unreachable (`Choaterboater/skyn3t-2-0`
> invisible to the active `gh` login); ship with:
> `git push -u origin worktree-rag-stack:feat/rag-stack && gh pr create --draft -F .github/PULL_REQUEST_DRAFT.md`

Thirteen commits, three complete workstreams, every commit verified by the full
suite (1857 → 1913 tests, all green throughout).

## 1. The `rag` stack — wave-2 §3.1, demand #1 (chat with your documents)

- **Full ten-touchpoint vocabulary** per `docs/ADDING_A_STACK.md` (registry
  `RAG_STACKS` + `rag_check` GateSpec, planner keywords with both-direction
  theft guards, agent vocab, selector menu/classification, proof artifact
  check, codegen directive, settings flag, drift-test family).
- **Fresh scaffold**: FastAPI `main.py` over a PURE stdlib `rag_core.py`
  (chunking + rarity-weighted retrieval — the sim-core split), seeded corpus
  with a planted marker fact, its own pytest suite, zero keys anywhere
  (`/chat` falls back to extractive answers; the LLM seam is `OPENAI_BASE_URL`
  only), a self-contained browser chat page at `/`, and **SSE streaming**
  (`GET /chat/stream`, `data: [DONE]` terminated, EventSource UI).
- **`rag_check`** — a deterministic end-of-build gate that boots the delivered
  app on a free port (LLM seams scrubbed; child pipes drained; every wait
  deadline-bounded) and proves: `/health` → `/v1/stats` → ingest a
  unique-marker doc → `/query` must retrieve it → `/chat` answers with zero
  keys → `/chat/stream` streams and terminates → malformed ingest yields a
  structured 4xx. **Phase 2** re-boots with `OPENAI_BASE_URL` pointed at the
  in-process deterministic mock-LLM and proves *retrieval feeds generation* at
  the protocol boundary: the captured prompt must contain the retrieved marker
  chunk and the completion must surface as the answer (pure-extractive apps
  stay compliant — recorded, never flagged). Advisory: snapshot → one repair →
  re-proof → rollback; never flips the verdict.
- **Acceptance seal**: one whole offline build through `StudioRunner` — an
  UNPINNED brief routes to `rag` (the system chooses), the scaffold delivers,
  verdict `go`, and the gate's full verdict lands in the manifest.

## 2. The `workflow` stack — wave-2 §3.2, demand #2 (agent workflows)

- Same ten-touchpoint treatment. Fresh scaffold: `workflow_core.py` is a PURE
  engine (steps run in order and may only call tools registered in a
  `ToolRegistry` — no fabricated APIs; failing steps retry then land in TYPED
  error envelopes; the engine never raises) under a FastAPI runner
  implementing the spec's exact contract: `POST /trigger` (dry-run **defaults
  true**) → `{run_id, dry_run, status, brief, delivery:{status}}`;
  live-with-no-config → `skipped_no_delivery`, never a crash (delivery is
  optional env config: `WEBHOOK_URL`); append-only `/runs` ledger; `/` runs
  dashboard.
- **`workflow_check`** — boots the delivered runner (WEBHOOK_URL + LLM seams
  scrubbed) and proves the contract end to end, including unknown-workflow
  rejection and ledger growth. Reuses `rag_check`'s hardened plumbing by
  import (shared-module extraction deferred until a third HTTP gate).
- **Acceptance seal** mirroring rag's; `mcp` also gained its missing seal.

## 3. Learning loop — capture, dedupe, seal ("make it learn better")

- **Advisory-gate findings become lessons even on a `go`**: gate verdicts
  (seo/mcp_check/rag_check issues + liveness dead routes) now flow through
  `extract_gate_findings` into `_summarize_outcome` regardless of verdict —
  previously a passing build's caught defect taught the system nothing.
- **Capture-side dedupe** (`MemoryStore.lesson_exists`): recurring findings
  reinforce ONE row's grading history instead of flooding the score-ranked
  injection top-5 with duplicates. Duck-typed stores degrade open.
- **Sealed at every level**: unit tests + a real-build e2e with the production
  wiring (same `MemoryStore` shared by runner and `LearningLoop`).

## 4. Cross-cutting fixes found along the way

- **Liveness fair to API stacks**: `check_liveness` counted 405/422 routes as
  dead — every fastapi/express/rag build was losing health score for POST-only
  and required-param routes a GET probe can never satisfy (a working rag build
  scored 2/6 → a 33% haircut). 405/422 now count as *wired*; 404/5xx/0 stay
  dead.
- **Latent `mcp_check` bug**: the gate spawned `python -I server.py`; since
  3.11, `-I` implies `-P`, dropping the script dir from `sys.path` — the
  scaffold's own `import tools` would crash at boot and file a FALSE "crashed
  on boot" issue whenever the SDK is installed. Both gates now spawn `-B`;
  regression test with a split-file fixture server.
- **Toolchain preflight** (ADDING_A_STACK step 10 closed): `select_stack`
  demotes heuristic/LLM choices whose toolchain is missing (npm/swift) to the
  nearest buildable stack with the reason recorded; pins never demoted.

## Verification

- Full suite green at every commit: 1857 → **1913 passed / 3 skipped**.
- Live verifications beyond tests: both scaffolds booted for real and driven
  over the wire (chat page 200, SSE frames, trigger contract states, ledger);
  the rag gate proven against the real scaffold under fastapi/uvicorn
  including the mock-LLM generation phase — the whole
  ingest → retrieve → generate loop at $0.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
