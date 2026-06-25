"""ConfigSpec — the declared configuration an app needs to run.

A first-class, serializable contract that flows through the build/improve
pipeline: it records the API keys, endpoints, secrets, toggles and plain values a
delivered app requires, plus the external APIs it talks to. It is detected two
ways (from the brief, and from generated code via :class:`EnvScanner`), merged,
recorded in ``manifest.extra["config_spec"]``, and used to generate a settings UI
and verify wiring.

Pure data + helpers. Import has zero side effects.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# A config key's kind drives how the settings UI renders it and how the
# wiring-verifier reasons about it.
KINDS = ("api_key", "url", "secret", "toggle", "value")
SCOPES = ("client", "server")

# Vite/Next/CRA only expose vars with these prefixes to the browser; a name with
# one is client-visible config, anything else is a server-side secret/value.
CLIENT_PREFIXES = ("VITE_", "NEXT_PUBLIC_", "REACT_APP_", "PUBLIC_")


@dataclass(slots=True)
class ConfigKey:
    """A single piece of configuration an app needs."""

    name: str
    kind: str = "value"      # api_key | url | secret | toggle | value
    description: str = ""
    scope: str = "server"    # client | server
    required: bool = True
    default: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            self.kind = "value"
        if self.scope not in SCOPES:
            self.scope = "server"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConfigKey:
        # Forward-compatible: ignore unknown keys rather than raising.
        allowed = {"name", "kind", "description", "scope", "required", "default"}
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass(slots=True)
class ConfigSpec:
    """The full set of configuration a build/improve determined the app needs."""

    keys: list[ConfigKey] = field(default_factory=list)
    apis: list[str] = field(default_factory=list)

    # ---- queries ---------------------------------------------------------
    def is_empty(self) -> bool:
        return not self.keys and not self.apis

    def client_keys(self) -> list[ConfigKey]:
        return [k for k in self.keys if k.scope == "client"]

    def server_keys(self) -> list[ConfigKey]:
        return [k for k in self.keys if k.scope == "server"]

    def key_names(self) -> list[str]:
        return [k.name for k in self.keys]

    # ---- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"keys": [k.to_dict() for k in self.keys], "apis": list(self.apis)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConfigSpec:
        if not isinstance(d, dict):
            return cls()
        keys = [ConfigKey.from_dict(k) for k in (d.get("keys") or []) if isinstance(k, dict)]
        apis = [str(a) for a in (d.get("apis") or [])]
        return cls(keys=keys, apis=apis)

    # ---- composition -----------------------------------------------------
    def merge(self, other: ConfigSpec) -> ConfigSpec:
        """Union with ``other`` by key name (self wins on conflict) and by API.

        Returns a NEW spec; neither input is mutated. A later code-scan that finds
        a key the brief already declared keeps the brief's richer metadata
        (description/kind), so detection order doesn't lose information.
        """
        by_name: dict[str, ConfigKey] = {}
        for k in [*self.keys, *other.keys]:
            by_name.setdefault(k.name, k)
        apis: list[str] = []
        for a in [*self.apis, *other.apis]:
            if a and a not in apis:
                apis.append(a)
        return ConfigSpec(keys=list(by_name.values()), apis=apis)


def scope_for(name: str) -> str:
    """Infer client/server scope from an env-var-style name."""
    return "client" if any(name.startswith(p) for p in CLIENT_PREFIXES) else "server"


def kind_for(name: str) -> str:
    """Infer a config kind from a name (heuristic, deterministic)."""
    up = name.upper()
    if any(t in up for t in ("SECRET", "PASSWORD", "PRIVATE")):
        return "secret"
    if any(t in up for t in ("API_KEY", "APIKEY", "TOKEN", "_KEY")) or up.endswith("KEY"):
        return "api_key"
    if any(t in up for t in ("URL", "URI", "ENDPOINT", "HOST", "_DSN", "ORIGIN")):
        return "url"
    if any(t in up for t in ("ENABLE", "DISABLE", "FEATURE_", "USE_", "_FLAG")):
        return "toggle"
    return "value"
