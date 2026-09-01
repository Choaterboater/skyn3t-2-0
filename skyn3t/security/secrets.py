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
import threading
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

# Fixed, non-secret sentinel VALUE the mock-LLM proof seam
# (:mod:`skyn3t.studio.mock_llm` via ``proof_run``) injects as the dummy
# OPENAI/ANTHROPIC/OPENROUTER API key so a generated LLM app's OWN tests can
# construct their client against the local mock. ``filter_env`` lets any var
# carrying exactly this value cross into the sandbox (see below): high-precision,
# because a real credential never has this literal value, so nothing leaks.
MOCK_PROOF_VALUE = "mock-proof-key"


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
            # Deploy tokens (mirrors Settings.deploy_tokens). Omitting them
            # meant redact()/scrub_text could not see them, and golden_bench
            # documented several leaking VERBATIM into artifacts/run.json.
            "fly_api_token", "vercel_token", "cloudflare_api_token",
            "netlify_auth_token", "railway_token", "render_api_key",
        ):
            val = getattr(self.settings, attr, "") or ""
            if val:
                self._store[attr.upper()] = val
        # Messaging-channel credentials are env-only (integrations.channels
        # reads bare and SKYN3T_-prefixed names, never Settings), so seed both
        # spellings — none of these token formats match _TOKEN_PATTERNS, so
        # value-based seeding is the only way redact() can scrub them.
        for name in (
            "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "DISCORD_WEBHOOK_URL",
            "SLACK_BOT_TOKEN", "SLACK_TOKEN", "GITHUB_WEBHOOK_SECRET",
        ):
            for env_name in (name, f"SKYN3T_{name}"):
                val = (os.environ.get(env_name) or "").strip()
                if val:
                    self._store[env_name] = val

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
        # The mock-LLM proof seam's dummy key — a fixed sentinel, never a real
        # secret — is allowed through so the generated app's tests can build a
        # client against the local mock (see MOCK_PROOF_VALUE).
        if value == MOCK_PROOF_VALUE:
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
# The same shape, grouped so scrub_text can redact ONLY the userinfo while the
# scheme/host stay readable in logs.
_URL_CRED_SUB = re.compile(r"(://)[^/\s:@]+:[^/\s@]+(@)")


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
    # Credentialed DSNs (postgres://user:pass@host/db ...) passed through
    # verbatim: filter_env used _URL_CRED to DETECT them but nothing redacted
    # them from free text. Keep scheme and host; drop the userinfo.
    out = _URL_CRED_SUB.sub(rf"\1{REDACTED}\2", out)
    return out


# ── output-side masking (ported from OpenHands SecretRegistry) ──────────────
# filter_env() scrubs the environment on the way IN to sandboxed code. This is
# the complement on the way OUT: any registered secret VALUE that turns up in
# text crossing back over the trust boundary (gate verdicts, captured build/
# test output, agent tool observations, CLI prose tails) is replaced before the
# text is recorded or returned. Value-based, so it works no matter which
# channel carried the credential into the text.

# Placeholder that replaces a registered secret value in outgoing text.
MASKED = "<secret-hidden>"

# Never mask values shorter than this: short strings false-positive on
# ordinary words, paths and numbers (the false-positive guard).
_MIN_MASK_VALUE_LEN = 12

# Boolean/number/word config values that must never be treated as credentials
# even when an env var with a secret-sounding NAME carries them
# (SKYN3T_LLM_BACKEND=stub, feature flags, ...).
_BENIGN_MASK_WORDS = frozenset({
    "true", "false", "yes", "no", "on", "off", "none", "null", "stub",
    "test", "debug", "production", "development", "release", "lab",
})

# Env-var name families whose values register for output masking: every
# SKYN3T_* var plus the bare provider spellings (OPENROUTER_API_KEY,
# ANTHROPIC_API_KEY, OPENAI_API_KEY, KIMI_API_KEY, REPLICATE_API_TOKEN, ...).
_MASK_NAME_FAMILY_RE = re.compile(
    r"SKYN3T_|OPENROUTER|ANTHROPIC|OPENAI|KIMI|REPLICATE", re.I
)

_mask_pattern: re.Pattern[str] | None = None
_mask_loaded = False
_mask_lock = threading.Lock()


def _maskable_value(value: str) -> bool:
    """False-positive guard: only values that could actually be credentials."""
    v = (value or "").strip()
    if len(v) < _MIN_MASK_VALUE_LEN:
        return False
    # The mock-LLM proof sentinel is a fixed NON-secret value (see
    # MOCK_PROOF_VALUE); masking it would corrupt generated-app test output.
    if v == MOCK_PROOF_VALUE:
        return False
    if v.lower() in _BENIGN_MASK_WORDS:
        return False
    try:
        float(v)
    except ValueError:
        return True
    return False  # a pure number is configuration, not a credential


def _registered_secret_values() -> set[str]:
    """All currently registered secret values worth masking (discovery reuses
    the module's existing store + name-marker logic)."""
    values: set[str] = set()
    try:
        # Settings-seeded vault: the known API keys, deploy tokens and the
        # messaging-channel credentials SecretsStore.__post_init__ collects.
        values.update(SecretsStore().values())
    except Exception:  # noqa: BLE001 - settings may be unbootable; env scan remains
        pass
    for name, value in os.environ.items():
        if not _MASK_NAME_FAMILY_RE.search(name):
            continue
        if not _looks_secret(name):
            continue
        values.add(value)
    return {v.strip() for v in values if _maskable_value(v)}


def _compiled_mask_pattern() -> re.Pattern[str] | None:
    """The cached alternation over registered values (longest first so
    overlapping prefixes can't leak a suffix). Compiled once at first use;
    ``reset_mask_cache`` re-discovers after a Settings/env change."""
    global _mask_pattern, _mask_loaded
    with _mask_lock:
        if not _mask_loaded:
            candidates = sorted(_registered_secret_values(), key=len, reverse=True)
            _mask_pattern = (
                re.compile("|".join(re.escape(v) for v in candidates))
                if candidates
                else None
            )
            _mask_loaded = True
        return _mask_pattern


def reset_mask_cache() -> None:
    """Drop the compiled masking pattern so the next :func:`mask_secrets` call
    re-discovers registered values (call after a Settings/env change)."""
    global _mask_pattern, _mask_loaded
    with _mask_lock:
        _mask_pattern = None
        _mask_loaded = False


def mask_secrets(text: str) -> str:
    """Replace every REGISTERED secret value in ``text`` with ``MASKED``.

    Output-side complement of :func:`filter_env`: no matter how a host
    credential reached the text (an echoed env var, a verbose toolchain, an
    untrusted subprocess parroting its environment), it does not cross back
    out unmasked. Never raises; never masks empty/short/benign values; with
    nothing registered it is a pure passthrough.
    """
    if not text or len(text) < _MIN_MASK_VALUE_LEN:
        return text
    try:
        pattern = _compiled_mask_pattern()
        if pattern is None:
            return text
        return pattern.sub(MASKED, text)
    except Exception:  # noqa: BLE001 - masking must never break a build
        return text
