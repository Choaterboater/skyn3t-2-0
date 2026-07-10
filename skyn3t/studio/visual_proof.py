"""Deterministic responsive browser proof for delivered web applications.

The vision judge in :mod:`skyn3t.studio.visual_check` answers a subjective
question: does the rendered page match the brief?  This module supplies the
objective half of that gate.  It captures pinned desktop and mobile evidence
and checks browser/runtime failures, overflow, empty content, broken images,
and only high-confidence element overlaps.

Playwright and its Chromium binary are optional.  Their absence is serialized
as ``skipped`` evidence; it is never represented as a passing proof.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from skyn3t.atomic_io import atomic_write_text
from skyn3t.core.stacks import GAME_STACKS
from skyn3t.studio.visual_check import playwright_available

VISUAL_PROOF_SCHEMA_VERSION = 1
_PIXEL_TOLERANCE = 2
_MAX_RECORDED_OVERLAPS = 8


@dataclass(frozen=True, slots=True)
class ViewportSpec:
    name: str
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_VIEWPORTS: tuple[ViewportSpec, ...] = (
    ViewportSpec("desktop", 1440, 900),
    ViewportSpec("mobile", 390, 844),
)


@dataclass(slots=True)
class VisualProofIssue:
    code: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ViewportProof:
    name: str
    width: int
    height: int
    passed: bool = False
    skipped: bool = False
    reason: str = ""
    screenshot: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    issues: list[VisualProofIssue] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "passed": self.passed,
            "skipped": self.skipped,
            "reason": self.reason,
            "screenshot": self.screenshot,
            "metrics": self.metrics,
            "issues": [issue.to_dict() for issue in self.issues],
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
        }


@dataclass(slots=True)
class ResponsiveVisualProof:
    url: str
    route: str
    stack: str
    passed: bool = False
    skipped: bool = False
    reason: str = ""
    report_path: str | None = None
    viewports: list[ViewportProof] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VISUAL_PROOF_SCHEMA_VERSION,
            "url": self.url,
            "route": self.route,
            "stack": self.stack,
            "passed": self.passed,
            "skipped": self.skipped,
            "reason": self.reason,
            "report_path": self.report_path,
            "viewports": [viewport.to_dict() for viewport in self.viewports],
        }


_DOM_SNAPSHOT_SCRIPT = r"""() => {
  const pxTolerance = 2;
  const viewportWidth = document.documentElement.clientWidth || window.innerWidth || 0;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
  const viewportArea = Math.max(1, viewportWidth * viewportHeight);
  const isVisible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        Number(style.opacity || 1) < 0.02) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  const cleanText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const ownText = (el) => cleanText(Array.from(el.childNodes)
    .filter((node) => node.nodeType === Node.TEXT_NODE)
    .map((node) => node.textContent || '').join(' '));
  const targetCandidates = Array.from(document.querySelectorAll(
    'main, [role="main"], #root, #app, #__next'));
  const target = targetCandidates.find(isVisible) || document.body;
  const targetKind = target === document.body ? 'body' :
    (target.matches('main, [role="main"]') ? 'main' : 'app-root');
  const inPrimaryContent = (el) => !el.closest(
    'header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]');
  const primaryText = (root) => {
    if (!root) return '';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const chunks = [];
    while (walker.nextNode()) {
      const parent = walker.currentNode.parentElement;
      if (parent && inPrimaryContent(parent) && isVisible(parent)) {
        chunks.push(walker.currentNode.textContent || '');
      }
    }
    return cleanText(chunks.join(' '));
  };
  const targetText = primaryText(target);
  const bodyText = cleanText(document.body ? document.body.innerText : '');
  const visibleControls = Array.from((target || document).querySelectorAll(
    'button, a[href], input, select, textarea, [role="button"], [role="link"]'
  )).filter((el) => isVisible(el) && inPrimaryContent(el)).length;
  const visibleMediaElements = Array.from((target || document).querySelectorAll(
    'img, video, svg, picture'
  )).filter((el) => {
    if (!isVisible(el) || !inPrimaryContent(el)) return false;
    const rect = el.getBoundingClientRect();
    if (el.tagName === 'IMG') {
      return Boolean(el.complete && el.naturalWidth > 1 && el.naturalHeight > 1 &&
        rect.width * rect.height >= 8000 && rect.width >= 40 && rect.height >= 40);
    }
    return rect.width * rect.height >= 8000 && rect.width >= 40 && rect.height >= 40;
  });
  const backgroundCandidates = target
    ? [target, ...Array.from(target.querySelectorAll('*'))] : [];
  const backgroundMedia = backgroundCandidates
    .filter((el) => {
      if (!isVisible(el) || !inPrimaryContent(el)) return false;
      const rect = el.getBoundingClientRect();
      return rect.width * rect.height >= 8000 &&
        getComputedStyle(el).backgroundImage.includes('url(');
    });
  const visibleMedia = visibleMediaElements.length + backgroundMedia.length;
  const visibleElements = Array.from((target || document).querySelectorAll('*'))
    .filter((el) => isVisible(el) && inPrimaryContent(el)).length;

  const images = Array.from(document.images || []);
  const deferredLazyImages = images.filter((img) => {
    if (!isVisible(img) || img.complete || img.loading !== 'lazy') return false;
    const rect = img.getBoundingClientRect();
    return rect.top > viewportHeight + 200 || rect.bottom < -200;
  });
  const brokenImages = images.filter((img) => {
    if (!isVisible(img)) return false;
    if (img.complete) return img.naturalWidth <= 0 || img.naturalHeight <= 0;
    const rect = img.getBoundingClientRect();
    const deferred = img.loading === 'lazy' &&
      (rect.top > viewportHeight + 200 || rect.bottom < -200);
    return !deferred;
  })
    .slice(0, 20).map((img) => ({
      src: String(img.currentSrc || img.src || '').slice(0, 500),
      alt: cleanText(img.alt).slice(0, 120),
      complete: Boolean(img.complete),
      natural_width: Number(img.naturalWidth || 0),
      natural_height: Number(img.naturalHeight || 0),
    }));

  const canvases = Array.from(document.querySelectorAll('canvas')).filter(isVisible);
  const canvasDetails = canvases.slice(0, 8).map((canvas) => {
    const rect = canvas.getBoundingClientRect();
    let readable = false;
    let nonblank = null;
    try {
      if (canvas.width > 0 && canvas.height > 0) {
        const probe = document.createElement('canvas');
        probe.width = 16; probe.height = 16;
        const ctx = probe.getContext('2d', {willReadFrequently: true});
        if (!ctx) throw new Error('2d probe context unavailable');
        ctx.drawImage(canvas, 0, 0, probe.width, probe.height);
        readable = true;
        const colors = new Set();
        let painted = 0;
        for (let gy = 1; gy <= 5; gy += 1) {
          for (let gx = 1; gx <= 5; gx += 1) {
            const x = Math.min(probe.width - 1, Math.floor(probe.width * gx / 6));
            const y = Math.min(probe.height - 1, Math.floor(probe.height * gy / 6));
            const pixel = ctx.getImageData(x, y, 1, 1).data;
            if (pixel[3] > 2) painted += 1;
            colors.add(`${pixel[0]},${pixel[1]},${pixel[2]},${pixel[3]}`);
          }
        }
        nonblank = painted > 0 && colors.size > 1;
      }
    } catch (_) {
      readable = false;
      nonblank = null;
    }
    return {
      width: Math.round(rect.width), height: Math.round(rect.height),
      left: Math.round(rect.left), top: Math.round(rect.top),
      right: Math.round(rect.right), bottom: Math.round(rect.bottom),
      area_ratio: Number(((rect.width * rect.height) / viewportArea).toFixed(4)),
      readable, nonblank,
    };
  });
  const canvasAreaRatio = canvasDetails.reduce(
    (best, item) => Math.max(best, Number(item.area_ratio || 0)), 0);

  const scrollWidth = Math.max(
    document.documentElement.scrollWidth || 0,
    document.body ? document.body.scrollWidth || 0 : 0
  );
  const overflowElements = Array.from(document.querySelectorAll('body *'))
    .filter(isVisible).map((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.left >= -pxTolerance && rect.right <= viewportWidth + pxTolerance) return null;
      const style = getComputedStyle(el);
      return {
        tag: el.tagName.toLowerCase(), id: String(el.id || '').slice(0, 80),
        classes: String(el.className || '').slice(0, 160),
        left: Math.round(rect.left), right: Math.round(rect.right),
        width: Math.round(rect.width), position: style.position,
        contains_canvas: Boolean(el.querySelector('canvas')),
      };
    }).filter(Boolean).slice(0, 16);

  const selector = [
    'button', 'a[href]', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'label'
  ].join(',');
  const nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible)
    .map((el) => {
      const interactive = el.matches(
        'button, a[href], input, select, textarea, [role="button"], [role="link"], ' +
        '[role="tab"], [role="menuitem"]');
      const text = interactive ? cleanText(el.innerText || el.value || el.getAttribute('aria-label')) :
        ownText(el);
      if (!interactive && text.length < 2) return null;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const overlay = el.closest(
        '[role="dialog"], [aria-modal="true"], [popover], [data-visual-overlap-ok]');
      return {el, interactive, text: text.slice(0, 100), rect, position: style.position,
        transformed: style.transform && style.transform !== 'none',
        overlay_scope: Boolean(overlay)};
    }).filter(Boolean).slice(0, 180);
  const overlaps = [];
  for (let i = 0; i < nodes.length && overlaps.length < 16; i += 1) {
    for (let j = i + 1; j < nodes.length && overlaps.length < 16; j += 1) {
      const a = nodes[i]; const b = nodes[j];
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      const left = Math.max(a.rect.left, b.rect.left);
      const top = Math.max(a.rect.top, b.rect.top);
      const right = Math.min(a.rect.right, b.rect.right);
      const bottom = Math.min(a.rect.bottom, b.rect.bottom);
      const iw = right - left; const ih = bottom - top;
      if (iw <= 0 || ih <= 0) continue;
      const intersection = iw * ih;
      const aArea = Math.max(1, a.rect.width * a.rect.height);
      const bArea = Math.max(1, b.rect.width * b.rect.height);
      const ratio = intersection / Math.min(aArea, bArea);
      if (ratio < 0.35) continue;
      const touchesCanvas = canvasDetails.some((canvas) =>
        right > canvas.left && left < canvas.right && bottom > canvas.top && top < canvas.bottom);
      overlaps.push({
        a: {tag: a.el.tagName.toLowerCase(), text: a.text,
          interactive: a.interactive, position: a.position},
        b: {tag: b.el.tagName.toLowerCase(), text: b.text,
          interactive: b.interactive, position: b.position},
        intersection_ratio: Number(ratio.toFixed(4)),
        intersection_width: Math.round(iw), intersection_height: Math.round(ih),
        smaller_area: Math.round(Math.min(aArea, bArea)),
        same_parent: a.el.parentElement === b.el.parentElement,
        positioned: !['static', 'relative'].includes(a.position) ||
          !['static', 'relative'].includes(b.position),
        transformed: Boolean(a.transformed || b.transformed),
        overlay_scope: Boolean(a.overlay_scope || b.overlay_scope),
        canvas_intersection: touchesCanvas,
      });
    }
  }
  return {
    ready_state: document.readyState,
    title: cleanText(document.title).slice(0, 200),
    target_kind: targetKind,
    body_text_chars: bodyText.length,
    main_text_chars: targetText.length,
    visible_controls: visibleControls,
    visible_media: visibleMedia,
    background_media_count: backgroundMedia.length,
    visible_elements: visibleElements,
    image_count: images.length,
    deferred_lazy_image_count: deferredLazyImages.length,
    broken_images: brokenImages,
    canvas_count: canvases.length,
    canvas_area_ratio: Number(canvasAreaRatio.toFixed(4)),
    canvases: canvasDetails,
    client_width: viewportWidth,
    client_height: viewportHeight,
    scroll_width: scrollWidth,
    overflow_elements: overflowElements,
    html_overflow_x: getComputedStyle(document.documentElement).overflowX,
    body_overflow_x: document.body ? getComputedStyle(document.body).overflowX : '',
    overlaps,
  };
}"""

_PRIME_LAZY_IMAGES_SCRIPT = r"""async () => {
  const images = Array.from(document.querySelectorAll('img[loading="lazy"]'))
    .filter((img) => !img.complete).slice(0, 24);
  const startX = window.scrollX; const startY = window.scrollY;
  for (const image of images) {
    image.scrollIntoView({block: 'center', inline: 'nearest'});
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  window.scrollTo(startX, startY);
  return images.length;
}"""

_SETTLE_MOTION_SCRIPT = r"""() => {
  for (const animation of document.getAnimations()) {
    try {
      const timing = animation.effect && animation.effect.getComputedTiming
        ? animation.effect.getComputedTiming() : null;
      if (timing && timing.iterations === Infinity) {
        animation.currentTime = 0;
        animation.pause();
      } else {
        animation.finish();
      }
    } catch (_) {
      try { animation.pause(); } catch (_) {}
    }
  }
  return document.getAnimations().length;
}"""


def _route_slug(route: str) -> str:
    normalized = str(route or "/")
    label = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalized.strip("/"))[:80] or "root"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{label}-{digest}"


def _short_error(exc: BaseException | str) -> str:
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text[:400] or type(exc).__name__


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _clear_previous_route_artifacts(root: Path) -> None:
    """Remove only route directories named by our previous batch report."""
    report_path = root / "visual-proof.json"
    try:
        previous = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    root_resolved = root.resolve()
    for route in previous.get("routes", []) if isinstance(previous, dict) else []:
        if not isinstance(route, dict) or not route.get("report_path"):
            continue
        relative = Path(str(route["report_path"]))
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.name != "report.json"
        ):
            continue
        candidate = root / relative.parts[0]
        # Reports are one level below the evidence root. Never trust persisted
        # JSON enough to recursively remove anything outside that exact shape.
        if (
            candidate.is_symlink()
            or candidate.resolve().parent != root_resolved
            or not re.fullmatch(r"[A-Za-z0-9._-]+-[0-9a-f]{8}", candidate.name)
            or not candidate.is_dir()
        ):
            continue
        try:
            shutil.rmtree(candidate)
        except OSError:
            pass


def _high_confidence_overlap(pair: dict[str, Any], *, game_canvas: bool) -> bool:
    """Keep only overlaps that are very unlikely to be deliberate composition.

    Dialog/popover layers, transformed animation frames, positioned HUDs, tiny
    intersections, and repeated labels are deliberately excluded.  This leaves
    normal-flow text/control peers substantially occupying the same pixels.
    """
    if float(pair.get("intersection_ratio") or 0.0) < 0.45:
        return False
    if int(pair.get("intersection_width") or 0) < 6:
        return False
    if int(pair.get("intersection_height") or 0) < 6:
        return False
    if int(pair.get("smaller_area") or 0) < 100:
        return False
    if pair.get("overlay_scope") or pair.get("transformed") or pair.get("positioned"):
        return False
    if game_canvas and pair.get("canvas_intersection"):
        return False
    a = cast(dict[str, Any], pair.get("a")) if isinstance(pair.get("a"), dict) else {}
    b = cast(dict[str, Any], pair.get("b")) if isinstance(pair.get("b"), dict) else {}
    a_text = re.sub(r"\s+", " ", str(a.get("text") or "")).strip().lower()
    b_text = re.sub(r"\s+", " ", str(b.get("text") or "")).strip().lower()
    if a_text and a_text == b_text and not (a.get("interactive") and b.get("interactive")):
        return False
    return bool(a.get("interactive") or b.get("interactive") or (a_text and b_text))


def analyze_viewport_snapshot(
    snapshot: dict[str, Any], *, stack: str = "",
) -> tuple[dict[str, Any], list[VisualProofIssue]]:
    """Turn browser measurements into deterministic, conservative findings.

    This pure function is the policy boundary: tests can lock every threshold
    and suppression without running Playwright.
    """
    stack_name = str(stack or "").strip().lower()
    game_canvas = stack_name in GAME_STACKS and int(snapshot.get("canvas_count") or 0) > 0
    canvas_ratio = float(snapshot.get("canvas_area_ratio") or 0.0)
    canvas_dominant = canvas_ratio >= 0.25
    canvas_rows = (
        cast(list[Any], snapshot.get("canvases"))
        if isinstance(snapshot.get("canvases"), list)
        else []
    )
    readable_blank_canvas = bool(canvas_rows) and all(
        bool(row.get("readable")) and row.get("nonblank") is False
        for row in canvas_rows if isinstance(row, dict)
    )
    metrics: dict[str, Any] = {
        "ready_state": str(snapshot.get("ready_state") or ""),
        "target_kind": str(snapshot.get("target_kind") or "body"),
        "body_text_chars": int(snapshot.get("body_text_chars") or 0),
        "main_text_chars": int(snapshot.get("main_text_chars") or 0),
        "visible_controls": int(snapshot.get("visible_controls") or 0),
        "visible_media": int(snapshot.get("visible_media") or 0),
        "background_media_count": int(snapshot.get("background_media_count") or 0),
        "visible_elements": int(snapshot.get("visible_elements") or 0),
        "image_count": int(snapshot.get("image_count") or 0),
        "deferred_lazy_image_count": int(snapshot.get("deferred_lazy_image_count") or 0),
        "broken_image_count": len(snapshot.get("broken_images") or []),
        "canvas_count": int(snapshot.get("canvas_count") or 0),
        "canvas_area_ratio": round(canvas_ratio, 4),
        "client_width": int(snapshot.get("client_width") or 0),
        "scroll_width": int(snapshot.get("scroll_width") or 0),
        "horizontal_overflow_px": max(
            0,
            int(snapshot.get("scroll_width") or 0) - int(snapshot.get("client_width") or 0),
        ),
        "canvas_dominant": canvas_dominant,
        "suppressed_game_canvas_overflow": False,
        "overlap_candidates": len(snapshot.get("overlaps") or []),
    }
    issues: list[VisualProofIssue] = []

    broken = snapshot.get("broken_images") or []
    if broken:
        issues.append(VisualProofIssue(
            code="broken_images",
            message=f"{len(broken)} visible image(s) did not load",
            evidence={"images": broken[:10]},
        ))

    overflow_px = int(metrics["horizontal_overflow_px"])
    if overflow_px > _PIXEL_TOLERANCE:
        culprits = snapshot.get("overflow_elements") or []
        only_canvas = bool(culprits) and all(
            str(item.get("tag") or "").lower() == "canvas" or item.get("contains_canvas")
            for item in culprits if isinstance(item, dict)
        )
        suppress_game_overflow = game_canvas and canvas_dominant and (only_canvas or not culprits)
        if suppress_game_overflow:
            metrics["suppressed_game_canvas_overflow"] = True
        else:
            issues.append(VisualProofIssue(
                code="horizontal_overflow",
                message=f"page is {overflow_px}px wider than the viewport",
                evidence={"overflow_px": overflow_px, "elements": culprits[:8]},
            ))

    text_chars = int(snapshot.get("main_text_chars") or 0)
    controls = int(snapshot.get("visible_controls") or 0)
    media = int(snapshot.get("visible_media") or 0)
    elements = int(snapshot.get("visible_elements") or 0)
    canvas_visible = int(snapshot.get("canvas_count") or 0) > 0 and canvas_ratio > 0.02
    canvas_content = canvas_visible and (game_canvas or not readable_blank_canvas)
    substantive = (
        text_chars >= 20
        or controls >= 2
        or media >= 1
        or canvas_content
    )
    if not substantive:
        target = "main content" if snapshot.get("target_kind") == "main" else "page content"
        issues.append(VisualProofIssue(
            code="blank_or_near_empty",
            message=f"{target} is blank or near-empty after the page settled",
            evidence={
                "text_chars": text_chars,
                "visible_controls": controls,
                "visible_media": media,
                "visible_elements": elements,
                "canvas_visible": canvas_visible,
            },
        ))

    overlaps = snapshot.get("overlaps") or []
    kept = [
        pair for pair in overlaps
        if isinstance(pair, dict) and _high_confidence_overlap(pair, game_canvas=game_canvas)
    ][:_MAX_RECORDED_OVERLAPS]
    metrics["high_confidence_overlaps"] = len(kept)
    if kept:
        issues.append(VisualProofIssue(
            code="incoherent_overlap",
            message=f"{len(kept)} high-confidence element overlap(s) detected",
            evidence={"pairs": kept},
        ))
    return metrics, issues


def _console_message(msg: Any) -> str | None:
    try:
        kind = str(msg.type or "").lower()
        text = str(msg.text or "").strip()
        location = msg.location or {}
    except Exception:  # noqa: BLE001 - browser event handlers must not raise
        return None
    if kind != "error" or not text:
        return None
    low = text.lower()
    if "resizeobserver loop limit exceeded" in low:
        return None
    if "resizeobserver loop completed with undelivered notifications" in low:
        return None
    source = str(location.get("url") or "") if isinstance(location, dict) else ""
    if source.lower().split("?", 1)[0].endswith("/favicon.ico"):
        return None
    return f"{text} ({source})" if source else text


def _finalize_route(proof: ResponsiveVisualProof) -> None:
    any_skipped = any(v.skipped for v in proof.viewports)
    inspected_findings = any(v.issues for v in proof.viewports if not v.skipped)
    proof.skipped = bool(proof.viewports) and any_skipped and not inspected_findings
    proof.passed = bool(proof.viewports) and all(v.passed for v in proof.viewports)
    if proof.skipped:
        reasons = [v.reason for v in proof.viewports if v.reason]
        if all(v.skipped for v in proof.viewports):
            proof.reason = reasons[0] if reasons else "responsive browser proof unavailable"
        else:
            proof.reason = "one or more required viewports could not be inspected"
    elif any_skipped:
        proof.reason = "one or more required viewports could not be inspected"
    elif not proof.passed:
        proof.reason = "deterministic responsive checks failed"
    else:
        proof.reason = ""


def _skipped_proof(
    url: str,
    route: str,
    stack: str,
    viewports: Sequence[ViewportSpec],
    reason: str,
) -> ResponsiveVisualProof:
    proof = ResponsiveVisualProof(url=url, route=route, stack=stack)
    proof.viewports = [
        ViewportProof(
            name=viewport.name,
            width=viewport.width,
            height=viewport.height,
            skipped=True,
            reason=reason,
        )
        for viewport in viewports
    ]
    _finalize_route(proof)
    return proof


def _audit_viewport(
    browser: Any,
    *,
    url: str,
    viewport: ViewportSpec,
    root: Path,
    route_dir: Path,
    stack: str,
    timeout_ms: int,
) -> ViewportProof:
    result = ViewportProof(viewport.name, viewport.width, viewport.height)
    console_errors: list[str] = []
    page_errors: list[str] = []
    try:
        page = browser.new_page(viewport={"width": viewport.width, "height": viewport.height})
    except Exception as exc:  # noqa: BLE001
        result.skipped = True
        result.reason = f"browser page unavailable: {_short_error(exc)}"
        return result
    try:
        page.on("console", lambda msg: console_errors.append(text)
                if (text := _console_message(msg)) else None)
        page.on("pageerror", lambda exc: page_errors.append(_short_error(exc)))
        nav_issue: VisualProofIssue | None = None
        try:
            response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            if response is not None and int(response.status) >= 400:
                nav_issue = VisualProofIssue(
                    "page_http_error",
                    f"browser navigation returned HTTP {int(response.status)}",
                    {"status": int(response.status), "url": str(response.url)},
                )
            try:
                page.wait_for_load_state("networkidle", timeout=min(2500, timeout_ms))
            except Exception:  # noqa: BLE001 - polling apps may never become idle
                pass
            page.wait_for_timeout(600)
        except Exception as exc:  # noqa: BLE001
            nav_issue = VisualProofIssue(
                "page_navigation_error",
                f"browser navigation failed: {_short_error(exc)}",
                {"url": url},
            )

        try:
            if str(stack or "").strip().lower() not in GAME_STACKS:
                try:
                    page.evaluate(_PRIME_LAZY_IMAGES_SCRIPT)
                    page.wait_for_timeout(250)
                except Exception:  # noqa: BLE001 - lazy priming is best-effort
                    pass
                try:
                    page.evaluate(_SETTLE_MOTION_SCRIPT)
                    page.wait_for_timeout(100)
                except Exception:  # noqa: BLE001 - motion settling is best-effort
                    pass
            snapshot = page.evaluate(_DOM_SNAPSHOT_SCRIPT)
            if not isinstance(snapshot, dict):
                raise TypeError("DOM audit returned a non-object result")
            result.metrics, result.issues = analyze_viewport_snapshot(snapshot, stack=stack)
        except Exception as exc:  # noqa: BLE001
            result.skipped = True
            result.reason = f"DOM audit unavailable: {_short_error(exc)}"
            return result

        if nav_issue is not None:
            result.issues.insert(0, nav_issue)
        result.console_errors = list(dict.fromkeys(console_errors))[:20]
        result.page_errors = list(dict.fromkeys(page_errors))[:20]
        if result.console_errors:
            result.issues.append(VisualProofIssue(
                "console_errors",
                f"{len(result.console_errors)} console error(s) occurred",
                {"errors": result.console_errors},
            ))
        if result.page_errors:
            result.issues.append(VisualProofIssue(
                "page_errors",
                f"{len(result.page_errors)} uncaught page error(s) occurred",
                {"errors": result.page_errors},
            ))

        shot_path = route_dir / f"{viewport.name}.png"
        try:
            page.screenshot(path=str(shot_path), full_page=True)
            result.screenshot = shot_path.relative_to(root).as_posix()
        except Exception as exc:  # noqa: BLE001
            had_app_findings = bool(result.issues)
            result.issues.append(VisualProofIssue(
                "screenshot_failed",
                f"required screenshot evidence could not be captured: {_short_error(exc)}",
            ))
            if not had_app_findings:
                result.skipped = True
                result.reason = "required screenshot evidence could not be captured"
        result.passed = not result.issues
        return result
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def _write_route_report(root: Path, proof: ResponsiveVisualProof) -> None:
    route_dir = root / _route_slug(proof.route)
    proof.report_path = (route_dir / "report.json").relative_to(root).as_posix()
    _write_json(route_dir / "report.json", proof.to_dict())


def _write_batch_report(
    root: Path,
    stack: str,
    proofs: Sequence[ResponsiveVisualProof],
    viewports: Sequence[ViewportSpec],
) -> None:
    attempted = [proof for proof in proofs if not proof.skipped]
    payload = {
        "schema_version": VISUAL_PROOF_SCHEMA_VERSION,
        "stack": stack,
        "passed": bool(proofs) and all(proof.passed for proof in proofs),
        "skipped": bool(proofs) and not attempted,
        "viewports": [viewport.to_dict() for viewport in viewports],
        "routes": [proof.to_dict() for proof in proofs],
    }
    _write_json(root / "visual-proof.json", payload)


def _mark_artifact_failure(
    proofs: Sequence[ResponsiveVisualProof], exc: BaseException | str,
) -> None:
    reason = f"visual proof artifact write failed: {_short_error(exc)}"
    for proof in proofs:
        if proof.skipped:
            proof.reason = f"{proof.reason}; {reason}" if proof.reason else reason
            continue
        for viewport in proof.viewports:
            if viewport.skipped:
                continue
            had_app_findings = bool(viewport.issues)
            if not any(issue.code == "artifact_write_failed" for issue in viewport.issues):
                viewport.issues.append(VisualProofIssue("artifact_write_failed", reason))
            if not had_app_findings:
                viewport.skipped = True
                viewport.reason = reason
            viewport.passed = False
        _finalize_route(proof)


def _persist_proofs(
    root: Path,
    stack: str,
    proofs: Sequence[ResponsiveVisualProof],
    viewports: Sequence[ViewportSpec],
) -> None:
    for proof in proofs:
        try:
            _write_route_report(root, proof)
        except Exception as exc:  # noqa: BLE001 - report failure becomes a finding
            _mark_artifact_failure([proof], exc)
    try:
        _write_batch_report(root, stack, proofs, viewports)
    except Exception as exc:  # noqa: BLE001
        _mark_artifact_failure(proofs, exc)
        # Route-level reports may still be writable and must reflect the failure.
        for proof in proofs:
            try:
                _write_route_report(root, proof)
            except Exception:  # noqa: BLE001
                pass


def audit_responsive_pages(
    pages: Sequence[tuple[str, str]],
    artifact_dir: str | Path,
    *,
    stack: str = "",
    timeout_ms: int = 10_000,
    viewports: Sequence[ViewportSpec] = DEFAULT_VIEWPORTS,
) -> list[ResponsiveVisualProof]:
    """Audit ``(route, url)`` pairs in one browser process and persist evidence.

    A route passes only when every requested viewport was actually inspected and
    has no deterministic findings.  Missing Playwright or Chromium returns
    explicit skipped proofs and still writes the JSON report when possible.
    """
    normalized = [(str(route or "/"), str(url)) for route, url in pages]
    if not normalized:
        return []
    root = Path(artifact_dir)
    stack_name = str(stack or "").strip().lower()
    viewport_list = tuple(viewports)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        reason = f"visual proof artifact directory unavailable: {_short_error(exc)}"
        return [
            _skipped_proof(url, route, stack_name, viewport_list, reason)
            for route, url in normalized
        ]
    _clear_previous_route_artifacts(root)
    if not playwright_available():
        proofs = [
            _skipped_proof(url, route, stack_name, viewport_list, "playwright not installed")
            for route, url in normalized
        ]
        _persist_proofs(root, stack_name, proofs, viewport_list)
        return proofs

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                reason = f"playwright chromium unavailable: {_short_error(exc)}"
                proofs = [
                    _skipped_proof(url, route, stack_name, viewport_list, reason)
                    for route, url in normalized
                ]
            else:
                try:
                    proofs = []
                    for route, url in normalized:
                        proof = ResponsiveVisualProof(url=url, route=route, stack=stack_name)
                        route_dir = root / _route_slug(route)
                        route_dir.mkdir(parents=True, exist_ok=True)
                        for viewport in viewport_list:
                            proof.viewports.append(_audit_viewport(
                                browser,
                                url=url,
                                viewport=viewport,
                                root=root,
                                route_dir=route_dir,
                                stack=stack_name,
                                timeout_ms=max(1000, int(timeout_ms)),
                            ))
                        _finalize_route(proof)
                        proofs.append(proof)
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001 - evidence is already captured
                        pass
    except Exception as exc:  # noqa: BLE001 - import/driver startup is an honest skip
        reason = f"playwright unavailable: {_short_error(exc)}"
        proofs = [
            _skipped_proof(url, route, stack_name, viewport_list, reason)
            for route, url in normalized
        ]

    _persist_proofs(root, stack_name, proofs, viewport_list)
    return proofs


def audit_responsive_page(
    url: str,
    artifact_dir: str | Path,
    *,
    route: str = "/",
    stack: str = "",
    timeout_ms: int = 10_000,
    viewports: Sequence[ViewportSpec] = DEFAULT_VIEWPORTS,
) -> ResponsiveVisualProof:
    """Single-page convenience wrapper around :func:`audit_responsive_pages`."""
    return audit_responsive_pages(
        [(route, url)],
        artifact_dir,
        stack=stack,
        timeout_ms=timeout_ms,
        viewports=viewports,
    )[0]
