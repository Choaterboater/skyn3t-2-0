"""Secrets store + environment scrubbing.

Design rules: safe by default (#4) and degrade-don't-crash (#6). The store is
an in-memory vault seeded from process env + settings; nothing is written to
disk at import time. ``filter_env`` strips anything that looks like a secret
from the environment handed to sandboxed agent subprocesses so credentials
never leak into untrusted build code.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from skyn3t.config.settings import Settings, get_settings

# Substrings (case-insensitive) that mark an env var as secret-bearing.
_SECRET_MARKERS: tuple[str, ...] = (
    "key", "token", "secret", "password", "passwd", "pwd", "credential",
    "auth", "api_key", "apikey", "access", "private", "session", "cookie",
    "bearer", "signature", "salt", "dsn", "conn", "webhook",
)

# Env vars that are safe even though they match a marker above.
_ALLOWLIST: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "PWD", "SHELL", "USER",
    "TMPDIR", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
})

# Redaction placeholder used everywhere a secret would otherwise appear.
REDACTED = "***REDACTED***"


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    if upper in _ALLOWLIST:
        return False
    low = name.lower()
    return any(m in low for m in _SECRET_MARKERS)


def is_secret_name(name: str) -> bool:
    """Public: True when an env-var NAME looks secret-bearing (see ``filter_env``).

    Callers that decide whether a name needs resolving/redacting (e.g. serve-time
    passthrough) use this rather than reaching for the private ``_looks_secret``.
    """
    return _looks_secret(name)


@dataclass
class SecretsStore:
    """In-memory secret vault. No disk persistence (rule #4: safe by default).

    Seeded from :class:`Settings` so the known API keys are tracked and can be
    redacted, but callers may also ``put`` arbitrary secrets at runtime.
    """

    settings: Settings = field(default_factory=get_settings)
    _store: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for attr in (
            "openrouter_api_key", "anthropic_api_key", "openai_api_key",
            "kimi_api_key", "auth_token", "replicate_api_token", "github_token",
        ):
            val = getattr(self.settings, attr, "") or ""
            if val:
                self._store[attr.upper()] = val

    # ---- vault ops -------------------------------------------------------
    def put(self, name: str, value: str) -> None:
        self._store[name.upper()] = value

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._store.get(name.upper(), default)

    def has(self, name: str) -> bool:
        return name.upper() in self._store

    def names(self) -> list[str]:
        return sorted(self._store)

    def values(self) -> set[str]:
        """All secret *values* currently tracked (for redaction)."""
        return {v for v in self._store.values() if v}

    # ---- redaction -------------------------------------------------------
    def redact(self, text: str) -> str:
        """Replace any tracked secret value in ``text`` with a placeholder."""
        if not text:
            return text
        out = text
        # Replace longest values first so overlapping prefixes don't leak.
        for val in sorted(self.values(), key=len, reverse=True):
            if val and val in out:
                out = out.replace(val, REDACTED)
        return out


def filter_env(
    env: Mapping[str, str] | None = None,
    *,
    extra_block: Iterable[str] = (),
    keep: Iterable[str] = (),
) -> dict[str, str]:
    """Return a copy of ``env`` with secret-looking variables removed.

    Used to build the environment passed to sandboxed agent subprocesses so no
    host credentials cross the trust boundary. ``keep`` overrides detection for
    a specific allowlist; ``extra_block`` force-removes named vars.
    """
    source = dict(env if env is not None else os.environ)
    keep_upper = {k.upper() for k in keep}
    block_upper = {k.upper() for k in extra_block}
    clean: dict[str, str] = {}
    for name, value in source.items():
        up = name.upper()
        if up in block_upper:
            continue
        if up in keep_upper:
            clean[name] = value
            continue
        if _looks_secret(name) or _value_has_credential(value):
            continue
        clean[name] = value
    return clean


_TOKEN_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-or-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
)

# scheme://user:pass@host — a credential embedded in a URL value (DATABASE_URL,
# GIT_REMOTE, REDIS_URL, npm registry _authToken urls, ...).
_URL_CRED = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


def _value_has_credential(value: str) -> bool:
    """True when a value embeds a credential even though its NAME looks innocuous.

    filter_env() only inspected variable NAMES, so GIT_REMOTE / DATABASE_URL with
    an embedded token or user:pass@ URL crossed into the sandbox unredacted. These
    patterns are high-precision token formats + URL userinfo, so a plain PATH/HOME
    won't false-positive; the ``keep`` allowlist overrides if a value is needed.
    """
    if not value:
        return False
    if _URL_CRED.search(value):
        return True
    return any(p.search(value) for p in _TOKEN_PATTERNS)


def scrub_text(text: str, store: SecretsStore | None = None) -> str:
    """Best-effort redaction of secrets in free text (known values + patterns).

    Useful for log lines and event payloads that may have captured a token.
    """
    if not text:
        return text
    out = store.redact(text) if store is not None else text
    for pat in _TOKEN_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out
