"""Repair miner — distill "stuck -> resolved" builds into up-front knowledge.

The self-learning (LSO-style) edge: when a build's proof gate fails, a
*deterministic* repair (:func:`skyn3t.studio.proof_run.apply_deterministic_repairs`)
changes files, and the build subsequently passes proof / ships 'go', that pair
is durable knowledge — "what we should have known up front". LLM fix-loop
retries are deliberately NEVER mined: their diffs are one-off and not
distillable; only the deterministic repair keys are.

Flow:

  1. :func:`mine_repairs` reads a COMPLETED build manifest and emits one
     :class:`RepairFinding` per deterministic repair key that changed files on
     a build that later resolved (proof passed or verdict 'go'). A clean
     first-pass build (nothing repaired) yields nothing.
  2. :func:`persist_findings_as_lessons` stores each finding as ONE lesson row
     via the MemoryStore public API, deduped on the stable (stack, title) text
     — the same ``lesson_exists`` mechanism behind ``learning.dedupe_skipped``.
     Lesson rows carry a single text (no title/tags columns), so the volatile
     evidence stays on the finding and on the promoted skill body; the lesson
     text is the stable title, which is exactly what makes re-mined builds
     dedupe instead of duplicating. Initial score is the store default (0.0 —
     mid-range; ``grade_lesson`` moves it by outcome on later builds).
  3. :func:`record_and_promote` counts qualifying (stack, repair_key) pairs on
     the existing :class:`~skyn3t.intelligence.build_patterns.BuildPatternBoard`
     (only 'go' builds count) and, at :data:`REPAIR_PROMOTE_MIN_USES` distinct
     builds, mints a ``won-repair-<key>-<stack>`` advisory skill via the
     SkillLibrary public API — so the NEXT build gets the knowledge injected
     BEFORE it gets stuck. New skills start at the library default score
     (0.5 — mid-range; ``record_use`` moves it by outcome).
  4. :func:`demote_failing_repair_skills` is the outcome-based prune: a
     repair-mined skill with 3+ recorded uses and zero helpful gradings is
     demoted through the existing quarantine mechanic
     (:meth:`skyn3t.intelligence.skill_library.SkillLibrary.demote`), which
     excludes it from default injection while keeping the markdown on disk.

Everything is deterministic, offline, and dependency-free; every public
function degrades (logs + skips) instead of raising — mining must never break
a build (design rules #2, #5, #6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from skyn3t.intelligence.learning_loop import extract_gate_findings

try:  # structlog is a core dep, but never let logging break import.
    import structlog

    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover - defensive
    _log = None  # type: ignore[assignment]


def _info(event: str, **kw: Any) -> None:
    if _log is not None:
        try:
            _log.info(event, **kw)
        except Exception:  # pragma: no cover - defensive
            pass


# Distinct 'go' builds that must mine the same (stack, repair_key) pair before
# it promotes to a skill. Deliberately lower than the pattern board's own bar
# (PROMOTE_MIN_USES=4): a deterministic repair resolving the same stuck state
# twice is already a repeatable, mechanical fact, and the whole point is that
# the NEXT build gets the knowledge up front.
REPAIR_PROMOTE_MIN_USES = 2

# A promoted repair skill with this many recorded uses and zero helpful
# gradings is demoted out of injection (outcome-based pruning — knowledge does
# not age well).
REPAIR_DEMOTE_MIN_USES = 3

# repair key -> short human error class. Keys mirror the dict returned by
# apply_deterministic_repairs (proof_run.py); keys not listed here (e.g. the
# advisory `contrast_issues` lint layered on by the runner) are never mined.
REPAIR_ERROR_CLASSES: dict[str, str] = {
    "cjs_tests_renamed": "CommonJS test files refused under ESM package type",
    "node_test_script": "node --test cannot run a directory on Node 24",
    "npm_deps_added": "imported packages missing from package.json dependencies",
    "node_types_added": "node: builtin imports missing @types/node types",
    "npm_deps_sanitized": "package.json declares invalid dependency entries",
    "next_config_peers": "next.config feature needs a missing peer dependency",
    "imports_relinked": "relative imports resolve to the wrong file",
    "imports_scaffolded": "code imports local modules that were never written",
    "use_client_added": "interactive Next.js component missing 'use client' directive",
    "source_fences_stripped": "source files wrapped in markdown code fences",
    "bullet_wrappers_stripped": "source file is a rendered markdown bullet, not code",
    "path_alias_config": "'@/' import alias unresolved without jsconfig paths",
    "vitest_alias_config": "vitest cannot resolve the '@/' import alias",
    "ts_in_js_stripped": "TypeScript-only syntax inside .js files",
    "react_entrypoint_repaired": "React entrypoint does not match the Vite template",
    "lucide_icons_fixed": "hallucinated lucide-react icon names fail the build",
    "tauri_cargo_fixed": "hallucinated Tauri Cargo feature names fail the Rust build",
    "phaser_entrypoint_repaired": "index.html boots a stale shell instead of the game",
    "fastapi_entrypoint_unified": "stale FastAPI scaffold shadows the packaged app",
    "assets_reconciled": "referenced image assets missing from disk",
    "assets_missing_audio": "referenced audio assets missing from disk",
    "textures_loaded_added": "texture keys rendered but never loaded",
    "texture_placeholders_created": "missing texture art needed a synthesized placeholder",
    "python_imports_sorted": "generated Python fails Ruff import-order check",
    "python_formatted": "generated Python fails Ruff format check",
    "astro_estree_pin": "astro build fails on Node 24 ESM/CJS interop",
    "dangling_scripts_dropped": "package.json script invokes a Node file that was never written",
    "nextjs_metadata_added": "Next.js App Router layout exports no metadata",
}


@dataclass(slots=True)
class RepairFinding:
    """One "stuck -> resolved" fact mined from a completed build manifest."""

    stack: str
    error_class: str
    repair_key: str
    files_count: int
    example_evidence: str = ""
    changed: list[str] = field(default_factory=list)


def lesson_text(finding: RepairFinding) -> str:
    """The stable lesson title — also the dedupe key within a stack.

    Kept free of volatile per-build evidence (file names, counts) so the same
    (stack, repair_key) pair mined from DIFFERENT builds still dedupes to one
    lesson row via ``MemoryStore.lesson_exists``.
    """
    return f"When {finding.stack} {finding.error_class}: apply {finding.repair_key}"


def repair_skill_slug(stack: str, repair_key: str) -> str:
    """Slug of the promoted advisory skill for a (stack, repair_key) pair."""

    def _clean(text: str) -> str:
        return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")

    return f"won-repair-{_clean(repair_key)}-{_clean(stack or 'generic')}"


# ---- mining ---------------------------------------------------------------


def _resolved(manifest: Any, extra: dict[str, Any]) -> bool:
    """Whether the build left its stuck state: proof passed or verdict 'go'."""
    proof = extra.get("proof")
    if isinstance(proof, dict) and bool(proof.get("passed")):
        return True
    verdict = str(getattr(manifest, "verdict", "") or "").strip().lower()
    return verdict == "go"


def _as_files(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _merge(dst: dict[str, list[str]], repairs: Any) -> None:
    if not isinstance(repairs, dict):
        return
    for key, value in repairs.items():
        if key not in REPAIR_ERROR_CLASSES:
            continue
        files = _as_files(value)
        if not files:
            continue
        bucket = dst.setdefault(key, [])
        for f in files:
            if f not in bucket:
                bucket.append(f)


def _changed_repairs(extra: dict[str, Any]) -> dict[str, list[str]]:
    """Union of every deterministic repair that changed files, per manifest.

    Sources, in precedence order: the initial pass's full changed summary
    (``deterministic_repairs``), the post-proof consistency pass
    (``final_consistency_repairs``), each fix-loop iteration's full repair dict
    (``fix_attempt_<n>.repairs`` / ``runtime_self_heal.rounds[*].repairs``),
    and the legacy per-key recordings from the initial pass. Only repair keys
    with a known error class and at least one changed entry survive.
    """
    changed: dict[str, list[str]] = {}
    _merge(changed, extra.get("deterministic_repairs"))
    _merge(changed, extra.get("final_consistency_repairs"))
    for key, value in extra.items():
        if not (isinstance(key, str) and key.startswith("fix_attempt_")):
            continue
        if isinstance(value, dict):
            _merge(changed, value.get("repairs"))
    heal = extra.get("runtime_self_heal")
    if isinstance(heal, dict):
        rounds = heal.get("rounds")
        if isinstance(rounds, list):
            for rnd in rounds:
                if isinstance(rnd, dict):
                    _merge(changed, rnd.get("repairs"))
    legacy = {k: extra.get(k) for k in REPAIR_ERROR_CLASSES if k in extra}
    _merge(changed, legacy)
    return changed


def _example_evidence(extra: dict[str, Any]) -> str:
    """One short line from the build's gate findings, if any were captured."""
    try:
        findings = extract_gate_findings(extra)
    except Exception:  # noqa: BLE001 - evidence is nice-to-have, never fatal
        findings = []
    return findings[0] if findings else ""


