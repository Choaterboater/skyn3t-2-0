# skyn3t/studio/visual_check.py
"""Advisory visual and workspace-layout inspection (Spec 3, Slice 3).

A clean layout audit without a vision model soft-skips. An audit issue remains a
repairable advisory verdict even when vision is unavailable. Missing Playwright
still soft-skips, and this module never turns visual evidence into a build gate
or raises to its caller.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from skyn3t.core.events import EventType
from skyn3t.exec_paths import resolve_executable
from skyn3t.security.secrets import filter_env
from skyn3t.studio.layout_profiles import LayoutProfile, profile_from_payload

VisionFn = Callable[[str, str], str]  # (image_path, prompt) -> raw model text

log = structlog.get_logger(__name__)

# Common "begin/continue play" affordance text — used by _dom_start_click for
# accessibility-tree/DOM matching BEFORE any vision call is attempted.
_START_TEXT_PATTERN = re.compile(r"\b(start|play|begin|continue|run|go|enter)\b", re.IGNORECASE)

# A small, widely-available, cheap vision-capable model on OpenRouter; override
# with settings.vision_model.
_DEFAULT_VISION_MODEL = "openai/gpt-4o-mini"

_LAYOUT_METRICS_JS = """() => {
  const viewportWidth = Math.max(0, Number(window.innerWidth) || 0);
  const viewportHeight = Math.max(0, Number(window.innerHeight) || 0);
  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden"
      && Number(style.opacity || 1) > 0 && rect.width > 2 && rect.height > 2
      && rect.bottom > 0 && rect.right > 0
      && rect.top < viewportHeight && rect.left < viewportWidth;
  };
  const clipped = (el) => {
    const rect = el.getBoundingClientRect();
    const left = Math.max(0, rect.left);
    const top = Math.max(0, rect.top);
    const right = Math.min(viewportWidth, rect.right);
    const bottom = Math.min(viewportHeight, rect.bottom);
    return {
      left, top, right, bottom,
      width: Math.max(0, right - left),
      height: Math.max(0, bottom - top),
    };
  };
  const semanticRoots = Array.from(document.querySelectorAll(
    "main, [role='main'], [role='application']"
  )).filter(visible);
  const applicationRoots = Array.from(document.querySelectorAll("#app, #root"))
    .filter(visible);
  const rootCandidates = semanticRoots.length ? semanticRoots : applicationRoots;
  if (!rootCandidates.length && document.body && visible(document.body)) {
    rootCandidates.push(document.body);
  }
  const root = rootCandidates.reduce((largest, candidate) => {
    if (!largest) return candidate;
    const area = clipped(candidate).width * clipped(candidate).height;
    const largestArea = clipped(largest).width * clipped(largest).height;
    return area > largestArea ? candidate : largest;
  }, null);
  const railCandidates = Array.from(document.querySelectorAll("nav, aside")).filter(visible);
  let leftRail = 0;
  let rightRail = 0;
  for (const rail of railCandidates) {
    const rect = clipped(rail);
    if (rect.height < viewportHeight * 0.40 || rect.width > viewportWidth * 0.40) {
      continue;
    }
    if (rect.left <= viewportWidth * 0.08) leftRail = Math.max(leftRail, rect.right);
    if (rect.right >= viewportWidth * 0.92) {
      rightRail = Math.max(rightRail, viewportWidth - rect.left);
    }
  }
  const workspaceLeft = Math.min(viewportWidth, leftRail);
  const workspaceRight = Math.max(workspaceLeft, viewportWidth - rightRail);
  const workspaceWidth = Math.max(1, workspaceRight - workspaceLeft);
  const workspaceArea = Math.max(1, workspaceWidth * viewportHeight);
  const descendants = root ? Array.from(root.querySelectorAll("*")).filter(visible) : [];
  const contentRects = [];
  for (const el of descendants) {
    const tag = el.tagName.toLowerCase();
    if (["script", "style", "meta", "link", "br"].includes(tag)) continue;
    if (el.closest("nav, aside, header, footer")) continue;
    const rect = clipped(el);
    const left = Math.max(workspaceLeft, rect.left);
    const right = Math.min(workspaceRight, rect.right);
    if (right <= left || rect.height <= 0) continue;
    const style = window.getComputedStyle(el);
    const directText = Array.from(el.childNodes).some((node) =>
      node.nodeType === Node.TEXT_NODE && Boolean((node.textContent || "").trim())
    );
    const semantic = [
      "table", "ul", "ol", "form", "canvas", "svg", "img", "input", "select",
      "textarea", "button",
    ].includes(tag);
    const painted = style.backgroundColor !== "rgba(0, 0, 0, 0)"
      && style.backgroundColor !== "transparent";
    const paintedLeaf = painted && !Array.from(el.children).some(visible);
    if (directText || semantic || paintedLeaf) {
      contentRects.push({
        left, right, top: rect.top, bottom: rect.bottom,
        width: right - left, height: rect.height,
      });
    }
  }
  let fillRatio = 0;
  if (contentRects.length) {
    const left = Math.min(...contentRects.map((rect) => rect.left));
    const right = Math.max(...contentRects.map((rect) => rect.right));
    const top = Math.min(...contentRects.map((rect) => rect.top));
    const bottom = Math.max(...contentRects.map((rect) => rect.bottom));
    fillRatio = Math.min(1, Math.max(0, ((right - left) * (bottom - top)) / workspaceArea));
  }
  const cardGroups = new Map();
  for (const el of descendants) {
    const rect = clipped(el);
    if (rect.width < 120 || rect.height < 70 || rect.width > workspaceWidth * 0.80) continue;
    const style = window.getComputedStyle(el);
    const radius = Number.parseFloat(style.borderRadius) || 0;
    const painted = style.backgroundColor !== "rgba(0, 0, 0, 0)"
      && style.backgroundColor !== "transparent";
    const bordered = (Number.parseFloat(style.borderTopWidth) || 0) > 0;
    if (radius < 4 || (!painted && !bordered)) continue;
    const widthBucket = Math.round(rect.width / 40);
    const heightBucket = Math.round(rect.height / 40);
    const key = `${widthBucket}:${heightBucket}:${style.backgroundColor}:${style.borderRadius}`;
    const current = cardGroups.get(key) || {count: 0, area: 0};
    current.count += 1;
    current.area += rect.width * rect.height;
    cardGroups.set(key, current);
  }
  let repeatedCards = 0;
  let repeatedCardArea = 0;
  let qualifyingCards = 0;
  let qualifyingCardArea = 0;
  for (const group of cardGroups.values()) {
    if (group.count > repeatedCards
        || (group.count === repeatedCards && group.area > repeatedCardArea)) {
      repeatedCards = group.count;
      repeatedCardArea = group.area;
    }
    if (group.count >= 4 && group.area > qualifyingCardArea) {
      qualifyingCards = group.count;
      qualifyingCardArea = group.area;
    }
  }
  if (qualifyingCards) {
    repeatedCards = qualifyingCards;
    repeatedCardArea = qualifyingCardArea;
  }
  const dataSelectors = [
    "table tbody tr", "[role='row']", "ul", "ol", "[role='list']",
    "canvas", "svg", "input", "select", "textarea",
    "[role='textbox']", "[role='combobox']", "[role='checkbox']",
  ];
  const dataBearing = new Set();
  for (const selector of dataSelectors) {
    for (const el of document.querySelectorAll(selector)) {
      if (visible(el)) dataBearing.add(el);
    }
  }
  return {
    viewport: {width: viewportWidth, height: viewportHeight},
    fill_ratio: Number.isFinite(fillRatio) ? fillRatio : 0,
    repeated_cards: Number.isFinite(repeatedCards) ? repeatedCards : 0,
    card_area_ratio: Number.isFinite(repeatedCardArea / workspaceArea)
      ? Math.min(1, repeatedCardArea / workspaceArea) : 0,
    data_bearing_count: dataBearing.size,
  };
}"""


@dataclass(slots=True)
class LayoutAudit:
    profile: str
    viewport: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, float | int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    fix_hint: str = ""
    skipped: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VisualVerdict:
    matches: bool = False
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)
    fix_hint: str = ""
    skipped: bool = False
    reason: str = ""
    layout_audit: LayoutAudit | None = None
    advisory_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_number(
    value: object,
    *,
    low: float = 0.0,
    high: float = 1_000_000.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return low
    number = float(value)
    if not math.isfinite(number):
        return low
    return min(high, max(low, number))


def normalize_layout_metrics(raw: object) -> dict[str, float | int | bool]:
    data = raw if isinstance(raw, dict) else {}
    viewport_value = data.get("viewport")
    viewport = viewport_value if isinstance(viewport_value, dict) else {}
    width = int(_safe_number(viewport.get("width"), high=10_000.0))
    height = int(_safe_number(viewport.get("height"), high=10_000.0))
    return {
        "viewport_width": width,
        "viewport_height": height,
        "fill_ratio": _safe_number(data.get("fill_ratio"), high=1.0),
        "repeated_cards": int(_safe_number(data.get("repeated_cards"), high=10_000.0)),
        "card_area_ratio": _safe_number(data.get("card_area_ratio"), high=1.0),
        "data_bearing_count": int(
            _safe_number(data.get("data_bearing_count"), high=10_000.0)
        ),
    }


def _has_complete_layout_metrics(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    viewport = raw.get("viewport")
    if not isinstance(viewport, dict):
        return False
    values = [
        viewport.get("width"),
        viewport.get("height"),
        raw.get("fill_ratio"),
        raw.get("repeated_cards"),
        raw.get("card_area_ratio"),
        raw.get("data_bearing_count"),
    ]
    return all(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        for value in values
    )


def _workspace_policy(
    profile: LayoutProfile,
    metrics: dict[str, float | int | bool],
) -> LayoutAudit:
    issues: list[str] = []
    if float(metrics["fill_ratio"]) < 0.62:
        issues.append(
            "Desktop workspace is under-filled after accounting for navigation rails."
        )
    if (
        int(metrics["repeated_cards"]) >= 4
        and float(metrics["card_area_ratio"]) > 0.50
    ):
        issues.append(
            "Desktop workspace is dominated by similarly sized cards."
        )
    viewport = {
        "width": int(metrics["viewport_width"]),
        "height": int(metrics["viewport_height"]),
    }
    fix_hint = ""
    if issues:
        fix_hint = (
            "Use the desktop workspace for a table/detail or split-pane composition "
            "instead of a narrow uniform-card layout."
        )
    return LayoutAudit(
        profile=profile.name,
        viewport=viewport,
        metrics={key: value for key, value in metrics.items() if not isinstance(value, bool)},
        issues=issues,
        fix_hint=fix_hint,
    )


def assess_layout(profile: LayoutProfile, raw: object) -> LayoutAudit:
    metrics = normalize_layout_metrics(raw)
    viewport = {
        "width": int(metrics["viewport_width"]),
        "height": int(metrics["viewport_height"]),
    }
    if not profile.audit_enabled:
        return LayoutAudit(
            profile.name,
            viewport=viewport,
            metrics=metrics,
            skipped=True,
            reason=f"{profile.name} profile audit exemption",
        )
    if (
        not _has_complete_layout_metrics(raw)
        or int(metrics["viewport_width"]) <= 0
        or int(metrics["viewport_height"]) <= 0
    ):
        return LayoutAudit(
            profile.name,
            viewport=viewport,
            metrics=metrics,
            skipped=True,
            reason="missing layout metrics",
        )
    if int(metrics["viewport_width"]) < 1024:
        return LayoutAudit(
            profile.name,
            viewport=viewport,
            metrics=metrics,
            skipped=True,
            reason="mobile viewport",
        )
    return _workspace_policy(profile, metrics)


def _merge_layout_audit(vision: VisualVerdict, audit: LayoutAudit) -> VisualVerdict:
    if audit.skipped or not audit.issues:
        return VisualVerdict(
            matches=vision.matches,
            confidence=vision.confidence,
            issues=list(vision.issues),
            fix_hint=vision.fix_hint,
            skipped=vision.skipped,
            reason=vision.reason,
            layout_audit=audit,
            advisory_only=vision.advisory_only,
        )
    return VisualVerdict(
        matches=False,
        confidence=vision.confidence,
        issues=[*audit.issues, *vision.issues],
        fix_hint=audit.fix_hint or vision.fix_hint,
        skipped=False,
        reason="",
        layout_audit=audit,
        advisory_only=vision.skipped or vision.matches,
    )


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def capture_visual_evidence(
    url: str,
    out_path: str,
    *,
    timeout_ms: int = 8000,
    audited_desktop: bool = False,
) -> tuple[str | None, dict[str, object] | None]:
    """Capture one screenshot and optional aggregate desktop geometry. Never raises."""
    if not playwright_available():
        return None, None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = (
                    browser.new_page(viewport={"width": 1440, "height": 900})
                    if audited_desktop
                    else browser.new_page()
                )
                page.goto(url, timeout=timeout_ms, wait_until="load")
                try:
                    page.wait_for_load_state("networkidle", timeout=min(2500, timeout_ms))
                except Exception:  # noqa: BLE001 - apps may poll forever; settle best-effort
                    pass
                page.wait_for_timeout(750)
                page.screenshot(path=out_path, full_page=True)
                metrics = None
                if audited_desktop:
                    try:
                        raw = page.evaluate(_LAYOUT_METRICS_JS)
                        metrics = raw if isinstance(raw, dict) else None
                    except Exception:  # noqa: BLE001 - audit capture is advisory
                        metrics = None
            finally:
                browser.close()
        return out_path, metrics
    except Exception:  # noqa: BLE001 - missing browser binary, nav error, etc.
        return None, None


def screenshot(url: str, out_path: str, *, timeout_ms: int = 8000) -> str | None:
    """Compatible screenshot-only wrapper around :func:`capture_visual_evidence`."""
    shot, _metrics = capture_visual_evidence(url, out_path, timeout_ms=timeout_ms)
    return shot


def _extract_json(text: str) -> str:
    t = (text or "").strip()
    if "```" in t:
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1].removeprefix("json").strip()
    a, b = t.find("{"), t.rfind("}")
    # no '{' -> returns raw text; json.loads then fails and inspect() soft-skips (safe).
    return t[a:b + 1] if a >= 0 and b > a else t


def inspect(image_path: str, goal: str, *, vision_fn: VisionFn | None = None,
            prior: str | None = None) -> VisualVerdict:
    """Ask `vision_fn` whether the screenshot fulfills `goal`. Soft-skips when no
    vision_fn is supplied or the output isn't parseable. Never raises."""
    if vision_fn is None:
        return VisualVerdict(skipped=True, reason="no vision provider wired")
    prompt = (
        "You are reviewing a screenshot of a running web app. "
        f"The user asked for: '{goal}'."
        + (f" Prior attempt note: {prior}." if prior else "")
        + " Does the screenshot fulfill that request AND look visually correct and"
        " polished (layout, contrast, no obvious breakage)? Respond ONLY as JSON: "
        '{"matches": <bool>, "confidence": <0..1>, "issues": ["..."], '
        '"fix_hint": "<one concrete change if it does not match, else empty>"}'
    )
    try:
        raw = vision_fn(image_path, prompt)
        data = json.loads(_extract_json(raw))
        return VisualVerdict(
            matches=bool(data.get("matches", False)),
            confidence=float(data.get("confidence", 0.5)),
            issues=[str(x) for x in (data.get("issues") or [])],
            fix_hint=str(data.get("fix_hint", "")),
        )
    except Exception as exc:  # noqa: BLE001
        return VisualVerdict(skipped=True, reason=f"vision error: {exc}")


