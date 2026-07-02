"""Deterministic RAG-app gate (wave-2 §3.1) — the proof story for the ``rag``
app type, with ZERO LLM anywhere.

A RAG app is an HTTP server: a user ingests documents, queries them, and chats
over the retrieved context. This gate is that user, mechanically: it boots the
delivered ``main.py`` on a free port and drives the real HTTP contract —

  GET /health  →  GET /v1/stats (the corpus-stats contract)  →  POST /ingest a
  fixture doc carrying a UNIQUE marker fact  →  GET /query must retrieve that
  marker back (retrieval genuinely works, not just routes-exist)  →  POST /chat
  must answer  →  one malformed ingest (no text) must yield a structured 4xx.

Then a SECOND boot with the ``OPENAI_BASE_URL`` seam pointed at the local
deterministic mock-LLM (wave-2 item 42) proves the generation side: /chat must
still answer with a seam configured, and — when the app calls the seam — the
captured request's prompt must CONTAIN the retrieved marker chunk (retrieval
feeds generation, the part codegen actually gets wrong) and the mock's
completion must surface as the answer. An app that answers extractively and
never calls the seam is contract-compliant (recorded, never flagged).

Design contract — mirrors :mod:`skyn3t.studio.mcp_check`:

* **Never hangs a build.** The boot wait and every HTTP read are deadline-bounded,
  and the child's stdout/stderr are drained by background threads (uvicorn logs
  requests to stderr — an undrained pipe would fill and wedge the server under
  the gate's own probes, a self-inflicted false timeout).
* **Never raises.** Any unexpected failure degrades to a soft-skip.
* **Soft-skips (degrade open, no gaps) when it CANNOT run the check** — a non-rag
  stack, no ``main.py``, no Python runtime, or the server crashing on boot with a
  missing third-party module (``No module named 'fastapi'`` — the runtime dep is
  declared in requirements.txt; the proof env just doesn't have it installed,
  exactly the split proof_run's boot check applies).
* **Records ISSUES (ok=False, gaps feed the fix-loop) when it RAN and found a
  real defect** — a boot crash, a dead /health, a missing stats contract, an
  ingest that doesn't grow the corpus, a query that can't retrieve what was just
  ingested, a /chat that errors, or a malformed request answered with a 5xx.
  ADVISORY: the runner records the verdict and feeds gaps to ONE repair, but
  NEVER flips the build's verdict.

The gate speaks plain HTTP (stdlib urllib), not any framework — so any server
that honours the route contract is provable, and the gate's own tests can use
dependency-free stdlib fixture servers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

# Default budgets (seconds). Generous enough for a real uvicorn boot, bounded so
# a wedged server can never hang a build. Overridable (tests pass small values).
_BOOT_TIMEOUT = 25.0
_HTTP_TIMEOUT = 10.0
_POLL_INTERVAL = 0.25

# The planted marker the gate ingests and then must retrieve. The token is
# deliberately unique (never in a seed corpus / README) so a hit PROVES the
# just-ingested document came back, not a lookalike.
_MARKER_TOKEN = "739417"
_MARKER_DOC = (
    "Gate fixture document. The gate fixture constant is 739417. "
    "This document was ingested by the skyn3t rag_check gate to prove that "
    "ingestion and retrieval work end to end."
)
_MARKER_QUERY = "what is the gate fixture constant"

# The delivered server crashed on boot because a third-party module is not
# importable — a degrade-open SKIP signal, not a code defect (deps are declared
# in requirements.txt; the proof env just doesn't have them installed). Local
# unresolved imports are caught upstream by proof_run's import scan; when the
# named module DOES exist in the delivery, _missing_local_module flags it as a
# real defect instead.
_DEP_MISSING_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError)[^\n]*No module named"
    r"|No module named ['\"][A-Za-z_][\w.]*['\"]",
)
_NO_MODULE_NAME_RE = re.compile(r"No module named ['\"]([A-Za-z_][\w.]*)['\"]")


def _missing_local_module(err: str, root: Path) -> str | None:
    """The module name from a boot-time ModuleNotFoundError when that module
    exists IN the delivery (so the failure is the app's own wiring, not a
    missing pip install) — a real defect. ``None`` otherwise (degrade open)."""
    match = _NO_MODULE_NAME_RE.search(err or "")
    if not match:
        return None
    mod = match.group(1).split(".")[0]
    try:
        if (root / f"{mod}.py").is_file() or (root / mod / "__init__.py").is_file():
            return mod
    except OSError:
        return None
    return None


# The gate-agnostic verdict shape, shared by every deterministic gate since the
# third one arrived (see gate_verdict.py). The alias keeps every existing
# import, test, and manifest reader unchanged.
from skyn3t.studio.gate_verdict import GateVerdict

RagVerdict = GateVerdict


class _PipeDrain:
    """Drain a child's stdout/stderr on background threads, keeping a bounded
    tail. Without this, a chatty server (uvicorn logs every request to stderr)
    fills the OS pipe buffer mid-probe and BLOCKS — a self-inflicted hang."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._err: deque[str] = deque(maxlen=200)
        self._out: deque[str] = deque(maxlen=50)
        self._t_err = threading.Thread(
            target=self._pump, args=(proc.stderr, self._err), daemon=True)
        self._t_out = threading.Thread(
            target=self._pump, args=(proc.stdout, self._out), daemon=True)
        self._t_err.start()
        self._t_out.start()

    @staticmethod
    def _pump(stream, sink: deque[str]) -> None:
        try:
            if stream is None:
                return
            for line in stream:
                sink.append(line)
        except Exception:  # noqa: BLE001 - a read failure just ends the stream
            pass

    def stderr_tail(self, n: int = 400) -> str:
        return "".join(self._err).strip()[-n:]

    def join(self, timeout: float = 2.0) -> None:
        self._t_err.join(timeout=timeout)
        self._t_out.join(timeout=timeout)


