# File Map

Use this when you need the fastest path to the important code.

| Area | Open these files |
| --- | --- |
| CLI entrypoint | `skyn3t/cli/main.py` |
| Core events + routing | `skyn3t/core/events.py, skyn3t/core/orchestrator.py, skyn3t/core/agent.py, skyn3t/core/model_router.py` |
| Studio pipeline | `skyn3t/studio/runner.py, skyn3t/studio/planner.py, skyn3t/studio/stages.py, skyn3t/studio/proof_run.py, skyn3t/studio/best_of_n.py` |
| Stack / app choice | `skyn3t/studio/stack_selector.py, skyn3t/agents/config_detector.py` |
| Config UI / env overrides | `skyn3t/agents/config_ui_agent.py, skyn3t/studio/config_spec.py` |
| Memory / lessons | `skyn3t/memory/store.py, skyn3t/memory/ingestor.py` |
| Persistence / checkpoints | `skyn3t/persistence/checkpoint.py` |
| Web control plane | `skyn3t/web/app.py, skyn3t/web/routes.py, skyn3t/web/ui/` |
| RAG / corpus | `skyn3t/rag/` |
| Cortex / autonomous loop | `skyn3t/cortex/` |
| Game work | `docs/game-capability-roadmap.md, skyn3t/agents/` |
| Visual / repair loops | `skyn3t/studio/visual_proof.py, skyn3t/studio/visual_check.py, skyn3t/studio/liveness.py, skyn3t/studio/game_visual_loop.py` |
| Repo map refresh | `scripts/refresh_file_map.py, skyn3t/rag/repo_map.py` |
| Docs hub | `docs/INDEX.md, docs/START_HERE.md, docs/WORKFLOW.md, docs/ARCHITECTURE.md, docs/ROADMAP.md, STATUS.md` |

## Naming rule

Prefer one owner per behavior. If a file starts to cover two jobs, split it and update `STATUS.md` so the next person knows where the new boundary lives.
