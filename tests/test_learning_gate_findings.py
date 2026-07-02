"""Advisory-gate findings become lessons — even on a 'go'.

The end-of-build gates (seo/mcp_check/rag_check/liveness) record findings and
feed ONE repair but never flip the verdict — so before this path, a passing
build whose gate caught a real defect (an SEO hole, an unwired LLM seam, a dead
route) taught the system nothing, and the same defect class recurred build
after build. Mirrors tests/test_learning_proof_errors.py, which closed the same
loop for hard proof failures.
"""

from __future__ import annotations

from skyn3t.intelligence.learning_loop import (
    _summarize_outcome,
    extract_gate_findings,
)

# A manifest.extra as the runner records it: two gate verdicts with issues, one
# skipped gate (must contribute nothing), and a liveness report with dead routes.
_EXTRA = {
    "rag_check": {
        "ok": False,
        "skipped": False,
        "issues": [
            "the app called the LLM seam but the prompt did NOT contain the "
            "retrieved context (the just-ingested marker chunk is absent)",
        ],
    },
    "seo": {"ok": False, "skipped": False, "issues": ["page has no <title>"]},
    "mcp_check": {"ok": False, "skipped": True, "issues": [],
                  "reason": "mcp SDK not importable"},
    "liveness": {"skipped": False, "dead_routes": ["/about", "/pricing"]},
}


def test_extract_flattens_and_prefixes_gate_issues():
    findings = extract_gate_findings(_EXTRA)
    joined = "\n".join(findings)
    assert "rag_check: the app called the LLM seam" in joined
    assert "seo: page has no <title>" in joined
    assert "liveness: route(s) dead after repair — /about, /pricing" in joined
    # Lessons stay short/one-line — no multi-line gate dump leaks in.
    assert all("\n" not in f and len(f) <= 180 for f in findings)


def test_skipped_gate_contributes_nothing():
    # A degrade-open skip (gate could not run) must never mint an avoid-rule.
    findings = extract_gate_findings(_EXTRA)
    assert not any(f.startswith("mcp_check:") for f in findings)
    only_skips = {"rag_check": {"skipped": True, "issues": ["ghost"]},
                  "liveness": {"skipped": True, "dead_routes": ["/x"]}}
    assert extract_gate_findings(only_skips) == []


def test_extract_caps_per_gate_and_survives_garbage():
    noisy = {"seo": {"skipped": False, "issues": [f"issue {i}" for i in range(10)]}}
    assert len(extract_gate_findings(noisy)) == 3  # capped per gate
    assert extract_gate_findings(None) == []
    assert extract_gate_findings({"seo": "not-a-dict", "liveness": 7}) == []
    assert extract_gate_findings({"rag_check": {"skipped": False, "issues": "nope"}}) == []


def test_gate_findings_become_lessons_on_a_go_build():
    # The whole point: the verdict is 'go', and the lesson still lands.
    build = {
        "stack": "rag", "verdict": "go", "score": 92, "gaps": [],
        "gate_findings": extract_gate_findings(_EXTRA),
    }
    lessons = _summarize_outcome(build)
    joined = "\n".join(lessons)
    assert "rag: gate flagged — rag_check: the app called the LLM seam" in joined
    assert all(len(ls) <= 200 for ls in lessons)


def test_no_gate_findings_changes_nothing():
    build = {"stack": "react", "verdict": "go", "score": 95, "gaps": []}
    lessons = _summarize_outcome(build)
    assert not any("gate flagged" in ls for ls in lessons)