def _image_data_url(image_path: str) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _vision_messages(data_url: str, prompt: str) -> list[dict[str, Any]]:
    """OpenAI/OpenRouter-style multimodal message: a text part + an image part."""
    return [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]


def _make_openrouter_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn that judges a screenshot via OpenRouter, or None when no key
    is configured — or when ``free_only`` is set with no ``vision_model``
    configured. This mirrors the adapter's cost guard (``LLMClient._resolve_vision``):
    configuring ``vision_model`` at all IS the explicit spend opt-in for image
    calls; the paid built-in default is never billed silently under the hard
    cost guard. Every caller already treats a None vision_fn as a soft-skip."""
    from skyn3t.adapters.llm import _is_free_model_id, openrouter_key

    key = openrouter_key(settings)
    if not key:
        return None
    model = str(getattr(settings, "vision_model", "") or "").strip()
    free_only = bool(getattr(settings, "free_only", False))
    if not model:
        if free_only:
            # Don't silently bill the paid default (mirror of the adapter's
            # "llm.vision_skipped_free_only"): log the skip loudly so the
            # operator knows to set vision_model as the explicit opt-in.
            log.info(
                "vision.skipped_free_only",
                reason="free_only with no vision_model configured",
                default_model=_DEFAULT_VISION_MODEL,
            )
            return None
        model = _DEFAULT_VISION_MODEL
    paid_under_free_only = free_only and not _is_free_model_id(model)

    def _vision_fn(image_path: str, prompt: str) -> str:
        import httpx

        from skyn3t.adapters.llm import OPENROUTER_URL
        if paid_under_free_only:
            # DELIBERATE exception to free_only (mirror of the adapter's
            # "llm.vision_paid_under_free_only"): an explicitly configured
            # vision_model is the spend opt-in; keep the spend visible per call.
            log.warning("vision.paid_under_free_only", model=model)
        body = {"model": model,
                "messages": _vision_messages(_image_data_url(image_path), prompt),
                "max_tokens": 700}
        headers = {"Authorization": f"Bearer {key}",
                   "HTTP-Referer": "https://github.com/skyn3t", "X-Title": "SkyN3t"}
        with httpx.Client(timeout=60) as client:
            resp = client.post(OPENROUTER_URL, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]

    return _vision_fn


def _make_cli_vision_fn(settings: Any, provider: str) -> VisionFn | None:
    """Build a vision_fn that judges a screenshot via a vision-capable CLI on PATH
    (claude/kimi), or None when the explicitly selected CLI is unavailable. The CLI
    reads the image FILE (not base64 over HTTP), so it judges the screenshot natively
    without an OpenRouter request."""
    from skyn3t.adapters.llm import _no_mcp_args  # local import avoids any cycle

    provider = str(provider or "").strip().lower()
    if provider not in {"claude", "kimi"} or not shutil.which(provider):
        return None

    def _cli_vision_fn(image_path: str, prompt: str) -> str:
        full = (f"View the image file at {image_path}. {prompt} "
                "Respond with ONLY the JSON object, no prose.")
        # cwd = the image's OWN directory. A `--setting-sources project` CLI
        # session scopes its file-read permission to its cwd ("the project");
        # screenshots live in a system temp dir, nowhere near wherever this
        # Python process happens to be running from. Without this, the CLI
        # refuses the read ("outside the project directory") and silently
        # degrades to an unparseable refusal string instead of a verdict.
        cwd = os.path.dirname(image_path) or None
        try:
            # Resolve at the exec boundary: a bare "claude" is unrunnable on
            # Windows (npm ships claude.cmd/.ps1 and CreateProcess only appends
            # ".exe"), and the WinError 2 is swallowed by the except below —
            # so the vision judge returned "" and the visual-proof gate
            # silently soft-skipped instead of ever running. Resolved here
            # rather than in the factory so a CLI installed later is still
            # found, and so the shutil.which() availability guard above keeps
            # its meaning.
            executable = resolve_executable(provider)
            out = subprocess.run([executable, "-p", full, *_no_mcp_args(settings, provider)],
                                 capture_output=True, text=True, timeout=120,
                                 cwd=cwd, env=filter_env())
            return out.stdout or ""
        except Exception:  # noqa: BLE001 - CLI missing/slow -> empty -> soft-skip
            return ""

    return _cli_vision_fn


def _explicit_vision_backend(settings: Any) -> str:
    """Return the only backend allowed to judge screenshots for this run.

    Liveness, self-heal, game checks, and click grounding all call this module.
    They must respect the same provider RESOLUTION as code generation: an
    explicit ``openrouter``/``claude_cli``/``kimi_cli`` selection is honored
    directly, and ``auto`` mirrors ``LLMClient._resolve_backend``'s auto arm —
    the first available CLI in ``auto_cli_priority`` judges screenshots iff it
    has an image adapter (claude/kimi). When auto resolves to a CLI without
    one (codex/copilot), vision soft-skips rather than skipping past to a
    provider codegen did not select. OpenRouter under ``auto`` requires the
    same explicit ``auto_allow_openrouter`` opt-in as codegen: a dormant key
    alone is configuration, not consent to spend.
    """
    backend = str(getattr(settings, "llm_backend", "auto") or "auto").strip().lower()
    if backend in {"openrouter", "claude_cli", "kimi_cli"}:
        return backend
    if backend != "auto":
        return ""
    # Local import (same no-cycle pattern as `_no_mcp_args` above). The
    # availability probe goes through LLMClient's cached classmethod — never
    # raw shutil.which — so the resolution here can't disagree with the
    # dispatch layer's, and the suite's CLI fence keeps governing it.
    from skyn3t.adapters.llm import LLMClient, _auto_priority_order, openrouter_key

    for provider in _auto_priority_order(settings):
        if LLMClient._cli_available(provider):
            return f"{provider}_cli" if provider in {"claude", "kimi"} else ""
    if bool(getattr(settings, "auto_allow_openrouter", False)) and openrouter_key(settings):
        return "openrouter"
    return ""


def make_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn that judges a screenshot (true/false + free-text issues), or
    None when the resolved backend has no supported image adapter (the loop then soft-
    skips). ``auto`` follows codegen's resolution (a winning claude/kimi CLI judges;
    codex/copilot soft-skip); OpenRouter needs an explicit selection or the
    ``auto_allow_openrouter`` opt-in — never just a dormant credential."""
    backend = _explicit_vision_backend(settings)
    if backend == "openrouter":
        return _make_openrouter_vision_fn(settings)
    if backend:
        return _make_cli_vision_fn(settings, backend.removesuffix("_cli"))
    return None


