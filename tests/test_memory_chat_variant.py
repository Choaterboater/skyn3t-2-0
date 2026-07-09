"""Memory-augmented chat variant of the rag stack (wave-2 §3.10).

A "remembers me" brief routes to the rag stack (new phrase keywords) and gets
the persistent-memory scaffold: every chat turn is journaled to an append-only
JSONL file and replayed into the retrieval index at boot — facts stated in
conversation survive a RESTART and feed later answers. A template variant on
rag (the §3.9 precedent): same routes, same gate contract, zero new stacks.
The live test here IS the spec's proof: state a fact → kill the process →
boot a fresh one on the same directory → the fact is retrievable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

from skyn3t.agents._common import detect_stack as agent_detect_stack
from skyn3t.agents._scaffold import _implies_memory_chat, scaffold_for
from skyn3t.studio.planner import detect_stack as plan_detect_stack


def test_memory_briefs_route_to_rag():
    for brief in (
        "a personal assistant with memory",
        "a chatbot that remembers me across sessions",
        "a stateful chat app for my team",
    ):
        assert plan_detect_stack(brief) == "rag", brief
        assert agent_detect_stack(brief=brief) == "rag", brief
        assert _implies_memory_chat(brief), brief


def test_bare_memory_briefs_are_not_stolen():
    for brief in ("a memory game for kids", "a memory profiler for python"):
        assert plan_detect_stack(brief) != "rag", brief
        assert not _implies_memory_chat(brief), brief


def test_memory_brief_gets_the_journal_scaffold():
    files = scaffold_for("rag", "membot", "a personal assistant with memory")
    assert "memory_store.py" in files and "test_memory_store.py" in files
    main = files["main.py"]
    # Every pinned replacement in the variant builder actually took (the
    # .replace-derivation drift guard — the fixture-variant lesson).
    assert "MEMORY_FILE" in main
    assert "memory_store.replay(MEMORY_FILE)" in main
    assert '"source": "chat-memory"' in main
    assert 'stats["memories"]' in main
    assert main.count("memory_store.append") == 2  # ingest + chat turn
    # The base rag contract is fully preserved.
    for route in ("/ingest", "/query", "/chat", "/chat/stream", "/health", "/v1/stats"):
        assert route in main, f"variant lost the {route} route"


def test_plain_rag_brief_is_unchanged():
    files = scaffold_for("rag", "docs", "a rag app to chat with my documents")
    assert "memory_store.py" not in files


def test_memory_scaffold_own_proof_passes_with_zero_deps():
    # The shipped persistence tests (append/replay/corrupt-line/restart) are
    # PURE — they must pass standalone with no third-party installs.
    import tempfile
    from pathlib import Path

    files = scaffold_for("rag", "membot", "a chatbot that remembers me")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for rel, contents in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(contents, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "test_memory_store.py", "test_rag_core.py"],
            cwd=str(root), capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, (
        f"memory scaffold proof failed:\n{proc.stdout}\n{proc.stderr}")


def _fastapi_stack_available() -> bool:
    return all(
        importlib.util.find_spec(mod) is not None for mod in ("fastapi", "uvicorn")
    )


@pytest.mark.skipif(
    not _fastapi_stack_available(),
    reason="fastapi/uvicorn not importable in this env",
)
@pytest.mark.requires_loopback
def test_facts_survive_a_real_process_restart(tmp_path):
    # The spec's §3.10 proof, live: turn 1 states a fact, the PROCESS is
    # killed, a fresh process on the same directory answers turn 2 from the
    # replayed journal — persistence + retrieval proven mechanically.
    files = scaffold_for("rag", "membot", "a chatbot that remembers me")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")

    def boot(port: int) -> subprocess.Popen:
        env = {**os.environ, "PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("OPENAI_BASE_URL", None)
        proc = subprocess.Popen(
            [sys.executable, "-B", "main.py"], cwd=str(tmp_path), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return proc
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.25)
        proc.terminate()
        raise AssertionError("server did not boot")

    def chat(port: int, question: str) -> str:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/chat",
            data=json.dumps({"question": question}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["answer"]

    proc1 = boot(8971)
    try:
        chat(8971, "please remember that my name is X-MARKER-73")
    finally:
        proc1.terminate()
        proc1.wait(timeout=5)

    proc2 = boot(8972)  # a NEW process, same directory, same journal
    try:
        answer = chat(8972, "what is my name")
        assert "X-MARKER-73" in answer, answer
    finally:
        proc2.terminate()
        proc2.wait(timeout=5)
