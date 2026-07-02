"""Toolchain preflight (ADDING_A_STACK step 10): the selector never picks a
stack this machine cannot build.

A heuristic/LLM choice whose toolchain executable is missing (npm for the node
family, swift for SwiftPM) is demoted to the nearest BUILDABLE stack with the
reason recorded in the rationale; python-family and static stacks need no
toolchain; an explicit PIN is user intent and is never demoted (the proof's
logged soft-skip covers it).
"""

from __future__ import annotations

import asyncio

from skyn3t.studio import stack_selector as ss


def _select(brief: str, **kw):
    return asyncio.run(ss.select_stack(brief, **kw))


def test_available_toolchain_keeps_the_choice(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    assert _select("a react dashboard for my metrics").stack == "react"


def test_missing_npm_demotes_node_web_to_static(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda exe: None)
    choice = _select("a react dashboard for my metrics")
    assert choice.stack == "static"
    assert "npm" in choice.rationale and "react" in choice.rationale
    assert choice.confidence <= 0.5


def test_missing_swift_walks_the_fallback_chain(monkeypatch):
    # swift missing, npm present -> the desktop intent survives via tauri.
    monkeypatch.setattr(
        ss.shutil, "which", lambda exe: "/usr/bin/npm" if exe == "npm" else None)
    assert _select("a swiftui macos app").stack == "tauri"
    # nothing installed -> the terminal python fallback.
    monkeypatch.setattr(ss.shutil, "which", lambda exe: None)
    assert _select("a swiftui macos app").stack == "python"


def test_express_demotes_to_the_python_api_equivalent(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda exe: None)
    assert _select("a node.js express api server").stack == "fastapi"


def test_python_family_needs_no_toolchain(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda exe: None)
    assert _select("a rag app to chat with my documents").stack == "rag"
    assert _select("an agent that sends me a daily briefing").stack == "workflow"


def test_pin_is_never_demoted(monkeypatch):
    monkeypatch.setattr(ss.shutil, "which", lambda exe: None)
    choice = _select("anything at all", pin="react")
    assert choice.stack == "react" and choice.method == "pin"


def test_toolchain_available_is_true_for_unlisted_stacks():
    assert ss.toolchain_available("rag")
    assert ss.toolchain_available("static")
    assert ss.toolchain_available("python")