def make_click_vision_fn(settings: Any) -> VisionFn | None:
    """Build a vision_fn for PIXEL-GROUNDING tasks (locating a click target on a
    screenshot). It follows the same routing policy as ``make_vision_fn``: a selected
    (or auto-resolved) Claude/Kimi CLI can inspect files, a selected OpenRouter
    backend can use its vision model, and ``auto`` activates OpenRouter only under
    the explicit ``auto_allow_openrouter`` opt-in — never from a dormant key."""
    backend = _explicit_vision_backend(settings)
    if backend == "openrouter":
        return _make_openrouter_vision_fn(settings)
    if backend:
        return _make_cli_vision_fn(settings, backend.removesuffix("_cli"))
    return None


def _dom_start_click(page: Any) -> bool:
    """Click a visible DOM element (button/link/any element) whose text suggests it
    begins or continues play (Start/Play/Begin/Continue/Run/Go/Enter) — BEFORE falling
    back to vision-grounded canvas clicking. Cheap, deterministic, zero LLM cost, and
    catches what vision-on-the-canvas fundamentally can't: many codegen UIs render the
    start/level-select screen as REAL HTML (a `<button>` or `<div>` overlay on top of
    the canvas, not Phaser-drawn) — invisible to a canvas-only screenshot. Mirrors the
    DOM-first / vision-fallback pattern used by browser-use, Stagehand, and similar
    browser-agent frameworks: try the cheap, reliable signal (the accessibility tree /
    DOM text) before paying for a vision call, which is reserved for genuinely opaque
    canvas content. Returns True iff a click was attempted (not whether it changed
    anything — the caller verifies that separately, the same way Anthropic's own
    computer-use loop verifies an action instead of trusting it blindly). Never
    raises."""
    for locator_fn in (
        lambda: page.get_by_role("button", name=_START_TEXT_PATTERN),
        lambda: page.get_by_role("link", name=_START_TEXT_PATTERN),
        lambda: page.get_by_text(_START_TEXT_PATTERN),
    ):
        try:
            candidates = locator_fn()
            count = candidates.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(min(count, 10)):
            try:
                el = candidates.nth(i)
                if el.is_visible():
                    el.click(timeout=2000)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _fit_viewport_to_canvas(
    page: Any, *, min_w: int = 800, min_h: int = 600,
    margin_w: int = 40, margin_h: int = 220,
) -> None:
    """Grow the page viewport to comfortably fit the game's canvas BEFORE any click or
    screenshot. A fixed-resolution canvas (e.g. 1280x720) routinely exceeds a driver's
    default 800x600 viewport; the scaffold's CSS commonly centers it in an
    ``overflow:hidden`` container with NO scrollbar, so "scroll into view" has nothing
    to scroll — the clipped portion (often the whole left/top of the canvas, at a
    NEGATIVE bounding-box x/y) is genuinely unreachable by click, not just off-screen.
    Reads the canvas's actual RENDERED size via ``getBoundingClientRect()`` (correct
    even for a CSS-scaled canvas) and never shrinks below ``min_w``/``min_h``. Never
    raises; on any failure (no canvas yet, no ``set_viewport_size`` support) the
    viewport is simply left as-is."""
    try:
        rect = page.locator("canvas").first.evaluate(
            "el => { const r = el.getBoundingClientRect(); "
            "return {w: r.width, h: r.height}; }"
        )
        w = int(rect.get("w") or 0)
        h = int(rect.get("h") or 0)
        if w > 0 and h > 0:
            page.set_viewport_size({
                "width": max(min_w, w + margin_w),
                "height": max(min_h, h + margin_h),
            })
    except Exception:  # noqa: BLE001 - a checker must never break a build
        pass


