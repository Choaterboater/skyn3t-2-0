# SkyN3t — Where This Goes Next

A north-star for making the whole factory (all app types — not games, which are a
small corner) better and more future-facing. The organizing idea: **reliability
becomes a measured number (a per-app-type GO-rate), and that number becomes both
the thing we optimize and the gate that lets autonomy off its leash.**

> A brief goes in; a **provably working** app comes out; and every night the
> factory gets measurably better at doing that — on its own.

## Two themes already in motion

1. **A self-improving reliability flywheel** — wire the existing `studio/bench.py`
   → Cortex auto-gating (`cortex/bootstrap.py`) so a proposed prompt/skill/router
   change is kept only when a background bench run *measurably* raises the GO-rate,
   and auto-reverted on regression. The measurement primitives for this are what
   this PR adds (`bench.summarize_by_stack`, carried in every serialized run).
2. **Make the live build legible** — the backend already emits a rich
   per-stage / per-agent / cost / score / debug / files event stream; the web SPA
   renders a *hardcoded* stage rail and dumps the rest as raw JSON. The fastest
   wins surface data that is already on the wire: which agent runs each stage, the
   *real* stage plan (`BUILD_STARTED.payload.stages`), and per-stage cost/score/gaps.

## Ranked leverage list (Fable review)

A recurring insight: much of the "future" is **already built but unwired** —
several top items are "activate the dormant organ," not "grow a new one."

1. **Products, not builds (L).** The factory as *maintainer*: per-project backlogs,
   chained goal-driven improve sessions, regression seals so v7 provably never
   breaks what v1 proved. Builds on `studio/improve.py`, `studio/manifest.py`,
   `memory/store.py`, `cortex/proposal_store.py`.
2. **Brief-fit verdicts (M).** Upgrade the invariant from "delivered != empty" to
   "delivered == what you meant": `test_author` derives executable acceptance
   checks from the clarified brief and blends brief-fit into go/no_go. Builds on
   `studio/intent_score.py`, `agents/test_author.py`, `studio/proof_run.py`.
3. **Bench = the factory's exam (S).** Extend `DEFAULT_CASES` to every registry
   stack (rag/workflow/mcp/agent_pack/swift/cli-copilot are invisible today) and
   auto-append every failed real build as a permanent regression case. Builds on
   `studio/bench.py`, `core/stacks.py`.
4. **Activate divergent fan-out (S–M).** `studio/fanout.py` already runs N
   divergent candidates refereed by proof — surface it as `--fanout` and feed
   referee decisions into `intelligence/model_tournament.py`.
5. **Sliced parallel codegen (M).** `studio/slicer.py` already decomposes the
   manifest into per-slice tiers; wire the orchestrator-worker path so builds run
   scoped sub-agents concurrently — attacks wall-clock for every stack at once.
6. **Overnight fleet (M).** Give the Cortex loop a brief queue and let it run
   fan-out builds all night inside existing caps → wake to a scored portfolio.
   Builds on `cortex/autonomous_loop.py`, `integrations/scheduler.py`.
7. **Ship it (M).** `studio/deploy.py` already emits a keyless deploy plan +
   Dockerfile; ship the token-gated *execution* slice + post-deploy liveness so
   the terminal gate becomes a live URL.
8. **Portfolio RAG (S–M).** Index every delivered project (tree, manifest, proof,
   repair history) and retrieve "how we last built a working X" into stage prompts.
   Builds on `rag/rag_engine.py`, `memory/ingestor.py`, `intelligence/build_patterns.py`.
9. **Cost–quality Pareto routing (M).** Join tournament wins with CostTracker spend
   so the learned router picks the cheapest (tier, task-type) that bench proves holds
   the GO-rate — the empirical, provider-agnostic answer to "which model where."
10. **Inbound channels (S–M).** Wire the existing `integrations/` Slack/Discord/
    Telegram/GitHub connectors as first-class brief sources + delivery notifiers.
11. **Counterfactual trajectory forks (M–L).** The event-sourced spine already
    supports replay; add "fork from stage N with a changed prompt/route, re-run,
    diff outcomes" — the time-travel viewer becomes a built-in A/B lab.
12. **Self-extending factory (L).** Let `cortex/repo_scout.py` + proposal store
    draft new scaffold variants / check-gates that merge only on a bench GO-rate
    lift — the factory extending the factory, using rails that already exist.

*Ideas 3/9/12 are consumers of theme 1; idea 1's natural UI is theme 2 grown into
a per-product workspace.*
