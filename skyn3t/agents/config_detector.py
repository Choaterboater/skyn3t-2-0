"""config_detector — figure out what configuration an app needs.

Two detectors, both returning a :class:`ConfigSpec`:

* :func:`detect_from_code` wraps the existing :class:`EnvScanner` so a delivered
  tree's ``process.env.X`` / ``os.getenv("X")`` references become declared keys
  (client/server split preserved).
* :func:`detect_from_brief` predicts required keys/APIs *upfront* from the brief.
  It prefers an injected ``llm_fn`` (so it can reason about novel services) and
  falls back to a deterministic keyword heuristic when none is wired — so the
  build never depends on a model being available (design rule #6).

Pure + offline by default. Import has zero side effects.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from skyn3t.agents.env_scanner import EnvScanner
from skyn3t.studio.config_spec import ConfigKey, ConfigSpec, kind_for, scope_for

# A text-in/text-out LLM, built the same way visual_check.make_vision_fn builds a
# vision fn (OpenRouter key -> claude/kimi CLI -> None). None -> keyword fallback.
LLMFn = Callable[[str], str]

# Pure-frontend web stacks have no backend to hold a secret, so an API key they
# need is necessarily client-visible config; everything else defaults to server.
_CLIENT_STACKS = frozenset({
    "react", "react_vite", "vite", "nextjs", "next", "astro", "remix",
    "static", "static_html", "html", "vue", "svelte",
})


def detect_from_code(project_dir: str | Path) -> ConfigSpec:
    """Turn env-var references found in delivered code into a ConfigSpec."""
    res = EnvScanner.scan(project_dir)
    client = set(res.client_vars)
    keys: list[ConfigKey] = []
    for name in res.variables:
        keys.append(ConfigKey(
            name=name,
            kind=kind_for(name),
            scope="client" if name in client else scope_for(name),
            description=f"Detected in source ({', '.join(_files_for(res, name))})".strip(", "),
        ))
    return ConfigSpec(keys=keys)


def _files_for(res: Any, name: str) -> list[str]:
    return [f for f, vs in getattr(res, "by_file", {}).items() if name in vs][:3]


# ---- brief-side detection -------------------------------------------------

# (regex over the lowercased brief) -> a config key factory. Order matters only
# for readability; all matches contribute. Named services come first so e.g.
# "stripe" yields STRIPE_API_KEY rather than the generic API_KEY.
_SERVICE_RULES: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"\bopenai\b|\bgpt-?\d"), "OPENAI_API_KEY", "api_key", "OpenAI API"),
    (re.compile(r"\bstripe\b|\bpayment|\bcheckout\b|\bbilling\b"), "STRIPE_API_KEY", "api_key", "Stripe"),
    (re.compile(r"\bsendgrid\b|\bsmtp\b|\bemail\b|\bmailgun\b"), "EMAIL_API_KEY", "api_key", "Email/SMTP"),
    (re.compile(r"\btwilio\b|\bsms\b"), "TWILIO_API_KEY", "api_key", "Twilio"),
    (re.compile(r"\bmaps?\b|\bgeocod|\bgoogle maps\b"), "MAPS_API_KEY", "api_key", "Maps"),
    (re.compile(r"\bweather\b"), "WEATHER_API_KEY", "api_key", "Weather API"),
    (re.compile(r"\bgithub\b"), "GITHUB_TOKEN", "api_key", "GitHub API"),
    (re.compile(r"\bwebhook"), "WEBHOOK_URL", "url", "Webhook endpoint"),
    (re.compile(r"\b(database|postgres|mysql|mongo|sqlite|db url)\b"), "DATABASE_URL", "url", "Database"),
    (re.compile(r"\b(auth|oauth|login|sign ?in|jwt)\b"), "AUTH_SECRET", "secret", "Auth/session secret"),
)
# Generic "the app calls an API / needs an API key" with no named service.
_GENERIC_API = re.compile(r"\bapi key\b|\bapi token\b|\bthird[- ]party api\b|\bexternal api\b|\bcalls? (an|the|a) .{0,20}api\b")


def _keyword_detect(brief: str, stack: str) -> ConfigSpec:
    low = (brief or "").lower()
    client_stack = (stack or "").strip().lower() in _CLIENT_STACKS
    keys: dict[str, ConfigKey] = {}
    apis: list[str] = []

    def add(name: str, kind: str, label: str) -> None:
        # Secrets/db/auth stay server-side even on a frontend stack; user-facing
        # third-party API keys are client config when there's no backend.
        if kind in ("secret",) or name in ("DATABASE_URL", "AUTH_SECRET"):
            scope = "server"
        else:
            scope = "client" if client_stack else "server"
        name = f"VITE_{name}" if (scope == "client" and not any(
            name.startswith(p) for p in ("VITE_", "NEXT_PUBLIC_", "REACT_APP_", "PUBLIC_"))) else name
        keys.setdefault(name, ConfigKey(name=name, kind=kind, scope=scope, description=label))
        if label not in apis and kind in ("api_key", "url"):
            apis.append(label)

    for pat, name, kind, label in _SERVICE_RULES:
        if pat.search(low):
            add(name, kind, label)
    if not keys and _GENERIC_API.search(low):
        add("API_KEY", "api_key", "External API")
    return ConfigSpec(keys=list(keys.values()), apis=apis)


_PROMPT = (
    "You are analyzing an app brief to determine the runtime configuration the app "
    "will need (API keys, endpoints, secrets, feature toggles). Brief:\n"
    "'''{brief}'''\n"
    "Stack: {stack}.\n"
    "Respond ONLY as JSON: {{\"keys\": [{{\"name\": \"UPPER_SNAKE\", \"kind\": "
    "\"api_key|url|secret|toggle|value\", \"scope\": \"client|server\", "
    "\"description\": \"...\", \"required\": true}}], \"apis\": [\"service name\"]}}. "
    "Use client scope only for values safe to expose in a browser bundle (prefix "
    "those names with VITE_). If the app needs no external configuration, return "
    "empty lists."
)


def _parse_llm(raw: str) -> ConfigSpec | None:
    text = (raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1].removeprefix("json").strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return ConfigSpec.from_dict(json.loads(text[a:b + 1]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def detect_from_brief(brief: str, stack: str = "", *, llm_fn: LLMFn | None = None) -> ConfigSpec:
    """Predict required config from the brief. LLM when wired, else keyword heuristic.

    Never raises: a model error or unparseable output falls back to the keyword
    detector, so detection degrades but never breaks the build."""
    if llm_fn is not None:
        try:
            spec = _parse_llm(llm_fn(_PROMPT.format(brief=brief or "", stack=stack or "")))
        except Exception:  # noqa: BLE001 - any model/transport failure -> fallback
            spec = None
        if spec is not None and not spec.is_empty():
            return spec
    return _keyword_detect(brief, stack)


def detect(brief: str, project_dir: str | Path, stack: str = "", *,
           llm_fn: LLMFn | None = None) -> ConfigSpec:
    """Convenience: merge brief-side prediction with code-side detection."""
    return detect_from_brief(brief, stack, llm_fn=llm_fn).merge(detect_from_code(project_dir))