def _python_exec(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    # Prefer the interpreter running the gate (its installed packages match the
    # proof env); fall back to a PATH python.
    return sys.executable or shutil.which("python") or shutil.which("python3")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# Loopback probes must never route through an ambient http_proxy (a proxied
# 127.0.0.1 request fails in ways that read as server defects).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    timeout: float = _HTTP_TIMEOUT,
) -> tuple[int, str]:
    """One bounded HTTP round-trip. Returns ``(status, body_text)``; a transport
    failure (refused/timeout/reset) is ``(-1, reason)``. Never raises."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:  # 4xx/5xx: a real, structured response
        try:
            text = err.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return int(err.code), text
    except Exception as exc:  # noqa: BLE001 - refused / timeout / reset
        return -1, str(exc)


def _sse_get(url: str, timeout: float) -> tuple[int, str, str]:
    """One bounded GET returning ``(status, content_type, body)``. The socket
    timeout bounds every read, so a stream that never terminates surfaces as a
    transport failure ``(-1, "", reason)`` instead of a hang."""
    req = urllib.request.Request(url, method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - loopback only
            ctype = str(resp.headers.get("Content-Type") or "")
            return int(resp.status), ctype, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return int(err.code), str(err.headers.get("Content-Type") or ""), ""
    except Exception as exc:  # noqa: BLE001 - refused / timeout / mid-read stall
        return -1, "", str(exc)


def _json_dict(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def _chunk_count(stats: dict) -> int | None:
    """The corpus-size figure from a /v1/stats payload, tolerantly: any int-like
    value under a recognised key (the directive pins ``chunks``)."""
    for key in ("chunks", "total_chunks", "chunk_count", "documents", "count"):
        val = stats.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            return val
        if isinstance(val, float) and val.is_integer():
            return int(val)
    return None


def check_rag(
    project_dir: str | Path,
    stack: str = "",
    *,
    python_exec: str | None = None,
    main_rel: str = "main.py",
    boot_timeout: float = _BOOT_TIMEOUT,
    http_timeout: float = _HTTP_TIMEOUT,
) -> RagVerdict:
    """Boot the delivered RAG server and drive its HTTP contract. Returns an
    advisory :class:`RagVerdict`. Never raises; never hangs a build (every wait
    is deadline-bounded); soft-skips when it cannot run (see module docstring)."""
    try:
        s = (stack or "").strip().lower()
        if s and s != "rag":
            return RagVerdict(skipped=True, reason=f"stack '{stack}' is not a rag stack")
        root = Path(project_dir)
        if not root.is_dir():
            return RagVerdict(skipped=True, reason="project dir is not a directory")
        main_py = root / main_rel
        if not main_py.is_file():
            return RagVerdict(skipped=True, reason=f"no {main_rel} to run")
        py = _python_exec(python_exec)
        if not py:
            return RagVerdict(skipped=True, reason="no python interpreter available")
        verdict = _probe_server(
            root, main_py.name, py,
            boot_timeout=boot_timeout, http_timeout=http_timeout,
        )
        # Phase 2 (generation): only when phase 1 actually got the app SERVING —
        # a skipped/boot-failed/hung phase 1 would just re-report the same
        # failure after a second full boot wait.
        if (not verdict.skipped and verdict.checked.get("health") == "ok"
                and _mock_seam_enabled()):
            llm_issues, llm_checked = _probe_llm_seam(
                root, main_py.name, py,
                boot_timeout=boot_timeout, http_timeout=http_timeout,
            )
            verdict.issues.extend(llm_issues)
            verdict.checked.update(llm_checked)
        return verdict
    except Exception as exc:  # noqa: BLE001 - a gate must never break a build
        return RagVerdict(skipped=True, reason=f"rag check error: {exc}")


def _spawn_server(
    root: Path,
    main_name: str,
    py: str,
    port: int,
    seam_env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen | None, str]:
    """Spawn ``python -B main.py`` with a scrubbed env. Returns ``(proc, err)``
    where a spawn failure is ``(None, reason)``. ``seam_env`` overlays the
    mock-LLM seam for the second, generation-proving boot."""
    env = {
        **os.environ,
        "PORT": str(port),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # The gate must observe the app's contract against a KNOWN seam state (none,
    # or the local mock), not whatever happens to be exported in this shell —
    # and it must never let a delivered app make a REAL, paid LLM call or a real
    # delivery mid-proof. (WEBHOOK_URL: the workflow gate reuses this spawn and
    # needs unconfigured-delivery to be deterministic; harmless for rag apps.)
    for seam in (
        "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
        "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "WEBHOOK_URL",
    ):
        env.pop(seam, None)
    if seam_env:
        env.update(seam_env)
    try:
        # NOT `-I`: since Python 3.11 isolated mode implies -P (safe path), which
        # drops the script's directory from sys.path — the app's own
        # `import rag_core` would crash at boot. `-B` keeps the proof from
        # polluting the artifact with __pycache__ instead.
        proc = subprocess.Popen(
            [py, "-B", main_name],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    return proc, ""


def _await_health(
    proc: subprocess.Popen,
    drain: _PipeDrain,
    base: str,
    root: Path,
    main_name: str,
    port: int,
    checked: dict[str, Any],
    *,
    boot_timeout: float,
    http_timeout: float,
) -> RagVerdict | None:
    """Poll /health until it answers, the process dies, or the deadline. Returns
    ``None`` on success, else the terminal verdict (crash issue / dep skip /
    boot-timeout issue) for the caller to return."""
    deadline = time.monotonic() + boot_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            drain.join()
            err = drain.stderr_tail()
            local = _missing_local_module(err, root)
            if local is not None:
                return RagVerdict(
                    issues=[f"main.py failed to import the project's own "
                            f"'{local}' module at boot — fix the import wiring: "
                            f"{err}"],
                    checked={**checked, "booted": False},
                )
            if _DEP_MISSING_RE.search(err):
                return RagVerdict(
                    skipped=True,
                    reason="third-party deps not importable — gate soft-skipped "
                           "(declared runtime deps; proof env lacks them)")
            return RagVerdict(
                issues=[f"server exited during startup before serving /health "
                        f"(it crashed on boot): {err}"],
                checked={**checked, "booted": False},
            )
        status, _ = _http("GET", f"{base}/health", timeout=min(2.0, http_timeout))
        if status == 200:
            return None
        time.sleep(_POLL_INTERVAL)
    return RagVerdict(
        issues=[f"server did not serve GET /health on port {port} within "
                f"{boot_timeout:.0f}s — `python {main_name}` must start an HTTP "
                "server on the port given by the PORT env var (default 8000) "
                "and expose a 200 /health route"],
        checked={**checked, "booted": proc.poll() is None},
    )


def _probe_server(
    root: Path,
    main_name: str,
    py: str,
    *,
    boot_timeout: float,
    http_timeout: float,
) -> RagVerdict:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc, spawn_err = _spawn_server(root, main_name, py, port)
    if proc is None:
        return RagVerdict(skipped=True, reason=f"could not spawn server: {spawn_err}")

    drain = _PipeDrain(proc)
    checked: dict[str, Any] = {"port": port}
    try:
        # 1. boot: poll /health until it answers, the process dies, or the deadline.
        failed = _await_health(
            proc, drain, base, root, main_name, port, checked,
            boot_timeout=boot_timeout, http_timeout=http_timeout)
        if failed is not None:
            return failed
        checked["booted"] = True
        checked["health"] = "ok"

        issues: list[str] = []

        # 2. /v1/stats — the corpus-stats contract.
        status, text = _http("GET", f"{base}/v1/stats", timeout=http_timeout)
        stats = _json_dict(text) if status == 200 else None
        baseline = _chunk_count(stats) if stats is not None else None
        if status != 200 or stats is None:
            issues.append(
                f"GET /v1/stats must return 200 with a JSON object describing the "
                f"corpus (e.g. {{\"documents\": 1, \"chunks\": 4}}); got status {status}")
        elif baseline is None:
            issues.append(
                "GET /v1/stats returned JSON without an integer corpus-size field — "
                "include a numeric \"chunks\" (and ideally \"documents\") count")
        checked["stats_baseline_chunks"] = baseline

        # 3. ingest the marker doc.
        status, text = _http(
            "POST", f"{base}/ingest",
            body={"text": _MARKER_DOC, "source": "rag-check-fixture"},
            timeout=http_timeout,
        )
        ingested = 200 <= status < 300
        checked["ingest_status"] = status
        if not ingested:
            issues.append(
                f"POST /ingest {{\"text\": ..., \"source\": ...}} must accept a document "
                f"and return 2xx; got status {status}: {text[:200]}")
        elif baseline is not None:
            status, text = _http("GET", f"{base}/v1/stats", timeout=http_timeout)
            after = _chunk_count(_json_dict(text) or {}) if status == 200 else None
            checked["stats_after_ingest_chunks"] = after
            if after is not None and after <= baseline:
                issues.append(
                    "POST /ingest returned 2xx but /v1/stats did not grow — ingested "
                    "documents must actually be chunked and added to the corpus index")

        # 4. query MUST retrieve the just-ingested marker (retrieval, not vibes).
        if ingested:
            q = urllib.parse.quote(_MARKER_QUERY)
            status, text = _http("GET", f"{base}/query?q={q}&k=3", timeout=http_timeout)
            payload = _json_dict(text) if status == 200 else None
            results = payload.get("results") if payload is not None else None
            if status != 200 or not isinstance(results, list):
                issues.append(
                    f"GET /query?q=...&k=3 must return 200 with a JSON object carrying "
                    f"a \"results\" list of retrieved chunks; got status {status}")
                checked["query"] = "malformed"
            elif _MARKER_TOKEN not in json.dumps(results):
                issues.append(
                    "GET /query could not retrieve a document that was JUST ingested "
                    f"(searched for the planted fact '{_MARKER_QUERY}') — retrieval "
                    "must rank the ingested chunks by relevance to the query")
                checked["query"] = "marker_not_found"
            else:
                checked["query"] = "marker_retrieved"

        # 5. /chat answers (extractive fallback: no key, no LLM, still 200).
        status, text = _http(
            "POST", f"{base}/chat",
            body={"question": _MARKER_QUERY},
            timeout=http_timeout,
        )
        payload = _json_dict(text) if status == 200 else None
        answer = payload.get("answer") if payload is not None else None
        if status != 200 or not (isinstance(answer, str) and answer.strip()):
            issues.append(
                f"POST /chat {{\"question\": ...}} must return 200 with a non-empty "
                f"\"answer\" string even with NO LLM configured (fall back to an "
                f"extractive answer from the top retrieved chunk); got status {status}")
            checked["chat"] = "failed"
        else:
            checked["chat"] = "ok"
            checked["chat_answer_grounded"] = _MARKER_TOKEN in answer

        # 5b. /chat/stream — the SSE contract (wave-2 §3.1 streaming tier): a
        # 200 text/event-stream that emits `data:` frames, carries the grounded
        # answer, and TERMINATES with `data: [DONE]` (a never-ending stream
        # surfaces as a bounded transport failure, never a hang).
        if ingested:
            q = urllib.parse.quote(_MARKER_QUERY)
            status, ctype, body = _sse_get(
                f"{base}/chat/stream?question={q}&k=3", timeout=http_timeout)
            if status != 200:
                issues.append(
                    f"GET /chat/stream?question=...&k=3 must return 200 with an SSE "
                    f"stream of the grounded answer; got status {status}")
                checked["chat_stream"] = "failed"
            elif not ctype.lower().startswith("text/event-stream"):
                issues.append(
                    f"/chat/stream must set Content-Type: text/event-stream "
                    f"(got {ctype!r}) so browsers can consume it via EventSource")
                checked["chat_stream"] = "wrong_content_type"
            elif "data:" not in body or "[DONE]" not in body:
                issues.append(
                    "/chat/stream must emit SSE `data:` frames and terminate with "
                    "a final `data: [DONE]` frame so clients know the answer ended")
                checked["chat_stream"] = "malformed_frames"
            else:
                checked["chat_stream"] = "ok"
                checked["chat_stream_grounded"] = _MARKER_TOKEN in body

        # 6. one malformed call — an ingest with no text must yield a structured
        # 4xx (validation), never a 5xx crash and never a silent 2xx accept.
        status, text = _http("POST", f"{base}/ingest", body={}, timeout=http_timeout)
        checked["malformed_ingest_status"] = status
        if status == -1:
            issues.append(
                "the server stopped responding after a malformed POST /ingest "
                "(missing \"text\") — it must validate inputs and return a 4xx")
        elif status >= 500:
            issues.append(
                f"a malformed POST /ingest (missing \"text\") crashed the handler "
                f"(status {status}) — validate the request body and return a 4xx")
        elif 200 <= status < 300:
            issues.append(
                "a malformed POST /ingest (missing \"text\") was ACCEPTED with a 2xx — "
                "the request body must be validated (a text field is required)")

        return RagVerdict(issues=issues, checked=checked)
    finally:
        _shutdown(proc)


def _mock_seam_enabled() -> bool:
    """The ``mock_llm_proof_enabled`` setting (default True) — the same
    kill-switch proof_run's mock seam honours. Never raises."""
    try:
        from skyn3t.config.settings import get_settings

        return bool(getattr(get_settings(), "mock_llm_proof_enabled", True))
    except Exception:  # noqa: BLE001 - a gate must never break a build
        return True


