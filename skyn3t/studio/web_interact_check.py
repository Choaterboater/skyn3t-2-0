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
  3. Ask ONE LLM call for a SHORT Playwright-python script driving ONE real
     user flow end to end (code-as-actions: loops/conditionals come free, no
     tool-call loop needed).
  4. RUN the script with sync Playwright in a worker thread (the sync API
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
    unservable preview, an uncompilable generated script (a harness fault,
    not the app's), or a script that records no steps.

Import has zero side effects; nothing is served and no LLM is called until
``check_web_interact`` runs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
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


# ── the ONE LLM call: compact tool spec -> short Playwright-python script ────

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
        f"{base_url}. Write ONE short Playwright Python (sync API) script that "
        "exercises ONE real user flow end to end — e.g. click the main nav "
        "link, fill and submit the main form, then verify the resulting state.\n"
        "\n"
        "Harvested interaction surface (ground EVERY selector in this — never "
        "invent ids, names, or texts):\n"
        f"Pages: {pages}\n"
        f"Nav links: {links}\n"
        f"Forms: {forms}\n"
        f"Buttons: {buttons}\n"
        f"API endpoints (backend surface): {apis}\n"
        "\n"
        "Already in scope (do NOT import anything):\n"
        "- page: sync Playwright Page, already loaded at the base URL\n"
        "- base_url: str\n"
        "- step(label): record what you are doing; call it before EACH action\n"
        "- expect: playwright.sync_api.expect\n"
        "- fetch(path, method=\"GET\", json_body=None) -> (status:int, body:str): "
        "same-origin HTTP probe for the BACKEND surface\n"
        "- json, re\n"
        "\n"
        "Rules:\n"
        "1. Drive the flow a real user would: navigate, act, verify.\n"
        "2. Assert the UI surface: after the main action a success text/element "
        "is visible (expect(...).to_be_visible() or assert on page.content()).\n"
        "3. Assert the BACKEND surface where API endpoints are listed above: "
        "after the UI action, fetch() the relevant endpoint and assert the "
        "created/updated record appears in its response body. With no API "
        "endpoints (static-only app), assert the UI change AND the page URL "
        "instead.\n"
        "4. Prefer get_by_role/get_by_label/get_by_text with the EXACT texts "
        "and names from the harvested surface; fill fields with realistic "
        "values.\n"
        "5. Under 40 lines. Respond with ONLY the Python code — no markdown "
        "fences, no prose."
    )


def _extract_script(text: str) -> str:
    """Pull the python code out of an LLM reply (fenced or bare)."""
    t = (text or "").strip()
    if "```" in t:
        parts = t.split("```")
        if len(parts) > 1:
            t = parts[1].removeprefix("python").removeprefix("py").strip()
    return t


def _make_interact_llm(settings: Any) -> Callable[[str], Any] | None:
    """Build the ONE-shot script author, or None when the resolved backend is
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

def _drive_interaction(
    url: str,
    script: str,
    *,
    nav_timeout_ms: int = 15000,
    action_timeout_ms: int = 8000,
) -> dict[str, Any]:
    """Execute the LLM-authored flow against the served app and report.

    The script runs with a small fixed namespace: ``page`` (already loaded at
    ``url``), ``base_url``, ``step`` (records human-readable flow steps),
    ``expect`` (playwright's sync assertions), ``fetch`` (same-origin HTTP
    probe for the backend surface), ``json``/``re``. A ``SyntaxError`` or an
    over-long/unimportable harness is ``script_error=True`` — a HARNESS fault
    the caller must soft-skip, never attribute to the app. An assertion
    failure, a playwright timeout (failed selector), or an exception mid-flow
    is REAL evidence against the app. Sync Playwright — call via
    ``asyncio.to_thread``. Never raises."""
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
    if len(script) > _MAX_SCRIPT_CHARS:
        out["script_error"] = True
        out["error"] = f"generated script too large ({len(script)} chars)"
        return out
    try:
        code = compile(script, "<web-interact>", "exec")
    except SyntaxError as exc:
        out["script_error"] = True
        out["error"] = f"generated script does not compile: {exc}"
        return out
    try:
        from playwright.sync_api import expect, sync_playwright
    except Exception as exc:  # noqa: BLE001
        out["script_error"] = True
        out["error"] = f"playwright unavailable: {exc}"
        return out
    base_url = str(url).rstrip("/")

    def _step(label: Any) -> None:
        if len(steps) < _MAX_STEPS:
            steps.append(str(label)[:160])

    def _fetch(
        path: Any,
        method: str = "GET",
        json_body: Any = None,
        timeout: float = 8.0,
    ) -> tuple[int, str]:
        probes["count"] += 1
        # Same-origin by construction: the path is always joined under base_url.
        target = base_url + "/" + str(path).lstrip("/")
        data = None
        headers = {}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            target, data=data, headers=headers, method=str(method).upper()
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost preview only
                return int(resp.status), resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "ignore")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
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
                env: dict[str, Any] = {
                    "page": page,
                    "base_url": base_url,
                    "step": _step,
                    "fetch": _fetch,
                    "expect": expect,
                    "json": json,
                    "re": re,
                }
                # Lab-authored flow against the isolated localhost preview —
                # same trust posture as qa_playtest's browser driver.
                exec(code, env)  # noqa: S102
                page.wait_for_timeout(300)  # let late async errors land
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
            f"generated script unusable: {str(result.get('error', ''))[:200]}",
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
        # A trivially-"passing" script that did nothing proves nothing.
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
    """Serve the delivered web app, drive ONE LLM-authored Playwright flow
    through it, and assert BOTH surfaces (UI + backend). ADVISORY and
    NEVER-RAISES: ``ok`` is False only for a REAL broken interaction; every
    environmental/harness gap soft-skips (``skipped=True`` with a reason).
    ``llm`` (prompt -> script text, sync or async), ``app_runner`` and
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
                return _skip(f"script generation failed: {exc}"[:300], checked=checked)
            script = _extract_script(str(raw or ""))
            if not script:
                return _skip("LLM returned no runnable script", checked=checked)
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
