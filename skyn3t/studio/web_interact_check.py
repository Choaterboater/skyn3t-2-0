"""Web interaction check — the "renders but isn't wired" catch.

Every other web gate probes ROUTES (liveness GETs each path), judges ONE
screenshot (visual self-heal / responsive proof), or statically lints markup
(web_polish). None of them CLICKS the delivered app the way a user does, so a
Potemkin interface — a nav link that goes nowhere, a main form whose submit is
wired to nothing — survives every gate while the app "renders fine".

This check closes that gap the way Replit Agent 3's self-testing does,
localized to SkyN3t's advisory posture:

  1. SERVE the delivered app in the isolated preview (the same
     ``PreviewSupervisor`` boot path liveness/qa_playtest use — NOT a second
     server implementation).
  2. HARVEST the app's interaction surface statically: links, forms and
     buttons from the built HTML (stdlib parser, zero deps), API/state
     endpoints via liveness' route enumerator.
  3. Ask ONE LLM call for a SHORT declarative JSON action plan driving ONE
     real user flow end to end through a closed action set.
  4. RUN the validated plan with sync Playwright in a worker thread (the sync API
     raises inside a live event loop — the qa_playtest/liveness solution).

DUAL-SURFACE assertions are the point: the script must assert BOTH the UI
surface (a success text/element becomes visible) AND the backend surface where
one exists (an API/state endpoint reflects the created/updated record, probed
through the in-scope ``fetch`` helper). Static-only apps degrade to UI+URL
assertions.

ADVISORY-FIRST and NEVER-RAISES, mirroring ``qa_playtest``:

  * a REAL broken interaction (failed selector, assertion mismatch, uncaught
    console error mid-flow) records ``ok=False`` with evidence — it NEVER
    flips the build verdict (the runner only records it under
    ``manifest.extra["web_interact"]``);
  * everything that prevents an honest run SOFT-SKIPS instead of failing:
    phaser (qa_playtest's turf) / non-web stacks, no Playwright, no non-stub
    LLM backend ($0: the skip is decided BEFORE anything is served), an
    unservable preview, an invalid generated plan (a harness fault,
    not the app's), or a plan that records no steps.

Import has zero side effects; nothing is served and no LLM is called until
``check_web_interact`` runs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from skyn3t.core.stacks import GAME_STACKS, WEB_STACKS
from skyn3t.studio.liveness import _SKIP_PARTS, enumerate_routes
from skyn3t.studio.qa_playtest import _dedup_cap
from skyn3t.studio.visual_check import playwright_available

# Every HTTP-served stack except the game stack (which qa_playtest drives).
_INTERACT_STACKS = WEB_STACKS - GAME_STACKS

_MAX_SCRIPT_CHARS = 6000
_MAX_STEPS = 40


def _skip(reason: str, *, checked: list[str] | None = None) -> dict[str, Any]:
    """A could-not-run result: never a failure, always with the reason."""
    return {
        "ok": True,
        "skipped": True,
        "reason": reason,
        "issues": [],
        "warnings": [],
        "interactions": [],
        "checked": list(checked or []),
    }


# ── static interaction-surface harvest (no browser, no LLM) ─────────────────

class _SurfaceParser(HTMLParser):
    """Collect the interactive surface of ONE built HTML page: links with
    their visible text, forms (action/method/fields/submit label), and
    standalone buttons. Best-effort stdlib parsing — malformed markup just
    yields less surface, never an exception."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.forms: list[dict[str, Any]] = []
        self.buttons: list[dict[str, str]] = []
        self._open: list[dict[str, Any]] = []  # open a/button collecting text
        self._form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._form = {
                "action": a.get("action", "")[:160],
                "method": (a.get("method") or "GET").upper(),
                "fields": [],
                "submit": "",
            }
        elif tag in ("input", "select", "textarea"):
            if self._form is None:
                return
            if a.get("type", "").lower() == "submit":
                self._form["submit"] = (a.get("value") or "")[:80]
                return
            field = {
                key: a[key][:80]
                for key in ("name", "type", "placeholder", "id")
                if a.get(key)
            }
            if tag != "input":
                field.setdefault("type", tag)
            if field.get("name") or field.get("id"):
                self._form["fields"].append(field)
        elif tag in ("a", "button"):
            self._open.append({"tag": tag, "attrs": a, "text": []})

    def handle_data(self, data: str) -> None:
        if self._open:
            self._open[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        if self._open and self._open[-1]["tag"] == tag:
            rec = self._open.pop()
            text = " ".join("".join(rec["text"]).split())[:80]
            a = rec["attrs"]
            if tag == "a":
                href = (a.get("href") or "").strip()
                if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    self.links.append({"href": href[:160], "text": text})
            elif self._form is not None and not self._form["submit"]:
                # A <button> inside the open form is its submit control.
                self._form["submit"] = (text or a.get("id", ""))[:80]
            else:
                btn: dict[str, str] = {"text": text}
                if a.get("id"):
                    btn["id"] = a["id"][:80]
                self.buttons.append(btn)


def _dedup_dicts(items: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        marker = tuple(str(item.get(k, "")) for k in keys)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def harvest_action_surface(project_dir: str | Path, stack: str = "") -> dict[str, Any]:
    """Statically harvest what a user can CLICK in the delivered app: links,
    forms and buttons from every built ``*.html`` page, plus the API/page
    route surface from liveness' enumerator (the BACKEND surface the flow
    should verify). Never raises; caps every list for a compact prompt."""
    root = Path(project_dir)
    checked: list[str] = []
    links: list[dict[str, str]] = []
    forms: list[dict[str, Any]] = []
    buttons: list[dict[str, str]] = []
    try:
        html_files = sorted(root.rglob("*.html"))
    except Exception:  # noqa: BLE001
        html_files = []
    for path in html_files:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if _SKIP_PARTS.intersection(rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            continue
        checked.append(rel.as_posix())
        parser = _SurfaceParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:  # noqa: BLE001 - malformed markup yields partial surface
            pass
        links.extend(parser.links)
        forms.extend(parser.forms)
        buttons.extend(parser.buttons)
    apis: list[str] = []
    pages: list[str] = []
    try:
        for route in enumerate_routes(root, stack):
            entry = f"{route.method} {route.path}"
            if route.kind == "api":
                apis.append(entry)
            else:
                pages.append(entry)
    except Exception:  # noqa: BLE001 - route enumeration is best-effort
        pass
    return {
        "checked": checked[:50],
        "links": _dedup_dicts(links, ("href", "text"))[:12],
        "forms": forms[:6],
        "buttons": _dedup_dicts(buttons, ("text", "id"))[:12],
        "apis": list(dict.fromkeys(apis))[:12],
        "pages": list(dict.fromkeys(pages))[:12],
    }


# ── the ONE LLM call: compact tool spec -> bounded JSON action plan ──────────

def _build_script_prompt(base_url: str, surface: dict[str, Any]) -> str:
    links = "; ".join(
        f"'{link['text'] or link['href']}' -> {link['href']}" for link in surface["links"]
    ) or "(none)"
    form_descs = []
    for form in surface["forms"]:
        fields = ", ".join(
            fld.get("name") or fld.get("id") or fld.get("type", "?")
            for fld in form["fields"]
        ) or "no named fields"
        form_descs.append(
            f"{form['method']} {form['action'] or '(current page)'} "
            f"fields=[{fields}] submit='{form['submit']}'"
        )
    forms = "; ".join(form_descs) or "(none)"
    buttons = "; ".join(
        btn.get("text") or btn.get("id") or "?" for btn in surface["buttons"]
    ) or "(none)"
    apis = "; ".join(surface["apis"]) or "(none — static-only app)"
    pages = "; ".join(surface["pages"]) or "(unknown)"
    return (
        "You are QA-ing a generated web app served at "
        f"{base_url}. Describe ONE real user flow as a bounded JSON action plan.\n\n"
        "Harvested interaction surface (ground EVERY locator in this — never "
        "invent ids, names, or texts):\n"
        f"Pages: {pages}\n"
        f"Nav links: {links}\n"
        f"Forms: {forms}\n"
        f"Buttons: {buttons}\n"
        f"API endpoints (backend surface): {apis}\n\n"
        "Return exactly one JSON object with an actions array. Every action needs "
        "a short label. Supported actions:\n"
        '- click: {"op":"click","label":"...","by":"role",'
        '"role":"link","name":"Guestbook"}\n'
        '- fill: {"op":"fill","label":"...","by":"selector",'
        '"selector":"#name","value":"Ada"}\n'
        '- expect_visible: same locator fields as click\n'
        '- expect_text: same locator fields plus "contains":"expected text"\n'
        '- fetch_expect: {"op":"fetch_expect","label":"...",'
        '"path":"/api/items","status":200,"contains":"Ada"}\n'
        '- expect_url_contains: {"op":"expect_url_contains","label":"...",'
        '"contains":"/done"}\n'
        "Locator modes are role (role + name), selector (selector), label "
        "(name), placeholder (name), and text (name).\n\n"
        "Rules:\n"
        "1. Drive one end-to-end flow: navigate, act, verify.\n"
        "2. Include a UI assertion after the main action.\n"
        "3. If API endpoints exist, include fetch_expect for the resulting backend "
        "state. Otherwise assert both UI state and URL.\n"
        "4. Use exact harvested locators and realistic values.\n"
        "5. At most 40 actions. JSON only — no markdown or code."
    )


def _extract_script(text: str) -> dict[str, Any] | None:
    """Parse an LLM reply as a declarative action plan; never execute code."""
    value = (text or "").strip()
    if "```" in value:
        parts = value.split("```")
        if len(parts) > 1:
            value = parts[1].strip()
            if value.lower().startswith("json"):
                value = value[4:].lstrip()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None

def _make_interact_llm(settings: Any) -> Callable[[str], Any] | None:
    """Build the ONE-shot action-plan author, or None when the resolved backend is
    the offline stub — mirroring ``visual_check.make_vision_fn``'s posture: a
    missing/unconfigured LLM is a soft-skip, never a failure, and deciding it
    HERE (before anything is served) keeps the skip deterministic $0."""
    try:
        from skyn3t.adapters.llm import LLMClient, Tier

        client = LLMClient(settings)
        if client.backend == "stub":
            return None

        async def _llm(prompt: str) -> str:
            result = await client.complete(
                prompt, tier=Tier.CHEAP, max_tokens=1500, task_type="web_interact"
            )
            return str(getattr(result, "text", "") or "")

        return _llm
    except Exception:  # noqa: BLE001 - unbuildable client == no backend
        return None


# ── the sync-Playwright driver (runs in a worker thread) ────────────────────

_LOCATOR_KEYS = {
    "role": {"by", "role", "name", "exact"},
    "selector": {"by", "selector"},
    "label": {"by", "name", "exact"},
    "placeholder": {"by", "name", "exact"},
    "text": {"by", "name", "exact"},
}
_ACTION_KEYS = {
    "click": {"op", "label", "timeout_ms"},
    "fill": {"op", "label", "timeout_ms", "value"},
    "expect_visible": {"op", "label", "timeout_ms"},
    "expect_text": {"op", "label", "timeout_ms", "contains"},
    "fetch_expect": {"op", "label", "path", "status", "contains"},
    "expect_url_contains": {"op", "label", "contains"},
}
_LOCATOR_ACTIONS = {"click", "fill", "expect_visible", "expect_text"}


def _validated_actions(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Validate the closed action schema before launching a browser."""
    if set(plan) != {"actions"}:
        return [], "plan must contain only an actions array"
    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        return [], "plan actions must be a non-empty array"
    if len(actions) > _MAX_STEPS:
        return [], f"plan has too many actions ({len(actions)} > {_MAX_STEPS})"
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            return [], f"action {index} must be an object"
        op = action.get("op")
        if op not in _ACTION_KEYS:
            return [], f"action {index} has unsupported op {op!r}"
        label = action.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 160:
            return [], f"action {index} needs a label of 1-160 characters"
        allowed = set(_ACTION_KEYS[op])
        if op in _LOCATOR_ACTIONS:
            by = action.get("by")
            if by not in _LOCATOR_KEYS:
                return [], f"action {index} has unsupported locator mode {by!r}"
            allowed.update(_LOCATOR_KEYS[by])
            needed = "selector" if by == "selector" else "name"
            value = action.get(needed)
            if not isinstance(value, str) or not value or len(value) > 300:
                return [], f"action {index} needs a valid {needed} locator"
            if by == "role":
                role = action.get("role")
                if not isinstance(role, str) or not role or len(role) > 60:
                    return [], f"action {index} needs a valid role"
        unknown = set(action) - allowed
        if unknown:
            return [], f"action {index} has unsupported fields: {sorted(unknown)}"
        for key in ("value", "contains", "path"):
            if key in action and (
                not isinstance(action[key], str) or len(action[key]) > 2000
            ):
                return [], f"action {index} has an invalid {key} value"
        if op == "fill" and "value" not in action:
            return [], f"action {index} needs a fill value"
        if op in {"expect_text", "expect_url_contains"} and not action.get("contains"):
            return [], f"action {index} needs non-empty expected text"
        if op == "fetch_expect":
            path = action.get("path")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                return [], f"action {index} needs a same-origin absolute path"
            status = action.get("status", 200)
            if not isinstance(status, int) or not 100 <= status <= 599:
                return [], f"action {index} has an invalid HTTP status"
        if "timeout_ms" in action:
            timeout = action["timeout_ms"]
            if not isinstance(timeout, int) or not 100 <= timeout <= 30000:
                return [], f"action {index} has an invalid timeout_ms"
    return actions, ""


def _drive_interaction(
    url: str,
    script: dict[str, Any],
    *,
    nav_timeout_ms: int = 15000,
    action_timeout_ms: int = 8000,
) -> dict[str, Any]:
    """Execute a validated declarative flow against the served app.

    The LLM can select only the closed action set above; no model-authored code
    is compiled or executed. Schema failures are harness faults and soft-skip.
    Assertion, selector, navigation, and browser errors remain real evidence
    against the app. Sync Playwright must run via ``asyncio.to_thread``.
    Never raises.
    """
    steps: list[str] = []
    raw_errors: list[str] = []
    probes = {"count": 0}
    out: dict[str, Any] = {
        "passed": False,
        "error": "",
        "script_error": False,
        "steps": steps,
        "console_errors": [],
        "backend_probes": 0,
    }
    try:
        serialized = json.dumps(script)
    except (TypeError, ValueError) as exc:
        out["script_error"] = True
        out["error"] = f"generated plan is not JSON-serializable: {exc}"
        return out
    if len(serialized) > _MAX_SCRIPT_CHARS:
        out["script_error"] = True
        out["error"] = f"generated plan too large ({len(serialized)} chars)"
        return out
    actions, validation_error = _validated_actions(script)
    if validation_error:
        out["script_error"] = True
        out["error"] = validation_error
        return out
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        out["script_error"] = True
        out["error"] = f"playwright unavailable: {exc}"
        return out
    base_url = str(url).rstrip("/")

    def _fetch(path: str, timeout: float = 8.0) -> tuple[int, str]:
        probes["count"] += 1
        target = base_url + "/" + path.lstrip("/")
        req = urllib.request.Request(target, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost preview only
                return int(resp.status), resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "ignore")

    def _locator(page: Any, action: dict[str, Any]) -> Any:
        by = action["by"]
        exact = bool(action.get("exact", True))
        if by == "selector":
            return page.locator(action["selector"])
        if by == "role":
            return page.get_by_role(action["role"], name=action["name"], exact=exact)
        if by == "label":
            return page.get_by_label(action["name"], exact=exact)
        if by == "placeholder":
            return page.get_by_placeholder(action["name"], exact=exact)
        return page.get_by_text(action["name"], exact=exact)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.set_default_timeout(action_timeout_ms)

                def _on_console(msg: Any) -> None:
                    try:
                        if msg.type == "error":
                            raw_errors.append(str(msg.text))
                    except Exception:  # noqa: BLE001
                        pass

                page.on("pageerror", lambda exc: raw_errors.append(str(exc)))
                page.on("console", _on_console)
                page.goto(base_url + "/", timeout=nav_timeout_ms, wait_until="load")
                page.wait_for_timeout(500)
                for action in actions:
                    steps.append(action["label"].strip())
                    op = action["op"]
                    timeout = int(action.get("timeout_ms", action_timeout_ms))
                    if op == "click":
                        _locator(page, action).click(timeout=timeout)
                    elif op == "fill":
                        _locator(page, action).fill(action["value"], timeout=timeout)
                    elif op == "expect_visible":
                        _locator(page, action).wait_for(state="visible", timeout=timeout)
                    elif op == "expect_text":
                        locator = _locator(page, action)
                        locator.wait_for(state="visible", timeout=timeout)
                        actual = locator.inner_text(timeout=timeout)
                        assert action["contains"] in actual, (
                            f"expected {action['contains']!r} in visible text {actual[:300]!r}"
                        )
                    elif op == "fetch_expect":
                        status, body = _fetch(action["path"])
                        expected = int(action.get("status", 200))
                        assert status == expected, f"api status {status}, expected {expected}"
                        if "contains" in action:
                            assert action["contains"] in body, (
                                f"expected {action['contains']!r} in API response"
                            )
                    elif op == "expect_url_contains":
                        assert action["contains"] in page.url, (
                            f"expected {action['contains']!r} in URL {page.url!r}"
                        )
                page.wait_for_timeout(300)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - assertion/timeout/nav = evidence
        out["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    out["console_errors"] = _dedup_cap(raw_errors)
    out["backend_probes"] = probes["count"]
    if not out["error"]:
        out["passed"] = True
    return out

def _score(
    surface: dict[str, Any],
    result: dict[str, Any],
    checked: list[str],
) -> dict[str, Any]:
    """Score a drive result: interaction passed / failed-with-evidence, with
    harness faults degraded to soft-skips. Never raises."""
    steps = [str(s) for s in (result.get("steps") or [])][:_MAX_STEPS]
    console = [str(e) for e in (result.get("console_errors") or [])]
    warnings: list[str] = []
    if not surface["apis"]:
        warnings.append(
            "no backend API surface detected — verified UI/URL assertions only"
        )
    elif not int(result.get("backend_probes") or 0):
        warnings.append(
            "the generated flow never probed the backend surface — UI-only verification"
        )
    if result.get("script_error"):
        # Our harness failed the app, not the other way round — degrade open.
        return _skip(
            f"generated action plan unusable: {str(result.get('error', ''))[:200]}",
            checked=checked,
        )
    if not result.get("passed"):
        issues = [
            f"interaction flow failed after {len(steps)} recorded step(s): "
            f"{str(result.get('error', ''))[:300]}",
            *(f"uncaught console error during interaction: {e}" for e in console),
        ]
        return {
            "ok": False,
            "skipped": False,
            "reason": "",
            "issues": issues,
            "warnings": warnings,
            "interactions": steps,
            "checked": checked,
        }
    if not steps:
        # A trivially-passing plan that did nothing proves nothing.
        return _skip("generated script recorded no interaction steps", checked=checked)
    if console:
        return {
            "ok": False,
            "skipped": False,
            "reason": "",
            "issues": [
                f"uncaught console error during interaction: {e}" for e in console
            ],
            "warnings": warnings,
            "interactions": steps,
            "checked": checked,
        }
    return {
        "ok": True,
        "skipped": False,
        "reason": "",
        "issues": [],
        "warnings": warnings,
        "interactions": steps,
        "checked": checked,
    }


async def check_web_interact(
    project_dir: str | Path,
    stack: str = "",
    *,
    settings: Any,
    llm: Callable[[str], Any] | None = None,
    app_runner: Any | None = None,
    drive_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Serve the delivered web app, drive ONE LLM-authored declarative Playwright flow
    through it, and assert BOTH surfaces (UI + backend). ADVISORY and
    NEVER-RAISES: ``ok`` is False only for a REAL broken interaction; every
    environmental/harness gap soft-skips (``skipped=True`` with a reason).
    ``llm`` (prompt -> JSON plan text, sync or async), ``app_runner`` and
    ``drive_fn`` are injectable so the logic is testable without a browser,
    a Docker daemon, or a paid model."""
    try:
        low = (stack or "").strip().lower()
        if low in GAME_STACKS:
            return _skip("game stack — covered by the qa_playtest driver")
        if low and low not in _INTERACT_STACKS:
            return _skip(f"stack {low!r} is not an HTTP-served web app")
        if not playwright_available():
            return _skip("playwright not installed")
        surface = harvest_action_surface(project_dir, low)
        checked = list(surface["checked"])
        if not (surface["links"] or surface["forms"] or surface["buttons"]):
            return _skip(
                "no interactive surface harvested from the built HTML",
                checked=checked,
            )
        llm_fn = llm if llm is not None else _make_interact_llm(settings)
        if llm_fn is None:
            # Deterministic $0: decided BEFORE the preview is served.
            return _skip(
                "no LLM backend configured (offline stub) — script authoring skipped",
                checked=checked,
            )

        from skyn3t.studio.app_runner import cleanup_serve
        from skyn3t.studio.preview_supervisor import PreviewSupervisor

        runner = app_runner or PreviewSupervisor()
        app = await runner.start(project_dir, low)
        try:
            url = getattr(app, "url", "") or ""
            if getattr(app, "status", "running") != "running" or not url:
                return _skip("app did not serve a preview", checked=checked)
            prompt = _build_script_prompt(url, surface)
            try:
                raw = llm_fn(prompt)
                if inspect.isawaitable(raw):
                    raw = await raw
            except Exception as exc:  # noqa: BLE001 - an LLM failure soft-skips
                return _skip(f"action-plan generation failed: {exc}"[:300], checked=checked)
            script = _extract_script(str(raw or ""))
            if not script:
                return _skip("LLM returned no valid action plan", checked=checked)
            drive = drive_fn or _drive_interaction
            try:
                result = await asyncio.to_thread(drive, url, script)
            except Exception as exc:  # noqa: BLE001 - a driver failure soft-skips
                return _skip(f"drive error: {exc}"[:300], checked=checked)
        finally:
            try:
                stopped = runner.stop(app)
                if inspect.isawaitable(stopped):
                    await stopped
            except Exception:  # noqa: BLE001
                pass
            try:
                cleanup_serve(app)
            except Exception:  # noqa: BLE001
                pass
        return _score(surface, result, checked)
    except Exception as exc:  # noqa: BLE001 - a checker must never break a build
        return _skip(f"web interact error: {exc}"[:300])