def _probe_llm_seam(
    root: Path,
    main_name: str,
    py: str,
    *,
    boot_timeout: float,
    http_timeout: float,
) -> tuple[list[str], dict[str, Any]]:
    """Second boot, seam configured: point OPENAI_BASE_URL at the local
    deterministic mock-LLM and prove the GENERATION side of the app.

    Returns ``(issues, checked)`` merged into the phase-1 verdict. Degrades open
    (no issues, a ``llm_seam`` reason in checked) when the mock can't run —
    this phase must never turn a provable app into a false flag. Contract:

    * /chat must still answer with a seam configured (a crash here means the
      LLM path is wired but broken — worse than no wiring).
    * IF the app called the seam: the captured prompt must contain the
      retrieved marker chunk (retrieval feeds generation) and the mock's
      completion must surface in the answer (the reply isn't discarded).
    * An app that never calls the seam (pure extractive) is compliant:
      ``llm_called=False`` is recorded, never flagged.
    """
    checked: dict[str, Any] = {}
    try:
        from skyn3t.studio.mock_llm import MOCK_MARKER, MockLLMServer
    except Exception as exc:  # noqa: BLE001 - degrade open, never flag
        return [], {"llm_seam": f"skipped: mock_llm unavailable ({exc})"}

    mock = MockLLMServer()
    proc: subprocess.Popen | None = None
    try:
        mock_base = mock.start()
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        # The app's convention (and the scaffold's): POST {OPENAI_BASE_URL}/chat/
        # completions — so the seam must include the mock's /v1 prefix.
        proc, spawn_err = _spawn_server(root, main_name, py, port, seam_env={
            "OPENAI_BASE_URL": f"{mock_base}/v1",
            "OPENAI_API_KEY": "sk-mock-proof",
        })
        if proc is None:
            return [], {"llm_seam": f"skipped: could not spawn ({spawn_err})"}
        drain = _PipeDrain(proc)
        failed = _await_health(
            proc, drain, base, root, main_name, port, {},
            boot_timeout=boot_timeout, http_timeout=http_timeout)
        if failed is not None:
            if failed.skipped:
                return [], {"llm_seam": f"skipped: {failed.reason}"}
            return (
                [f"with OPENAI_BASE_URL configured, the server failed to boot — "
                 f"the LLM seam must never break startup: {failed.issues[0]}"],
                {"llm_seam": "boot_failed"},
            )

        issues: list[str] = []
        # Ingest the marker doc so retrieval has something to feed generation.
        status, _text = _http(
            "POST", f"{base}/ingest",
            body={"text": _MARKER_DOC, "source": "rag-check-fixture"},
            timeout=http_timeout,
        )
        if not (200 <= status < 300):
            return (
                [f"with OPENAI_BASE_URL configured, POST /ingest broke "
                 f"(status {status}) — the seam must not affect ingestion"],
                {"llm_seam": "ingest_failed"},
            )

        status, text = _http(
            "POST", f"{base}/chat",
            body={"question": _MARKER_QUERY},
            timeout=http_timeout,
        )
        payload = _json_dict(text) if status == 200 else None
        answer = payload.get("answer") if payload is not None else None
        if status != 200 or not (isinstance(answer, str) and answer.strip()):
            checked["llm_seam"] = "chat_failed"
            issues.append(
                f"POST /chat broke when OPENAI_BASE_URL was configured "
                f"(status {status}) — the LLM path must work against any "
                "OpenAI-compatible endpoint and fall back cleanly on failure")
            return issues, checked

        calls = mock.captured_requests()
        checked["llm_called"] = bool(calls)
        if not calls:
            # Pure-extractive /chat: contract-compliant. Record grounding only.
            checked["llm_seam"] = "ok_extractive"
            checked["chat_answer_grounded"] = _MARKER_TOKEN in answer
            return issues, checked

        prompt_text = json.dumps([c.to_dict() for c in calls])
        if _MARKER_TOKEN not in prompt_text:
            issues.append(
                "the app called the LLM seam but the prompt did NOT contain the "
                "retrieved context (the just-ingested marker chunk is absent) — "
                "/chat must pass the /query results into the LLM prompt so "
                "retrieval feeds generation")
        if MOCK_MARKER not in answer:
            issues.append(
                "the app called the LLM seam but the completion never surfaced "
                "in the /chat answer — return the LLM's reply (grounded in the "
                "retrieved context), don't discard it for a canned string")
        checked["llm_seam"] = "ok" if not issues else "defective"
        return issues, checked
    except Exception as exc:  # noqa: BLE001 - degrade open, never flag
        return [], {"llm_seam": f"skipped: {exc}"}
    finally:
        if proc is not None:
            _shutdown(proc)
        mock.stop()


def _shutdown(proc: subprocess.Popen) -> None:
    """Tear the server down — terminate, then kill. Never raises."""
    try:
        proc.terminate()
        proc.wait(timeout=2.0)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
