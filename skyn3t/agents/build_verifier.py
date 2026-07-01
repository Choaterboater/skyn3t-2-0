"""BuildVerifierAgent — runs the real install/build, plus reward-hacking guard.

The verifier attempts an actual dependency install + build using the configured
execution backend (npm/pip). When the backend or toolchain is unavailable it
DEGRADES to a deterministic dry check (manifest parses, build script declared)
rather than crashing (design rule #6) — but it never reports green on an empty
or obviously gamed project (design rule #3).

2.0 P0 — REWARD HARDENING: :func:`detect_reward_hacking` is a module-level
helper that flags graders being gamed: empty/trivial tests, fabricated success
logs, deleted assertions, suspiciously tiny projects claiming success.

agent_type: "verify_build"   capability: "verify_build"
Stage output: {"ok": bool, "verdict": "pass"|"fail", "mode": str,
               "details": str, "reward_hacking": {...}}
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from skyn3t.agents import _verify_common as vc
from skyn3t.config.settings import get_settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus

# --- reward-hacking heuristics ------------------------------------------------

# A test body with no assertion / expectation is a trivial/gamed test.
_ASSERT_TOKENS = (
    "assert", "self.assert", "expect(", "should", "pytest.raises",
    ".toBe", ".toEqual", ".toThrow", "assertEqual", "assertTrue",
)
# Phrases that look like a fabricated/hallucinated success log rather than
# genuine tool output.
_FABRICATED_LOG = re.compile(
    r"(?i)(all tests pass(ed)?|build succeeded|100% coverage|everything works|"
    r"no errors found|✅\s*done)"
)


def _iter_test_files(root: Path) -> list[Path]:
    out = []
    for p in vc.iter_files(root):
        name = p.name.lower()
        if p.suffix == ".py" and (name.startswith("test_") or name.endswith("_test.py")):
            out.append(p)
        elif p.suffix in {".js", ".ts", ".tsx", ".jsx"} and (
            ".test." in name or ".spec." in name
        ):
            out.append(p)
    return out


def detect_reward_hacking(artifact_dir: str | Path | None,
                          claimed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flag attempts to game the grader.

    Args:
        artifact_dir: project / artifact directory to inspect.
        claimed: what the upstream stage claims (e.g. {"success": True,
                 "tests_passed": 10, "log": "...", "score": 95}).

    Returns a dict: {"suspicious": bool, "flags": [str, ...], "score": float}
    where ``score`` is a 0-1 suspicion level. Pure & offline.
    """
    claimed = claimed or {}
    flags: list[str] = []
    root = None
    if artifact_dir:
        try:
            cand = Path(artifact_dir)
            if cand.exists() and cand.is_dir():
                root = cand
        except (TypeError, ValueError):
            root = None

    claims_success = bool(
        claimed.get("success") or claimed.get("ok")
        or (claimed.get("score") or 0) >= 50
        or (claimed.get("tests_passed") or 0) > 0
        or str(claimed.get("verdict", "")).lower() in {"go", "pass"}
    )

    if root is None:
        if claims_success:
            flags.append("claims success but no inspectable artifact directory exists")
        return {"suspicious": bool(flags), "flags": flags,
                "score": 1.0 if flags else 0.0}

    files = vc.iter_files(root)
    src_count = vc.non_empty_source_count(root)
    total_bytes = sum(vc.file_size(p) for p in files)

    # 1) Suspiciously tiny project claiming success.
    if claims_success and (src_count == 0 or total_bytes < 200):
        flags.append(
            f"claims success but project is near-empty "
            f"({src_count} source files, {total_bytes} bytes)"
        )

    # 2) Tests that exist but assert nothing (trivial/gamed tests).
    test_files = _iter_test_files(root)
    if claimed.get("tests_passed") and not test_files:
        flags.append("claims passing tests but no test files exist")
    trivial = 0
    for tf in test_files:
        text = vc.safe_read(tf)
        if not any(tok in text for tok in _ASSERT_TOKENS):
            trivial += 1
    if test_files and trivial == len(test_files):
        flags.append(f"all {len(test_files)} test file(s) contain no assertions (trivial tests)")
    elif trivial:
        flags.append(f"{trivial} test file(s) contain no assertions")

    # 3) Deleted assertions: tests that are only skips/pass/xfail.
    for tf in test_files:
        text = vc.safe_read(tf)
        lowered = text.lower()
        if ("pytest.skip" in lowered or "@pytest.mark.skip" in lowered
                or "xfail" in lowered) and not any(t in text for t in _ASSERT_TOKENS):
            flags.append(f"{tf.name}: tests skipped/xfailed instead of asserting")

    # 4) Fabricated success logs committed as files (vs real tool output).
    claimed_log = str(claimed.get("log", "")) + str(claimed.get("details", ""))
    if claimed_log and _FABRICATED_LOG.search(claimed_log) and src_count == 0:
        flags.append("success log present but no real source to have produced it")

    # 5) A README/log file declaring victory while code is empty.
    for p in files:
        if p.suffix in {".md", ".txt", ".log"} and src_count == 0:
            text = vc.safe_read(p, max_bytes=4000)
            if _FABRICATED_LOG.search(text):
                flags.append(f"{p.name} declares success but no source files exist")
                break

    score = min(1.0, 0.4 * len(flags))
    return {"suspicious": bool(flags), "flags": flags, "score": round(score, 3)}


