"""Regenerate docs/FILE_MAP.md from the current repo tree.

This is a lightweight maintenance helper for the docs-first workflow. It uses
SkyN3t's repo-map index so the map stays aligned with the codebase instead of
going stale after a few refactors.
"""

from __future__ import annotations

from pathlib import Path

from skyn3t.rag.repo_map import RepoMapIndex

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "FILE_MAP.md"


def main() -> None:
    index = RepoMapIndex(str(ROOT))
    index.scan()

    lines = [
        "# File Map",
        "",
        "Use this when you need the fastest path to the important code.",
        "",
        "| Area | Open these files |",
        "| --- | --- |",
    ]
    sections = [
        ("CLI entrypoint", "skyn3t/cli/main.py"),
        ("Core events + routing", "skyn3t/core/events.py, skyn3t/core/orchestrator.py, skyn3t/core/agent.py, skyn3t/core/model_router.py"),
        ("Studio pipeline", "skyn3t/studio/runner.py, skyn3t/studio/planner.py, skyn3t/studio/stages.py, skyn3t/studio/proof_run.py, skyn3t/studio/best_of_n.py"),
        ("Stack / app choice", "skyn3t/studio/stack_selector.py, skyn3t/agents/config_detector.py"),
        ("Config UI / env overrides", "skyn3t/agents/config_ui_agent.py, skyn3t/studio/config_spec.py"),
        ("Memory / lessons", "skyn3t/memory/store.py, skyn3t/memory/ingestor.py"),
        ("Persistence / checkpoints", "skyn3t/persistence/checkpoint.py"),
        ("Web control plane", "skyn3t/web/app.py, skyn3t/web/routes.py, skyn3t/web/ui/"),
        ("RAG / corpus", "skyn3t/rag/"),
        ("Cortex / autonomous loop", "skyn3t/cortex/"),
        ("Game work", "docs/game-capability-roadmap.md, skyn3t/agents/"),
        ("Visual / repair loops", "skyn3t/studio/game_visual_loop.py, skyn3t/studio/game_visual_check.py"),
        ("Repo map refresh", "scripts/refresh_file_map.py, skyn3t/rag/repo_map.py"),
        ("Docs hub", "docs/INDEX.md, docs/START_HERE.md, docs/WORKFLOW.md, docs/ARCHITECTURE.md, docs/ROADMAP.md, STATUS.md"),
    ]
    for area, files in sections:
        lines.append(f"| {area} | `{files}` |")

    lines += [
        "",
        "## Naming rule",
        "",
        "Prefer one owner per behavior. If a file starts to cover two jobs, split it and update `STATUS.md` so the next person knows where the new boundary lives.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
