"""Resolve a command name to something the OS can actually execute.

``asyncio.create_subprocess_exec`` does not go through a shell, so on Windows
``CreateProcess`` only appends ``.exe`` — it does not search ``PATHEXT``. A
command installed as a ``.cmd``/``.bat`` shim (npm, npx, yarn, pnpm) or as a
PowerShell script (npm.ps1, claude.ps1 from an npm global install) therefore
fails with ``WinError 2: The system cannot find the file specified`` even though
the command works perfectly in a shell.

That produced three separate, hard-to-attribute failures in this codebase:

* every non-Codex coding CLI was unusable (``claude`` resolves to ``claude.ps1``)
* ``npm install`` failed inside the sandbox, which skipped the generated tests
  and the native build, which capped every build's score as a "degraded proof
  environment" — reported as an offline/environment problem when npm was in
  fact installed and working
* the same for any ``.cmd``-shimmed tool a stack needs

``shutil.which`` alone is not sufficient: it consults ``PATHEXT``, which
commonly lists ``.PS1``, so it can hand back the one form that cannot be
exec'd. This module prefers a directly-executable sibling.

Pure stdlib, no side effects at import.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Windows suffixes ``CreateProcess`` can launch directly, best first.
#: ``.ps1`` is deliberately absent — it is a script, not an image.
WINDOWS_EXEC_SUFFIXES: tuple[str, ...] = (".exe", ".cmd", ".bat", ".com")


def executable_shim(resolved: str) -> str:
    """Swap a non-executable Windows launcher for a runnable sibling.

    npm installs a command as three files (``claude``, ``claude.ps1``,
    ``claude.cmd``); Node ships ``npm``, ``npm.ps1`` and ``npm.cmd``. Returns
    ``resolved`` unchanged on POSIX, when it is already directly executable, or
    when no sibling exists.
    """
    if os.name != "nt" or not resolved:
        return resolved
    path = Path(resolved)
    if path.suffix.lower() in WINDOWS_EXEC_SUFFIXES:
        return resolved
    for suffix in WINDOWS_EXEC_SUFFIXES:
        sibling = (
            path.with_name(path.name + suffix) if not path.suffix
            else path.with_suffix(suffix)
        )
        try:
            if sibling.is_file():
                return str(sibling)
        except OSError:  # pragma: no cover - defensive
            continue
    return resolved


def resolve_executable(name: str) -> str:
    """Absolute path to an executable form of ``name``, or ``name`` unchanged.

    Never raises. Returning the input unchanged preserves the caller's existing
    failure handling for a genuinely missing command.
    """
    text = str(name or "").strip()
    if not text:
        return text
    # An explicit path (absolute or containing a separator) is the caller's
    # choice; only fix up its suffix if it needs it.
    if os.path.isabs(text) or ("/" in text) or ("\\" in text):
        return executable_shim(text)
    try:
        found = shutil.which(text)
    except Exception:  # noqa: BLE001 - never let lookup break a command
        found = None
    if not found:
        # On Windows shutil.which can miss a .cmd shim when PATHEXT is unusual;
        # probe PATH directly for the executable suffixes before giving up.
        if os.name == "nt":
            for directory in os.environ.get("PATH", "").split(os.pathsep):
                if not directory:
                    continue
                for suffix in WINDOWS_EXEC_SUFFIXES:
                    candidate = Path(directory) / f"{text}{suffix}"
                    try:
                        if candidate.is_file():
                            return str(candidate)
                    except OSError:
                        continue
        return text
    return executable_shim(found)


__all__ = ["WINDOWS_EXEC_SUFFIXES", "executable_shim", "resolve_executable"]