# --- agent --------------------------------------------------------------------


class BuildVerifierAgent(BaseAgent):
    def __init__(self, name: str = "build_verifier", event_bus: EventBus | None = None,
                 config: dict | None = None) -> None:
        super().__init__(name, agent_type="verify_build", provider="local",
                         event_bus=event_bus, config=config or {})
        self.add_capability(AgentCapability(
            name="verify_build",
            description="Runs install/build (or degraded dry check) and guards against reward hacking",
            tags=("verify", "build"),
        ))
        self.settings = get_settings()

    async def initialize(self) -> None:
        self.metadata["backend"] = getattr(self.settings, "execution_backend", "auto")

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        payload = task.payload or {}
        root = vc.resolve_project_dir(payload)
        stack = vc.detect_stack(root, payload)

        # Reward-hardening gate runs FIRST: never green a gamed project.
        raw_prior = payload.get("prior")
        prior: dict[str, Any] = raw_prior if isinstance(raw_prior, dict) else {}
        raw_code_out = prior.get("code")
        code_out: dict[str, Any] = raw_code_out if isinstance(raw_code_out, dict) else {}
        claimed = {**code_out}
        claimed.update({k: payload.get(k) for k in ("success", "score", "tests_passed", "log", "details") if k in payload})
        # The real code stage emits {files_written, worktree_dir, stack, files,
        # backend} and never the explicit success keys, so derive the implicit
        # success claim from what it actually reports: producing files IS the
        # claim of a delivered project. Without this, reward-hacking checks #1
        # and #4 are dead on production data (they gate on claims_success).
        if "success" not in claimed and "ok" not in claimed:
            files_written = code_out.get("files_written")
            files_list = code_out.get("files")
            implied = bool(
                (isinstance(files_written, int) and files_written > 0)
                or (isinstance(files_list, (list, tuple, dict)) and len(files_list) > 0)
            )
            if implied:
                claimed["success"] = True
        # Feed any generation/build log the code stage reported into the
        # fabricated-log heuristic (check #4) when no explicit log was passed.
        if "log" not in claimed and "details" not in claimed:
            for key in ("log", "details", "output", "summary"):
                val = code_out.get(key)
                if isinstance(val, str) and val:
                    claimed["log"] = val
                    break
        reward = detect_reward_hacking(str(root) if root else None, claimed)

        ran_real, build_ok, mode, details = await self._build(root, stack)

        ok = build_ok and not reward["suspicious"]
        if reward["suspicious"]:
            details = f"reward-hacking suspected: {'; '.join(reward['flags'])} | {details}"

        return TaskResult(
            task_id=task.task_id, success=True,
            output={
                "ok": ok,
                "verdict": "pass" if ok else "fail",
                "mode": mode,
                "ran_real_build": ran_real,
                "details": details,
                "stack": stack,
                "reward_hacking": reward,
            },
        )

    async def _build(self, root: Path | None, stack: str) -> tuple[bool, bool, str, str]:
        """Return (ran_real, ok, mode, details)."""
        if root is None:
            return False, False, "none", "no project directory to build"

        backend = getattr(self.settings, "execution_backend", "auto")
        # We only run real builds inline here; docker backend is owned elsewhere.
        allow_real = backend in ("auto", "inline")

        # Key the npm path on a real package.json, NOT the stack label. The label
        # for a JS app is "nextjs"/"react"/"static"/"vite"/… — none of which is
        # literally "node", so gating on stack=="node" silently sent every web
        # build to the degraded dry check and the real `npm install`/build never
        # ran (hallucinated versions + app/pages route conflicts slipped through
        # as a false "pass"). package.json presence is the honest signal.
        if (root / "package.json").is_file() and allow_real and shutil.which("npm"):
            ok, out = await self._run(["npm", "install", "--no-audit", "--no-fund"], root, timeout=300)
            if ok:
                # build script optional — try it but don't hard-fail if absent
                pkg = json.loads(vc.safe_read(root / "package.json") or "{}")
                if isinstance(pkg, dict) and "build" in (pkg.get("scripts") or {}):
                    bok, bout = await self._run(["npm", "run", "build"], root, timeout=300)
                    return True, bok, "npm", (bout[-500:] if bout else "build run")
                return True, True, "npm", "install ok (no build script)"
            return True, False, "npm", out[-500:]

        # Swift Package Manager: key on the manifest (like package.json above),
        # NOT the stack label, and compile for real with `swift build`. Absent
        # toolchain falls through to the degraded dry check below.
        if (root / "Package.swift").is_file() and allow_real and shutil.which("swift"):
            ok, out = await self._run(["swift", "build"], root, timeout=300)
            return True, ok, "swift", (out[-500:] if out else "swift build")

        if stack == "python" and allow_real and shutil.which("python"):
            req = root / "requirements.txt"
            if req.exists():
                ok, out = await self._run(
                    ["python", "-m", "pip", "install", "--dry-run", "-r", "requirements.txt"],
                    root, timeout=120,
                )
                return True, ok, "pip-dry", out[-500:]
            # compile all .py files as a real build signal
            ok, out = await self._compile_python(root)
            return True, ok, "py_compile", out

        # Degraded dry check: manifest parses + has source content.
        return False, self._dry_check(root, stack), "dry", "degraded dry check (toolchain unavailable)"

    def _dry_check(self, root: Path, stack: str) -> bool:
        if vc.non_empty_source_count(root) == 0:
            return False
        if (root / "package.json").is_file():
            try:
                pkg = json.loads(vc.safe_read(root / "package.json") or "")
            except (json.JSONDecodeError, ValueError):
                return False
            if isinstance(pkg, dict):
                from skyn3t.studio.proof_run import _invalid_npm_package_names

                if _invalid_npm_package_names(pkg):
                    return False
        return True

    async def _compile_python(self, root: Path) -> tuple[bool, str]:
        import py_compile
        errors = []
        compiled = 0
        for p in vc.iter_files(root):
            if p.suffix == ".py":
                try:
                    py_compile.compile(str(p), doraise=True)
                    compiled += 1
                except py_compile.PyCompileError as exc:
                    errors.append(str(exc))
        if errors:
            return False, "; ".join(errors[:3])
        return compiled > 0, f"compiled {compiled} python file(s)"

    async def _run(self, cmd: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                # Reap the killed child / drain pipes so repeated timeouts (npm
                # install/build at 300s) don't accumulate zombies + open FDs.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (TimeoutError, ProcessLookupError, Exception):  # noqa: BLE001
                    pass
                return False, f"command timed out after {timeout}s: {' '.join(cmd)}"
            text = (out or b"").decode("utf-8", errors="replace")
            return proc.returncode == 0, text
        except (OSError, ValueError) as exc:
            return False, f"failed to run {' '.join(cmd)}: {exc}"