def mine_repairs(manifest: Any, *, stack: str | None = None) -> list[RepairFinding]:
    """Extract "stuck -> resolved" findings from a COMPLETED build manifest.

    ``manifest`` is duck-typed (``.extra``, ``.verdict``, ``.stack``) so both
    :class:`~skyn3t.studio.manifest.BuildManifest` and lightweight test doubles
    work. ``stack`` overrides the manifest's own stack label (the runner passes
    the plan's). Returns one finding per deterministic repair that changed
    files on a build that later resolved; ``[]`` for clean first-pass builds,
    unresolved builds, and any malformed input — mining never raises.
    """
    try:
        extra = getattr(manifest, "extra", None)
        if not isinstance(extra, dict):
            return []
        if not _resolved(manifest, extra):
            return []
        changed = _changed_repairs(extra)
        if not changed:
            return []
        build_stack = (stack or getattr(manifest, "stack", "") or "generic")
        build_stack = str(build_stack).strip() or "generic"
        evidence = _example_evidence(extra)
        return [
            RepairFinding(
                stack=build_stack,
                error_class=REPAIR_ERROR_CLASSES[key],
                repair_key=key,
                files_count=len(files),
                example_evidence=evidence,
                changed=list(files),
            )
            for key, files in sorted(changed.items())
        ]
    except Exception:  # noqa: BLE001 - mining must never break a build
        return []


