# skyn3t/studio/intent_score.py
"""Intent-honest scoring (Spec 2).

Judges whether a delivered project matches the BRIEF'S INTENT, not merely whether
it builds. Closes the documented gap where a hollow scaffold that compiles passes
the proof-run and reads 100/go even though it isn't what the brief asked for (the
reviewer heuristic is purely structural — it never reads the brief content).

Two signals, both degrade gracefully (design rule: offline-first):
  * heuristic (always, offline, deterministic): the fraction of the brief's
    salient domain terms that actually appear in the delivered source/markup.
  * LLM judge (only when a real backend is wired): a calibrated 0..100 intent
    score over a content digest of the delivered tree.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_TERM_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{2,}")

# Words that carry no domain intent: function words + generic build/software
# vocabulary. Stripped from a brief so only the meaningful nouns/verbs remain.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "you", "your", "our", "its",
    "from", "into", "over", "under", "out", "off", "are", "was", "were", "has",
    "have", "had", "not", "but", "can", "should", "would", "will", "they", "them",
    "their", "there", "here", "what", "when", "where", "which", "who", "how", "why",
    "all", "any", "each", "both", "either", "than", "then", "very", "just", "only",
    # generic build/software vocabulary
    "app", "apps", "application", "website", "web", "site", "sites", "page", "pages",
    "webpage", "webapp", "build", "builds", "building", "create", "creates",
    "creating", "make", "makes", "making", "simple", "basic", "small", "quick",
    "nice", "clean", "modern", "good", "great", "project", "program", "programme",
    "software", "tool", "tools", "system", "platform", "using", "use", "uses",
    "used", "generate", "please", "want", "wants", "need", "needs", "like", "let",
    "lets", "allow", "allows", "add", "adds", "display", "show", "shows", "support",
    "user", "users", "data", "feature", "features", "functionality", "ability",
    "able", "new", "one", "two", "some", "more", "most", "also", "via", "per", "etc",
})

_SOURCE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".html", ".htm", ".css",
    ".scss", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".vue", ".svelte",
    ".go", ".rs", ".java", ".rb", ".php",
)

_SKIP_DIRS = frozenset({
    "node_modules", ".git", "dist", "build", ".preview", "__pycache__", ".venv",
    ".next", ".cache", "coverage", "vendor",
})

_MAX_SCAN_BYTES = 200_000
_MAX_DIGEST_BYTES = 6000


@dataclass(slots=True)
class IntentResult:
    score: float                      # 0..100 intent-match
    method: str                       # "heuristic" | "llm+heuristic" | "skipped"
    terms: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def salient_terms(brief: str, *, limit: int = 24) -> list[str]:
    """Domain-bearing terms from a brief: content words minus stopwords + generic
    build vocabulary, de-duplicated, order-preserving."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _TERM_RE.finditer((brief or "").lower()):
        w = m.group(0)
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _iter_source_files(project_dir: Path):
    for p in sorted(project_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if set(p.parts) & _SKIP_DIRS:
            continue
        yield p


def _delivered_tokens(project_dir) -> set[str]:
    """Lower-cased token set across the delivered source/markup, bounded."""
    pdir = Path(project_dir)
    if not pdir.is_dir():
        return set()
    budget = _MAX_SCAN_BYTES
    toks: set[str] = set()
    for p in _iter_source_files(pdir):
        if budget <= 0:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:budget]
        except OSError:
            continue
        budget -= len(text)
        for m in _TERM_RE.finditer(text.lower()):
            toks.add(m.group(0))
    return toks


def _content_digest(project_dir, *, max_bytes: int = _MAX_DIGEST_BYTES) -> str:
    """A bounded excerpt of actual file contents for the LLM judge."""
    pdir = Path(project_dir)
    if not pdir.is_dir():
        return ""
    chunks: list[str] = []
    budget = max_bytes
    for p in _iter_source_files(pdir):
        if budget <= 0:
            break
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        snippet = text[:1500]
        chunks.append(f"# {p.name}\n{snippet}")
        budget -= len(snippet)
    return "\n\n".join(chunks)[:max_bytes]


def _hit(term: str, toks: set[str]) -> bool:
    """A brief term matches the delivered tokens by exact, singular-stem, or
    (for longer terms) shared-prefix membership (color↔coloring, page↔pages)."""
    if term in toks:
        return True
    s = term[:-1] if term.endswith("s") and len(term) > 3 else term
    if s in toks:
        return True
    if len(term) >= 4:
        for tok in toks:
            if len(tok) >= 4 and (tok.startswith(term) or term.startswith(tok)):
                return True
    return False


def score_intent(brief: str, project_dir, stack: str = "", *,
                 llm_score: float | None = None) -> IntentResult:
    """Heuristic intent-match: fraction of the brief's salient domain terms that
    actually appear in the delivered source/markup, optionally blended with a
    precomputed LLM judge score. Pure + offline; never raises."""
    terms = salient_terms(brief)
    if not terms:
        return IntentResult(score=100.0, method="skipped",
                            detail={"reason": "no salient terms in brief"})
    toks = _delivered_tokens(project_dir)
    matched = [t for t in terms if _hit(t, toks)]
    missing = [t for t in terms if t not in matched]
    heuristic = round(100.0 * len(matched) / len(terms), 2)
    result = IntentResult(score=heuristic, method="heuristic", terms=terms,
                          matched=matched, missing=missing,
                          detail={"heuristic_score": heuristic})
    if llm_score is not None:
        result.score = round(0.5 * heuristic + 0.5 * float(llm_score), 2)
        result.method = "llm+heuristic"
        result.detail["llm_score"] = float(llm_score)
    return result


def intent_gate(code_backend: str, intent_score: float, floor: float) -> bool:
    """Whether a delivered project clears the intent floor for a 'go'. Stub
    backends are exempt (a stub's minimal scaffold is acceptable degraded output,
    mirroring the substance gate) — only a real model that ignored the brief is
    failed."""
    return code_backend == "stub" or intent_score >= floor


async def llm_intent_score(brief: str, project_dir, *, llm) -> float | None:
    """Calibrated separate LLM judge: does the delivered CONTENT satisfy the
    brief's intent? Returns 0..100, or None (no/stub LLM, or any failure)."""
    if llm is None or getattr(llm, "backend", "stub") == "stub":
        return None
    digest = _content_digest(project_dir)
    prompt = (
        "You are grading whether a generated software project satisfies the "
        "USER BRIEF's intent — judge the actual CONTENT, not whether it merely "
        "compiles. A generic placeholder that ignores the brief scores low.\n"
        'Reply ONLY with JSON {"score": <0-100>, "missing": ["..."]}.\n\n'
        f"BRIEF: {brief}\n\nDELIVERED CONTENT (excerpt):\n{digest}\n"
    )
    try:
        from skyn3t.core.model_router import Tier
        res = await llm.complete(prompt, tier=Tier.STRONG, json_mode=True,
                                 max_tokens=256)
        data = json.loads(res.text)
        return max(0.0, min(100.0, float(data.get("score"))))
    except Exception:  # noqa: BLE001 - judge is best-effort; degrade to heuristic
        return None
