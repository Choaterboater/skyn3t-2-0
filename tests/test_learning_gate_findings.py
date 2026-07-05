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

_HARD_EXTRA = {
    "verifier_gate": "verify_build failed: npm run build exited 1",
    "critic_gate": "critic blocked: browser-exposed API key",
    "intent_gate": "intent match 22 < 34 floor",
    "headless_gate_gate": "headless gate failed after repair: no pure src/sim.js core",
    "game_visual_gate": "visual check failed: empty play field",
    "qa_playtest_gate": "qa playtest failed: sprites never rendered",
    "game_visual": {
        "ok": False,
        "skipped": False,
        "issues": ["empty play field", "sprites too small"],
        "gap": "the running game does not look right: empty board",
    },
    "qa_playtest": {
        "ok": False,
        "skipped": False,
        "console_errors": ["ReferenceError: dist is not defined"],
        "missing_sprite_roles": ["player", "enemy"],
        "gaps": ["generated sprites exist but are never rendered on screen"],
    },
}


def test_extract_flattens_and_prefixes_gate_issues():
    findings = extract_gate_findings(_EXTRA)
    joined = "\n".join(findings)
    assert "rag_check: the app called the LLM seam" in joined
    assert "seo: page has no <title>" in joined
    assert "liveness: route(s) dead after repair — /about, /pricing" in joined
    # Lessons stay short/one-line — no multi-line gate dump leaks in.
    assert all("\n" not in f and len(f) <= 180 for f in findings)


def test_extract_hard_quality_gate_failures():
    findings = extract_gate_findings(_HARD_EXTRA)
    joined = "\n".join(findings)
    assert "verifier_gate: verify_build failed" in joined
    assert "critic_gate: critic blocked" in joined
    assert "intent_gate: intent match 22" in joined
    assert "headless_gate_gate: headless gate failed" in joined
    assert "game_visual_gate: visual check failed: empty play field" in joined
    assert "qa_playtest_gate: qa playtest failed: sprites never rendered" in joined
    assert "game_visual: the running game does not look right" in joined
    assert "game_visual: empty play field" in joined
    assert "qa_playtest: ReferenceError: dist is not defined" in joined
    assert "qa_playtest: missing sprite role(s) not rendered" in joined
    assert all("\n" not in f and len(f) <= 190 for f in findings)


def test_extract_product_quality_gate_failures_and_skips():
    extra = {
        "finance_sanity": {
            "ok": False,
            "skipped": False,
            "issues": [
                "cash must be non-negative",
                "positions must be non-negative",
                "portfolio totals must balance",
                "ignored fourth finance issue",
            ],
        },
        "workflow_depth": {
            "ok": False,
            "skipped": False,
            "missing": ["audit_log"],
            "issues": [
                "audit_log: mentioned concept has no backing route/api/state",
                "trade_history: mentioned concept has no backing route/api/state",
                "risk_controls: mentioned concept has no backing route/api/state",
                "ignored fourth workflow issue",
            ],
        },
    }
    findings = extract_gate_findings(extra)
    joined = "\n".join(findings)
    assert "finance_sanity: cash must be non-negative" in joined
    assert "workflow_depth: audit_log: mentioned concept has no backing route/api/state" in joined
    assert "ignored fourth finance issue" not in joined
    assert "ignored fourth workflow issue" not in joined

    skipped = {
        "finance_sanity": {"ok": True, "skipped": True, "issues": ["ghost finance"]},
        "workflow_depth": {
            "ok": True,
            "skipped": True,
            "missing": ["ghost"],
            "issues": ["ghost workflow"],
        },
    }
    assert extract_gate_findings(skipped) == []


def test_skipped_gate_contributes_nothing():
    # A degrade-open skip (gate could not run) must never mint an avoid-rule.
    findings = extract_gate_findings(_EXTRA)
    assert not any(f.startswith("mcp_check:") for f in findings)
    only_skips = {"rag_check": {"skipped": True, "issues": ["ghost"]},
                  "game_visual": {"skipped": True, "issues": ["empty"]},
                  "qa_playtest": {"skipped": True, "gaps": ["error"]},
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


def test_hard_gate_findings_replace_generic_no_go_lesson():
    build = {
        "stack": "phaser",
        "verdict": "no_go",
        "score": 49,
        "gaps": [],
        "proof_errors": [],
        "gate_findings": extract_gate_findings(_HARD_EXTRA),
    }
    lessons = _summarize_outcome(build)
    joined = "\n".join(lessons)
    assert "phaser: gate flagged — game_visual_gate: visual check failed" in joined
    assert "phaser: gate flagged — qa_playtest_gate: qa playtest failed" in joined
    assert "phaser: gate flagged — qa_playtest: ReferenceError: dist is not defined" in joined
    assert not any("re-check the plan" in lesson for lesson in lessons)


def test_no_gate_findings_changes_nothing():
    build = {"stack": "react", "verdict": "go", "score": 95, "gaps": []}
    lessons = _summarize_outcome(build)
    assert not any("gate flagged" in ls for ls in lessons)