# ---- persist as lessons ---------------------------------------------------


async def persist_findings_as_lessons(
    findings: list[RepairFinding],
    store: Any,
    *,
    stack: str,
    source_build: str | None = None,
) -> list[int]:
    """Store each finding as one deduped lesson row; return new lesson ids.

    Reuses the store's public ``lesson_exists`` / ``add_lesson`` — the same
    capture-side dedupe mechanism the learning loop's ``learning.dedupe_skipped``
    path uses (same event emitted here for observability parity). A store
    failure on one finding logs and skips that finding, never the rest.
    """
    if store is None or not findings:
        return []
    ids: list[int] = []
    skipped = 0
    for finding in findings:
        text = lesson_text(finding)
        try:
            exists = getattr(store, "lesson_exists", None)
            if exists is not None and bool(await exists(stack, text)):
                skipped += 1
                continue
            ids.append(
                int(await store.add_lesson(stack, "", text, source_build=source_build))
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
            _info("repair_miner.store_add_failed", error=str(exc))
    if skipped:
        _info("learning.dedupe_skipped", stack=stack, skipped=skipped)
    if ids:
        _info("repair_miner.captured", stack=stack, count=len(ids))
    return ids


# ---- promote to skills ----------------------------------------------------


def _skill_body(finding: RepairFinding, *, uses: int) -> str:
    changed = ", ".join(finding.changed[:8]) or "(files not recorded)"
    evidence = finding.example_evidence or "(no gate finding captured)"
    return (
        f"A **{finding.stack}** build got stuck — {finding.error_class} — and the "
        f"deterministic repair `{finding.repair_key}` resolved it "
        f"({finding.files_count} file(s) changed); proof passed afterwards. Seen on "
        f"{uses} shipped builds, so prevent it UP FRONT instead of waiting for the "
        f"fix loop: watch for this defect class while generating and apply the "
        f"`{finding.repair_key}` fix before the first proof.\n\n"
        f"Evidence from the stuck build: {evidence}\n"
        f"Changed by the repair: {changed}"
    )


def record_and_promote(
    findings: list[RepairFinding],
    patterns: Any,
    skills: Any,
    *,
    score: float,
    go: bool,
    example: str | None = None,
) -> list[Any]:
    """Count qualifying repairs on the pattern board; promote recurrences.

    Only 'go' builds count — a repair that did not ship teaches nothing about
    what to do up front. Each finding records one ``{"repair": key}`` shape on
    the existing BuildPatternBoard (same ``uses``/win semantics as build
    shapes); at :data:`REPAIR_PROMOTE_MIN_USES` distinct builds the pair mints
    a ``won-repair-<key>-<stack>`` skill via ``SkillLibrary.add`` (idempotent
    by slug, mirroring ``maybe_promote_pattern``). Returns the skills promoted
    this call. Never raises.
    """
    if patterns is None or skills is None or not findings or not go:
        return []
    promoted: list[Any] = []
    for finding in findings:
        try:
            rec = patterns.record(
                finding.stack,
                {"repair": finding.repair_key},
                float(score),
                example=example,
            )
        except Exception as exc:  # noqa: BLE001
            _info("repair_miner.pattern_record_failed", error=str(exc))
            continue
        if int(getattr(rec, "uses", 0)) < REPAIR_PROMOTE_MIN_USES:
            continue
        slug = repair_skill_slug(finding.stack, finding.repair_key)
        try:
            if skills.get(slug) is not None:
                continue
            promoted.append(
                skills.add(
                    title=lesson_text(finding),
                    body=_skill_body(finding, uses=int(getattr(rec, "uses", 0))),
                    stack=finding.stack,
                    tags=[finding.stack, "repair-mined", finding.repair_key],
                    source="repair-mined",
                    slug=slug,
                )
            )
            _info("repair_miner.promoted", slug=slug, stack=finding.stack)
        except Exception as exc:  # noqa: BLE001
            _info("repair_miner.promote_failed", slug=slug, error=str(exc))
    return promoted


# ---- prune by outcome -----------------------------------------------------


def demote_failing_repair_skills(
    skills: Any, *, min_uses: int = REPAIR_DEMOTE_MIN_USES
) -> list[str]:
    """Demote repair-mined skills with ``min_uses``+ uses and zero helpfuls.

    Uses the library's existing quarantine demotion (the skill stays on disk
    but leaves default injection). Returns the demoted slugs. Never raises.
    """
    if skills is None:
        return []
    try:
        all_skills = skills.all()
    except Exception:  # noqa: BLE001
        return []
    demoted: list[str] = []
    for sk in all_skills:
        if getattr(sk, "source", "") != "repair-mined":
            continue
        if int(getattr(sk, "uses", 0) or 0) < min_uses:
            continue
        if int(getattr(sk, "helpful", 0) or 0) > 0:
            continue
        slug = str(getattr(sk, "slug", "") or "")
        if not slug:
            continue
        try:
            if skills.demote(slug) is not None:
                demoted.append(slug)
                _info("repair_miner.demoted", slug=slug, uses=int(sk.uses))
        except Exception as exc:  # noqa: BLE001
            _info("repair_miner.demote_failed", slug=slug, error=str(exc))
    return demoted
