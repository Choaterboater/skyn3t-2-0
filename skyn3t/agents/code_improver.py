"""CodeImproverAgent — rewrites existing files given reviewer gaps.

Reads the files already in the worktree plus the reviewer's gaps/score, and
rewrites the files to address them. With a real LLM backend it regenerates the
flagged files from a repair prompt; offline it applies deterministic, safe
touch-ups (e.g. guaranteeing default exports / module.exports) so the stage
always makes a concrete, non-destructive change (design rules #1 and #6).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from skyn3t.adapters.llm import LLMClient
from skyn3t.agents._common import detect_stack, extract_code
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.model_router import Tier

_SYSTEM = (
    "You are a senior engineer fixing code. Given a file and a list of issues, "
    "rewrite the file to resolve them. Output ONLY the corrected file contents, "
    "no commentary, no markdown fences."
)

_CREATE_SYSTEM = (
    "You are a senior engineer completing a codebase. A file is imported by "
    "existing code but was never created. Given the importing file's context and "
    "a list of issues, write the COMPLETE contents of the new file. Output ONLY "
    "the new file's contents, no commentary, no markdown fences."
)

_TARGET_DISCOVERY_SYSTEM = (
    "You are a senior engineer localizing a code change. Given a repository map "
    "and a plain-English goal, name the EXISTING file paths (relative to the "
    "repo root, exactly as shown in the map) that most need editing to achieve "
    "the goal. Output ONLY the file paths, one per line, no commentary, no "
    "markdown, no bullets, no numbering, no code fences. List at most 6 paths, "
    "most important first."
)

# Leading bullet/numbering decoration a target-discovery response line might
# carry despite being asked for bare paths (e.g. "- app/page.jsx", "1. app/page.jsx").
_DISCOVERY_BULLET_RE = re.compile(r"^(?:[-*•]|\d+[.)])\s*")

_MAX_DISCOVERED_TARGETS = 6


def _parse_discovery_lines(text: str) -> list[str]:
    """Best-effort parse of a target-discovery LLM response into bare relative
    paths, one per input line. Never raises; unparseable/empty input -> []."""
    if not text:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _DISCOVERY_BULLET_RE.sub("", line).strip()
        line = line.strip("`'\" \t")
        if not line or " " in line:
            continue  # a real path never contains a space; drop commentary lines
        out.append(line)
    return out

# "<importer> -> <spec>" — the exact format extract_error_gaps() in proof_run.py
# emits for an unresolved local import (proof_run.py:739).
_UNRESOLVED_IMPORT_RE = re.compile(r"UNRESOLVED IMPORT.*?:\s*(\S+)\s*->\s*(\S+)")

# Every `<script ... src="X">` in an HTML document — the external entrypoints a page
# loads (e.g. the Vite `/src/main.js` bundle). A rewrite that drops one renders a blank
# page, so the improver must preserve them (see _preserves_html_entrypoints).
_HTML_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)


def _html_script_srcs(content: str) -> set[str]:
    """The set of `src` values of every `<script src="...">` in ``content``."""
    return {m.group(1).strip() for m in _HTML_SCRIPT_SRC_RE.finditer(content)}


def _sanitize_package_json(content: str) -> str | None:
    """Deterministically repair malformed dependency keys in package.json.

    Trims a fixable leading/trailing space (' slick-carousel' -> 'slick-carousel'),
    drops names that can't be made npm-legal (empty, internal whitespace). Returns
    the rewritten JSON, or None when nothing needed fixing / not parseable — so a
    valid file is never touched. This lets the fix-loop repair the dominant JS
    failure class (EINVALIDPACKAGENAME) with no model round-trip.
    """
    try:
        pkg = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(pkg, dict):
        return None
    changed = False
    for section in ("dependencies", "devDependencies",
                    "peerDependencies", "optionalDependencies"):
        deps = pkg.get(section)
        if not isinstance(deps, dict):
            continue
        rebuilt: dict[str, Any] = {}
        for name, ver in deps.items():
            if not isinstance(name, str):
                changed = True
                continue
            trimmed = name.strip()
            # Unfixable: empty, or internal whitespace a trim can't resolve.
            if not trimmed or re.search(r"\s", trimmed):
                changed = True
                continue
            if trimmed != name:
                changed = True
            rebuilt[trimmed] = ver
        if rebuilt != deps:
            pkg[section] = rebuilt
            changed = True
    if not changed:
        return None
    return json.dumps(pkg, indent=2) + "\n"


class CodeImproverAgent(BaseAgent):
    def __init__(self, name: str = "code_improver", *, event_bus: EventBus,
                 llm: LLMClient | None = None, config: dict | None = None) -> None:
        super().__init__(name, agent_type="code_improve", provider="llm",
                         event_bus=event_bus, config=config)
        self.add_capability(AgentCapability(
            name="code_improve", description="Rewrite files to address reviewer gaps",
            tags=("generative", "code", "repair")))
        self.llm = llm or LLMClient()

    async def initialize(self) -> None:
        self.metadata["backend"] = self.llm.backend

    async def execute(self, task: TaskRequest) -> TaskResult:
        p = task.payload
        brief = p.get("brief", "") or p.get("slug", "app")
        root = p.get("worktree_dir") or p.get("project_dir")
        if not root:
            # Never default a write-capable target root to the process cwd:
            # safe-by-default, no-op rather than mutating arbitrary files.
            return TaskResult(task_id=task.task_id, success=False,
                              output={"files_improved": 0, "files": []},
                              error="no project_dir in payload")
        worktree = Path(root)
        stack = detect_stack(brief=brief, plan=p.get("plan"), explicit=p.get("stack", ""))

        prior = p.get("prior", {}) if isinstance(p.get("prior"), dict) else {}
        review = prior.get("review", {}) if isinstance(prior.get("review"), dict) else {}
        gaps = p.get("gaps") or review.get("gaps") or []
        target_files = p.get("files") or self._targets_from_gaps(gaps, worktree)
        if not target_files:
            # Neither an explicit "files" list nor the error-shaped gap parser
            # found anything to edit -- the common case for a free-text improve
            # goal on a stack whose entrypoint isn't in the deterministic guess
            # list. Ask the LLM to localize the change from the repo map the
            # caller already computed, instead of silently doing nothing.
            target_files = await self._discover_targets_via_repo_map(
                p.get("repo_map"), brief, worktree)

        improved: list[str] = []
        for rel in target_files:
            target = (worktree / rel).resolve()
            if not self._confined(worktree, target):
                continue
            if target.is_file():
                original = target.read_text(encoding="utf-8")
                new_content = await self._improve_one(rel, original, brief, gaps, stack)
            elif target.exists():
                continue  # a dir sits where a file was expected — nothing sensible to do
            else:
                # The file was NAMED by a repair gap (typically an UNRESOLVED
                # IMPORT) but never written — e.g. codegen's main.js imports
                # ./PreloadScene.js which was never created, so the app can't
                # boot. Editing can't fix a file that doesn't exist; CREATE it.
                original = ""
                new_content = await self._create_one(rel, brief, gaps, stack, worktree)
            if new_content and new_content.strip() and new_content != original:
                from skyn3t.agents.validate import validate_source
                ok, _ = validate_source(rel, new_content)
                if ok and self._preserves_html_entrypoints(rel, original, new_content):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(new_content, encoding="utf-8")
                    improved.append(rel)
                # else keep original (broke syntax OR an .html rewrite dropped a
                # <script src> entrypoint) — never regress a working file.

        return TaskResult(task_id=task.task_id, success=True,
                          output={"files_improved": len(improved), "files": sorted(improved),
                                  "worktree_dir": str(worktree), "backend": self.llm.backend})

    async def _improve_one(self, rel: str, original: str, brief: str,
                           gaps: list[Any], stack: str) -> str:
        if self.llm.backend != "stub":
            ext = Path(rel).suffix.lower()
            tier = Tier.UI if ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"} else Tier.BACKEND
            prompt = (
                f"Brief: {brief}\nFile: {rel}\nIssues to fix: {gaps}\n\n"
                f"Current contents:\n{original}\n\nRewrite the file."
            )
            try:
                result = await self.llm.complete(prompt, tier=tier, system=self.system_prompt(_SYSTEM),
                                                 file_hint=rel, max_tokens=4096, task_type=self.agent_type)
                # Degraded-to-stub result must not clobber a working file.
                if result.backend != "stub":
                    fixed = extract_code(result.text)
                    if fixed and fixed.strip():
                        return fixed
            except Exception:  # noqa: BLE001 - fall through to deterministic touch-up
                pass
        return self._deterministic_fix(rel, original, stack)

    async def _create_one(self, rel: str, brief: str, gaps: list[Any], stack: str,
                          worktree: Path) -> str:
        """Write a BRAND NEW file at `rel` that some existing file imports but that
        codegen never created. Unlike `_improve_one`, there is no deterministic
        offline fallback here — synthesizing a plausible NEW file (not just a
        touch-up) needs the brief/context only an LLM has; `scaffold_missing_imports`
        (proof_run.py) is the deterministic backstop for when this can't run or
        doesn't converge. Returns "" (no-op) when there's no real backend or the
        call fails — the caller then correctly leaves the file absent rather than
        writing something nonsensical."""
        if self.llm.backend == "stub":
            return ""
        importer_rel, spec = self._find_importer_for_missing(rel, gaps, worktree)
        importer_context = ""
        if importer_rel:
            try:
                importer_src = (worktree / importer_rel).read_text(encoding="utf-8")
            except OSError:
                importer_src = ""
            importer_context = (
                f"\nThe importing file is {importer_rel}, which does:\n"
                f"  import ... from '{spec}'\n\n"
                f"Full contents of {importer_rel} for context:\n{importer_src}\n"
            )
        ext = Path(rel).suffix.lower()
        tier = Tier.UI if ext in {".jsx", ".tsx", ".css", ".html", ".vue", ".svelte"} else Tier.BACKEND
        prompt = (
            f"Brief: {brief}\nStack: {stack}\nMissing file to CREATE: {rel}\n"
            f"Issues: {gaps}\n{importer_context}\n"
            f"This file does NOT exist yet — you are creating it, not editing it. "
            f"Write the COMPLETE, real contents of a new file at {rel} that the "
            f"importer above expects, matching the project's existing code style "
            f"and the {stack} stack."
        )
        try:
            result = await self.llm.complete(prompt, tier=tier, system=self.system_prompt(_CREATE_SYSTEM),
                                             file_hint=rel, max_tokens=4096, task_type=self.agent_type)
            if result.backend != "stub":
                created = extract_code(result.text)
                if created and created.strip():
                    return created
        except Exception:  # noqa: BLE001 - never break the whole repair task
            pass
        return ""

    @staticmethod
    def _find_importer_for_missing(
        rel: str, gaps: list[Any], worktree: Path,
    ) -> tuple[str | None, str | None]:
        """For a missing target `rel`, find the (importer_rel, raw_spec) from an
        UNRESOLVED IMPORT gap whose resolved path matches it — so the create-prompt
        can show the LLM what's actually being imported. (None, None) if no gap
        names it this way (e.g. `rel` came from an explicit `files` list)."""
        target_abs = (worktree / rel).resolve()
        for g in gaps:
            text = g if isinstance(g, str) else str(g)
            m = _UNRESOLVED_IMPORT_RE.search(text)
            if not m:
                continue
            importer_rel, spec = m.group(1), m.group(2)
            resolved = CodeImproverAgent._resolve_import_target(importer_rel, spec, worktree)
            if resolved is not None and (worktree / resolved).resolve() == target_abs:
                return importer_rel, spec
        return None, None

    @staticmethod
    def _resolve_import_target(importer_rel: str, spec: str, worktree: Path) -> str | None:
        """Resolve a relative import spec against its IMPORTER's directory to a
        worktree-relative path — 'src/main.js' + './PreloadScene.js' ->
        'src/PreloadScene.js', NOT the wrong worktree-ROOT-relative guess a naive
        filename regex over the raw gap text would produce (a bare './x' spec is
        relative to the file that imports it, not to the project root). Returns
        None for a non-relative/bare (npm package) spec."""
        spec_clean = spec.split("?", 1)[0].split("#", 1)[0]
        if not spec_clean.startswith("."):
            return None
        importer_abs = (worktree / importer_rel).resolve()
        try:
            target_abs = (importer_abs.parent / spec_clean).resolve()
            return str(target_abs.relative_to(worktree.resolve()))
        except (ValueError, OSError):
            return None

    def _deterministic_fix(self, rel: str, content: str, stack: str) -> str:
        """Safe offline improvements that don't break a working file."""
        if rel.endswith("package.json"):
            sanitized = _sanitize_package_json(content)
            if sanitized:
                return sanitized
        if rel.endswith(".jsx") and "App" in rel and "export default" not in content:
            if re.search(r"function\s+App\b", content):
                return content.rstrip() + "\n\nexport default App\n"
        if rel.endswith("server.js") and "module.exports" not in content:
            return content.rstrip() + "\nmodule.exports = app;\n"
        return content

    def _targets_from_gaps(self, gaps: list[Any], worktree: Path) -> list[str]:
        """Infer which files to touch from gap text; fall back to entrypoints."""
        candidates: list[str] = []
        for g in gaps:
            text = g if isinstance(g, str) else str(g)
            unresolved = _UNRESOLVED_IMPORT_RE.search(text)
            if unresolved:
                # "<importer> -> <spec>": spec is relative to the IMPORTER's own
                # directory, not the worktree root — resolve it properly instead
                # of letting the generic regex below grab the bare, wrongly
                # worktree-root-relative spec string as a second, wrong candidate.
                importer_rel, spec = unresolved.group(1), unresolved.group(2)
                candidates.append(importer_rel)  # still a valid edit target on its own
                resolved = self._resolve_import_target(importer_rel, spec, worktree)
                if resolved:
                    candidates.append(resolved)
                continue
            for m in re.findall(r"[\w./-]+\.(?:jsx|tsx|js|ts|json|py|css|html)", text):
                candidates.append(m)
            # A dependency/install/resolution failure must route to package.json
            # even when the gap text never names the file — otherwise the loop
            # rewrites an unrelated source entrypoint and the same error recurs.
            # Includes BUILD-time "module not found" errors (a missing or
            # version-broken dependency, e.g. '@hookform/resolvers/yup'), not
            # just npm-install errors.
            if re.search(r"npm error|EINVALIDPACKAGENAME|ERESOLVE|ETARGET|E404|"
                         r"Invalid package name|npm install|peer dep|"
                         r"module not found|can(?:no|')t resolve|cannot resolve|"
                         r"cannot find module|failed to resolve", text, re.I):
                if (worktree / "package.json").is_file():
                    candidates.append("package.json")
            # "X is not exported from '../lib/constants'" — a named export the
            # module never defines. Route to that module so the repair adds the
            # real export (e.g. the HVAC `services`/`companyInfo` data), instead
            # of an unrelated entrypoint. The spec lacks an extension, so glob it.
            for spec in re.findall(r"not exported from ['\"]([^'\"]+)['\"]", text):
                tail = re.sub(r"^(?:@[\w-]*/|\.\.?/)+", "", spec.split("?")[0]).strip("/")
                if not tail:
                    continue
                for ext in (".js", ".jsx", ".ts", ".tsx"):
                    hits = sorted(worktree.glob(f"**/{tail}{ext}"))
                    if hits:
                        candidates.append(str(hits[0].relative_to(worktree)))
                        break
        if candidates:
            return list(dict.fromkeys(candidates))
        # Default to known entrypoints that exist. Covers plain React-Vite /
        # Python / Node / static shapes AND the framework-router conventions a
        # free-text goal (no error text to parse) commonly lands on — without
        # this, a real Next.js/Astro/Remix project got targets == [] and the
        # whole edit loop silently never ran (see _targets_from_gaps docstring).
        for guess in (
            "src/App.jsx", "src/main.jsx", "main.py", "server.js", "index.html",
            # Next.js App Router
            "app/page.jsx", "app/page.tsx",
            # Next.js Pages Router
            "pages/index.jsx", "pages/index.tsx",
            # Astro
            "src/pages/index.astro",
            # Remix
            "app/routes/_index.jsx", "app/root.jsx",
        ):
            if (worktree / guess).is_file():
                candidates.append(guess)
        return candidates

    async def _discover_targets_via_repo_map(
        self, repo_map: str | None, goal: str, worktree: Path,
    ) -> list[str]:
        """LLM-driven fallback for when `_targets_from_gaps` finds nothing: ask
        the model to name existing files to edit from the repo map ImproveEngine
        already computed. Defensive by construction -- a stub backend, a missing
        repo_map, an LLM failure, or a garbage response all degrade to [] rather
        than raising or writing anywhere. Every returned path is verified to
        resolve to a real, worktree-confined file before being trusted."""
        if not repo_map or not repo_map.strip() or self.llm.backend == "stub":
            return []
        prompt = (
            f"Goal: {goal}\n\nRepository map:\n{repo_map}\n\n"
            "Which existing files should be edited to achieve the goal?"
        )
        try:
            result = await self.llm.complete(
                prompt, tier=Tier.CHEAP, system=self.system_prompt(_TARGET_DISCOVERY_SYSTEM),
                max_tokens=300, task_type=self.agent_type)
        except Exception:  # noqa: BLE001 - discovery is best-effort, never fatal
            return []
        if result.backend == "stub":
            return []
        found: list[str] = []
        for rel in _parse_discovery_lines(result.text):
            target = (worktree / rel).resolve()
            if not self._confined(worktree, target) or not target.is_file():
                continue
            if rel not in found:
                found.append(rel)
            if len(found) >= _MAX_DISCOVERED_TARGETS:
                break
        return found

    @staticmethod
    def _preserves_html_entrypoints(rel: str, original: str, new_content: str) -> bool:
        """For an ``.html`` rewrite over a NON-EMPTY original, every ``<script src="X">``
        the original loaded must still be present in the rewrite. Dropping the module
        entrypoint (e.g. the Vite ``<script src="/src/main.js">``) yields a page that
        renders nothing — a net regression the advisory improver must never introduce.

        True (no constraint) for non-HTML targets, brand-new files (empty original), and
        inline-script-only originals (no external ``src`` to preserve)."""
        if Path(rel).suffix.lower() not in (".html", ".htm"):
            return True
        if not original.strip():
            return True
        orig_srcs = _html_script_srcs(original)
        if not orig_srcs:
            return True
        return orig_srcs <= _html_script_srcs(new_content)

    @staticmethod
    def _confined(worktree: Path, target: Path) -> bool:
        root = worktree.resolve()
        try:
            return os.path.commonpath([str(root), str(target)]) == str(root)
        except ValueError:
            return False

    async def health_check(self) -> bool:
        return self.llm is not None