def _vision_locate_start_click(
    vision_fn: VisionFn | None, image_bytes: bytes | None, canvas_w: int, canvas_h: int,
) -> tuple[bool, tuple[float, float] | None]:
    """Ask a vision model whether a game canvas already shows active gameplay, or — for
    a menu/level-select-driven genre (tower defense: pick a level card, then press a
    "Start Wave" button) a blind click(400,300)+Space+Enter heuristic never reaches —
    where to click next. Returns ``(in_play, click)`` where ``click`` is an (x, y) pair
    in the SAME pixel space as the supplied screenshot (the canvas itself, so the caller
    only has to add the canvas's page offset). Never raises: any missing input or
    unparseable/exploding response is treated as "no instruction" (``(False, None)``),
    same soft-skip posture as every other vision call in this module. Shared by
    ``qa_playtest`` and ``game_visual_check`` — both serve a game mid-play and both hit
    the same "blind start heuristic can't reach a menu-driven game" gap."""
    if vision_fn is None or not image_bytes:
        return False, None
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="skyn3t-qa-shot-", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
        prompt = (
            "This is a screenshot of a video game's canvas "
            f"({canvas_w}x{canvas_h} px). The goal is to reach a state where the play "
            "field shows REAL GAME ENTITIES (enemies, characters, obstacles, "
            "projectiles, or pickups) actually present on it. This is NOT satisfied by "
            "a title/menu/level-select screen, and NOT satisfied by a loaded game "
            "scene that only shows a HUD/shop/path/empty field with no entities yet — "
            "waiting on the player to press a \"start wave\"/\"play\"/\"begin\" "
            "control still counts as NOT YET. If the canvas ALREADY shows entities "
            'like that, respond ONLY as JSON: {"in_play": true}. Otherwise identify '
            "the SINGLE most likely click target to progress toward that state (a "
            "level/start card, a \"Play\"/\"Start\" button, a \"Start Wave\" button, "
            "etc.) and respond ONLY as JSON: "
            '{"in_play": false, "x": <int>, "y": <int>} where x and y are PIXEL '
            f"coordinates measured from the TOP-LEFT corner of this image (0, 0), "
            f"with x ranging from 0 to {canvas_w} and y ranging from 0 to {canvas_h} — "
            "not a normalized/percentage scale."
        )
        raw = vision_fn(path, prompt)
        data = json.loads(_extract_json(raw))
        if bool(data.get("in_play")):
            return True, None
        x, y = data.get("x"), data.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
                and not isinstance(x, bool) and not isinstance(y, bool):
            fx, fy = float(x), float(y)
            # Sanity-clamp: a vision model occasionally answers outside the image it
            # was shown (hallucination, or a provider normalizing to a 0-1000/0-1
            # scale instead of raw pixels despite the prompt). An out-of-bounds click
            # target is meaningless — treat it the same as "no instruction" rather
            # than sending a click to a point that was never on screen.
            if 0 <= fx <= canvas_w and 0 <= fy <= canvas_h:
                return False, (fx, fy)
        return False, None
    except Exception:  # noqa: BLE001 - a checker must never break a build
        return False, None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _vision_locate_with_retry(
    vision_fn: VisionFn | None,
    shot_fn: Callable[[], tuple[bytes | None, dict[str, Any] | None]],
    *, attempts: int = 2, wait_fn: Callable[[], None] | None = None,
) -> tuple[bool, tuple[float, float] | None, dict[str, Any] | None]:
    """Retry ``_vision_locate_start_click`` up to ``attempts`` times within ONE
    round before giving up — a single transient parse failure (a CLI backend
    wrapping JSON in prose despite instructions, a frame caught mid-transition)
    shouldn't burn a whole round of the caller's outer retry budget. ``shot_fn()``
    is called FRESH each attempt (the canvas may have settled further by the
    retry) and must return ``(screenshot_bytes, bounding_box_dict)`` — a
    ``(None, None)`` result (can't even screenshot) stops immediately, no point
    retrying. Returns ``(in_play, click, box)`` from the first attempt that
    resolves to an actual instruction (``in_play=True`` or ``click is not
    None``), or the LAST attempt's ``(False, None, box)`` after exhausting
    attempts. Never raises (mirrors ``_vision_locate_start_click``'s soft-skip
    posture)."""
    result: tuple[bool, tuple[float, float] | None] = (False, None)
    box: dict[str, Any] | None = None
    for i in range(max(1, attempts)):
        shot, box = shot_fn()
        if not shot or not box:
            return False, None, box
        result = _vision_locate_start_click(
            vision_fn, shot, int(box["width"]), int(box["height"]))
        if result[0] or result[1] is not None:
            return result[0], result[1], box
        if i < attempts - 1 and wait_fn is not None:
            wait_fn()
    return result[0], result[1], box


