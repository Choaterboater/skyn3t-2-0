"""Tests for the deterministic RAG-app gate (``skyn3t/studio/rag_check.py``).

The gate speaks plain HTTP, not any framework — so these fixtures are stdlib
``http.server`` programs (zero third-party deps), mirroring how test_mcp_check
uses SDK-free JSON-RPC fixture servers. A verifier gets the adversarial
treatment (memory: a false PASS in a gate is worse than a bug): every
degrade-open path is pinned as SKIP (no gaps), and every real-defect path is
pinned as an ISSUE (gaps feed the fix-loop).

The one live integration test boots the REAL rag scaffold under fastapi/uvicorn
(skipped when they aren't importable) — proving the scaffold and the gate agree
on the contract end to end.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from skyn3t.studio.rag_check import RagVerdict, check_rag

# ---------------------------------------------------------------------------
# fixture servers (stdlib only — the gate probes HTTP, not FastAPI)
# ---------------------------------------------------------------------------

# A well-behaved RAG server: naive token-overlap retrieval over an in-memory
# list, the full route contract, input validation on /ingest.
_GOOD_SERVER = '''
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

DOCS = ["seed document about nothing in particular"]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"status": "ok"})
        if url.path == "/v1/stats":
            return self._send(200, {"documents": len(DOCS), "chunks": len(DOCS)})
        if url.path == "/query":
            q = (parse_qs(url.query).get("q") or [""])[0].lower()
            hits = [d for d in DOCS if any(t in d.lower() for t in q.split())]
            results = [{"text": d, "source": "inline", "score": 1.0} for d in hits]
            return self._send(200, {"query": q, "results": results})
        if url.path == "/chat/stream":
            q = (parse_qs(url.query).get("question") or [""])[0].lower()
            hits = [d for d in DOCS if any(t in d.lower() for t in q.split())]
            answer = hits[0] if hits else "nothing relevant found"
            frames = "".join("data: " + w + "\\n\\n" for w in answer.split())
            body = (frames + "data: [DONE]\\n\\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        if self.path == "/ingest":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                return self._send(422, {"detail": "text is required"})
            DOCS.append(text)
            return self._send(200, {"ingested_chunks": 1, "chunks": len(DOCS)})
        if self.path == "/chat":
            q = str(payload.get("question") or "").lower()
            hits = [d for d in DOCS if any(t in d.lower() for t in q.split())]
            answer = hits[0] if hits else "nothing relevant found"
            return self._send(200, {"answer": answer, "sources": [], "llm_used": False})
        return self._send(404, {"detail": "not found"})

HTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
'''

# Retrieval is broken: every /query returns an empty results list.
_EMPTY_RETRIEVAL_SERVER = _GOOD_SERVER.replace(
    'return self._send(200, {"query": q, "results": results})',
    'return self._send(200, {"query": q, "results": []})',
)

# The good server's extractive /chat block (must match _GOOD_SERVER verbatim —
# the seam-variant fixtures below are built by swapping it out).
_CHAT_EXTRACTIVE_BLOCK = '''            answer = hits[0] if hits else "nothing relevant found"
            return self._send(200, {"answer": answer, "sources": [], "llm_used": False})'''

# A properly seam-wired /chat: when OPENAI_BASE_URL is set, POST the retrieved
# context + question to {base}/chat/completions and surface the completion.
_CHAT_SEAM_BLOCK = '''            base = os.environ.get("OPENAI_BASE_URL", "")
            if base:
                import urllib.request
                req = urllib.request.Request(
                    base + "/chat/completions",
                    data=json.dumps({"model": "m", "messages": [
                        {"role": "user",
                         "content": "Context: " + " ".join(hits) + " Question: " + q},
                    ]}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                answer = data["choices"][0]["message"]["content"]
                return self._send(200, {"answer": answer, "sources": [], "llm_used": True})
            answer = hits[0] if hits else "nothing relevant found"
            return self._send(200, {"answer": answer, "sources": [], "llm_used": False})'''

_LLM_WIRED_SERVER = _GOOD_SERVER.replace(_CHAT_EXTRACTIVE_BLOCK, _CHAT_SEAM_BLOCK)

# Defect: calls the seam but sends ONLY the question — retrieval never feeds
# generation (the context is dropped on the floor).
_LLM_NO_CONTEXT_SERVER = _GOOD_SERVER.replace(
    _CHAT_EXTRACTIVE_BLOCK,
    _CHAT_SEAM_BLOCK.replace(
        '"Context: " + " ".join(hits) + " Question: " + q', '"Question: " + q'),
)

# Defect: no SSE endpoint at all (/chat/stream 404s).
_NO_STREAM_SERVER = _GOOD_SERVER.replace(
    'url.path == "/chat/stream"', 'url.path == "/chat/stream-disabled"')

# Defect: calls the seam (with context) but discards the completion for a
# canned string.
_LLM_DISCARD_SERVER = _GOOD_SERVER.replace(
    _CHAT_EXTRACTIVE_BLOCK,
    _CHAT_SEAM_BLOCK.replace(
        'answer = data["choices"][0]["message"]["content"]',
        'answer = "a canned reply " + str(len(data))'),
)

# Input validation is broken: a missing "text" 500s instead of 4xx-ing.
_CRASHY_INGEST_SERVER = _GOOD_SERVER.replace(
    'return self._send(422, {"detail": "text is required"})',
    'return self._send(500, {"detail": "unhandled"})',
)

_DEP_MISSING_MAIN = 'raise ModuleNotFoundError("No module named \'fastapi\'")\n'

_BOOT_CRASH_MAIN = (
    "import sys\n"
    'print("Traceback (most recent call last):", file=sys.stderr)\n'
    'print("RuntimeError: config file is missing", file=sys.stderr)\n'
    "sys.exit(3)\n"
)

_HANGING_MAIN = "import time\ntime.sleep(120)\n"


def _project(tmp_path, main_code: str):
    (tmp_path / "main.py").write_text(main_code, encoding="utf-8")
    return tmp_path


def _fastapi_stack_available() -> bool:
    return all(
        importlib.util.find_spec(mod) is not None for mod in ("fastapi", "uvicorn")
    )


# ---------------------------------------------------------------------------
# verdict shape
# ---------------------------------------------------------------------------


def test_verdict_ok_and_gaps_contract():
    ok = RagVerdict()
    assert ok.ok and ok.gaps() == []
    skipped = RagVerdict(skipped=True, reason="nope")
    assert not skipped.ok
    assert skipped.gaps() == []  # a could-not-run gate must never false-flag
    bad = RagVerdict(issues=["/query returned nothing"])
    assert not bad.ok
    assert bad.gaps() == ["/query returned nothing"]
    d = bad.to_dict()
    assert d["ok"] is False and d["gaps"] == ["/query returned nothing"]


# ---------------------------------------------------------------------------
# degrade-open skips (no gaps, never false-flag)
# ---------------------------------------------------------------------------


def test_skips_non_rag_stack(tmp_path):
    v = check_rag(_project(tmp_path, _GOOD_SERVER), stack="fastapi")
    assert v.skipped and v.gaps() == []
    assert "not a rag stack" in v.reason


def test_skips_missing_project_dir(tmp_path):
    v = check_rag(tmp_path / "nope", stack="rag")
    assert v.skipped and v.gaps() == []


def test_skips_missing_main_py(tmp_path):
    v = check_rag(tmp_path, stack="rag")
    assert v.skipped and v.gaps() == []
    assert "main.py" in v.reason


def test_missing_third_party_dep_on_boot_soft_skips(tmp_path):
    v = check_rag(_project(tmp_path, _DEP_MISSING_MAIN), stack="rag",
                  boot_timeout=10.0)
    assert v.skipped, v.to_dict()
    assert v.gaps() == []
    assert "deps" in v.reason


def test_unimportable_local_module_is_an_issue_not_a_skip(tmp_path):
    # When the "missing" module exists IN the delivery, the boot failure is the
    # app's own wiring (circular import, bad path) — a real defect, never a
    # missing-pip-install degrade.
    _project(tmp_path, 'raise ModuleNotFoundError("No module named \'rag_core\'")\n')
    (tmp_path / "rag_core.py").write_text("x = 1\n", encoding="utf-8")
    v = check_rag(tmp_path, stack="rag", boot_timeout=10.0)
    assert not v.skipped, v.to_dict()
    assert any("rag_core" in issue and "import" in issue for issue in v.issues), v.issues


# ---------------------------------------------------------------------------
# real defects are issues (gaps feed the fix-loop)
# ---------------------------------------------------------------------------


def test_good_stdlib_server_passes(tmp_path):
    v = check_rag(_project(tmp_path, _GOOD_SERVER), stack="rag")
    assert v.ok, v.to_dict()
    assert v.checked["query"] == "marker_retrieved"
    assert v.checked["chat"] == "ok"
    assert 400 <= v.checked["malformed_ingest_status"] < 500
    # A pure-extractive /chat never calls the seam — compliant, recorded only.
    assert v.checked["llm_seam"] == "ok_extractive"
    assert v.checked["llm_called"] is False
    # The SSE tier: frames + terminator + grounded content.
    assert v.checked["chat_stream"] == "ok"
    assert v.checked["chat_stream_grounded"] is True


def test_missing_sse_endpoint_is_an_issue(tmp_path):
    v = check_rag(_project(tmp_path, _NO_STREAM_SERVER), stack="rag")
    assert not v.skipped
    assert v.checked["chat_stream"] == "failed"
    assert any("/chat/stream" in issue for issue in v.issues), v.issues


def test_seam_wired_server_proves_retrieval_feeds_generation(tmp_path):
    v = check_rag(_project(tmp_path, _LLM_WIRED_SERVER), stack="rag")
    assert v.ok, v.to_dict()
    assert v.checked["llm_called"] is True
    assert v.checked["llm_seam"] == "ok"


def test_llm_call_without_retrieved_context_is_an_issue(tmp_path):
    v = check_rag(_project(tmp_path, _LLM_NO_CONTEXT_SERVER), stack="rag")
    assert not v.skipped
    assert v.checked["llm_called"] is True
    assert any("retrieval feeds generation" in issue for issue in v.issues), v.issues


def test_discarded_completion_is_an_issue(tmp_path):
    v = check_rag(_project(tmp_path, _LLM_DISCARD_SERVER), stack="rag")
    assert not v.skipped
    assert v.checked["llm_called"] is True
    assert any("never surfaced" in issue for issue in v.issues), v.issues
    # The prompt DID carry the context, so that half of the contract held.
    assert not any("retrieval feeds generation" in issue for issue in v.issues)


def test_seam_fixtures_are_real_variants():
    # The .replace()-built fixtures silently no-op if _GOOD_SERVER's chat block
    # drifts — pin that they genuinely differ (the phaser keyword-drift lesson).
    assert _LLM_WIRED_SERVER != _GOOD_SERVER
    assert _LLM_NO_CONTEXT_SERVER != _GOOD_SERVER
    assert _LLM_DISCARD_SERVER != _GOOD_SERVER


def test_boot_crash_is_an_issue_not_a_skip(tmp_path):
    v = check_rag(_project(tmp_path, _BOOT_CRASH_MAIN), stack="rag",
                  boot_timeout=10.0)
    assert not v.skipped
    assert v.issues and "crashed on boot" in v.issues[0]
    assert "config file is missing" in v.issues[0]  # the stderr tail is actionable


def test_server_that_never_binds_times_out_as_an_issue(tmp_path):
    v = check_rag(_project(tmp_path, _HANGING_MAIN), stack="rag",
                  boot_timeout=2.0, http_timeout=1.0)
    assert not v.skipped
    assert v.issues and "/health" in v.issues[0]
    assert "PORT" in v.issues[0]  # the gap teaches the port contract


def test_empty_retrieval_is_an_issue(tmp_path):
    v = check_rag(_project(tmp_path, _EMPTY_RETRIEVAL_SERVER), stack="rag")
    assert not v.skipped
    assert v.checked["query"] == "marker_not_found"
    assert any("JUST ingested" in issue for issue in v.issues), v.issues


def test_malformed_ingest_500_is_an_issue(tmp_path):
    v = check_rag(_project(tmp_path, _CRASHY_INGEST_SERVER), stack="rag")
    assert not v.skipped
    assert v.checked["malformed_ingest_status"] == 500
    assert any("4xx" in issue for issue in v.issues), v.issues
    # ...and it is the ONLY defect: the rest of the contract held.
    assert v.checked["query"] == "marker_retrieved"


@pytest.mark.skipif(
    not _fastapi_stack_available(),
    reason="fastapi/uvicorn not importable in this env",
)
def test_gate_does_not_pollute_a_memory_chat_delivery(tmp_path):
    # The §3.10 variant journals chat turns to MEMORY_FILE — the gate's own
    # probes must not ship fixture memories inside the delivered app (the
    # proof-must-not-pollute rule; the spawn env points the journal at devnull).
    from skyn3t.agents._scaffold import scaffold_for

    files = scaffold_for("rag", "membot", "a chatbot that remembers me")
    assert "memory_store.py" in files  # the variant, not the base
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    v = check_rag(tmp_path, stack="rag", python_exec=sys.executable)
    assert v.ok, v.to_dict()
    assert not (tmp_path / "memory.jsonl").exists(), (
        "the gate's fixture questions leaked into the delivered app's memory")


def test_check_rag_never_raises_on_garbage_input():
    v = check_rag(12345, stack="rag")  # type: ignore[arg-type]
    assert v.skipped and v.gaps() == []


# ---------------------------------------------------------------------------
# live integration: the REAL scaffold under fastapi/uvicorn
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _fastapi_stack_available(),
    reason="fastapi/uvicorn not importable in this env",
)
def test_real_rag_scaffold_passes_the_gate(tmp_path):
    from skyn3t.agents._scaffold import scaffold_for

    files = scaffold_for("rag", "docs-chat", "chat with my documents")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents, encoding="utf-8")
    v = check_rag(tmp_path, stack="rag", python_exec=sys.executable)
    assert not v.skipped, v.to_dict()
    assert v.ok, v.to_dict()
    assert v.checked["query"] == "marker_retrieved"
    # The SSE tier streams the grounded answer and terminates.
    assert v.checked["chat_stream"] == "ok", v.to_dict()
    assert v.checked["chat_stream_grounded"] is True, v.to_dict()
    # Phase 2: the scaffold's OPENAI_BASE_URL seam is genuinely wired — it
    # calls the mock, feeds it the retrieved context, and surfaces the reply.
    assert v.checked["llm_called"] is True, v.to_dict()
    assert v.checked["llm_seam"] == "ok", v.to_dict()
