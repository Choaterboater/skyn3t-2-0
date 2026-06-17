"""Test-session defaults.

Pin the LLM backend to the deterministic offline ``stub`` so the suite never
shells out to a locally-installed CLI (claude/kimi/copilot) or the network —
tests stay fast, hermetic, and reproducible regardless of what's on the host.
"""

import os

os.environ["SKYN3T_LLM_BACKEND"] = "stub"

try:  # settings may have been imported+cached already
    from skyn3t.config.settings import get_settings

    get_settings.cache_clear()
except Exception:  # pragma: no cover
    pass