class VisualChecker:
    """Screenshot a URL + judge it against a goal. Soft-skips on any missing dep."""

    def __init__(self, event_bus: Any | None = None) -> None:
        self.event_bus = event_bus

    async def _emit(self, payload: dict[str, Any], cid: str | None) -> None:
        if self.event_bus is None:
            return
        try:
            await self.event_bus.emit(EventType.VISUAL_CHECK, "improve", payload, correlation_id=cid)
        except Exception:  # noqa: BLE001 - events never break the loop
            pass

    async def check(
        self,
        url: str,
        goal: str,
        *,
        vision_fn: VisionFn | None = None,
        correlation_id: str | None = None,
        layout_profile: object | None = None,
    ) -> VisualVerdict:
        profile = None
        if layout_profile is not None:
            payload = (
                layout_profile.to_dict()
                if isinstance(layout_profile, LayoutProfile)
                else layout_profile
            )
            profile = profile_from_payload(payload)
        metrics: dict[str, object] | None = None
        if not playwright_available():
            verdict = VisualVerdict(skipped=True, reason="playwright not installed")
        else:
            try:
                fd, path = tempfile.mkstemp(prefix="skyn3t-shot-", suffix=".png")
                os.close(fd)
            except Exception as exc:  # noqa: BLE001
                verdict = VisualVerdict(skipped=True, reason=f"temp file error: {exc}")
            else:
                try:
                    # The sync Playwright API refuses to run inside a live asyncio
                    # loop, and check() is always awaited from one — so run the
                    # capture in a worker thread (no running loop there).
                    shot, metrics = await asyncio.to_thread(
                        capture_visual_evidence,
                        url,
                        path,
                        audited_desktop=bool(profile and profile.audit_enabled),
                    )
                    if shot is None:
                        verdict = VisualVerdict(skipped=True, reason="screenshot failed")
                    else:
                        verdict = inspect(shot, goal, vision_fn=vision_fn)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        if profile is not None:
            verdict = _merge_layout_audit(verdict, assess_layout(profile, metrics))
        await self._emit({"url": url, "goal": goal, **verdict.to_dict()}, correlation_id)
        return verdict
