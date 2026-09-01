"""Typed stall evidence + deterministic classification for agentic CLI sessions.

When a coding CLI (``codex exec`` / ``claude -p`` / ``kimi --prompt``) stalls, a
bare wall-clock timeout throws away the one thing an operator needs: WHY. This
module captures a bounded evidence bundle at the moment the stall is detected
(:class:`StallEvidence`) and reduces it to a typed kind via
:func:`classify_stall` — deterministic rules, first match wins:

1. ``trust_required`` — the process is sitting on an interactive
   trust/approval prompt (known phrases in the output tail). A headless build
   cannot answer it; escalate so the operator can pre-trust the folder.
2. ``worker_crashed`` — the process died on its own with a nonzero exit code,
   or crash markers (traceback/panic/segfault) appear in the stderr tail.
3. ``transport_dead`` — the process exited (or the output channel closed)
   mid-stream: bytes flowed but no terminal result event ever arrived.
4. ``prompt_misdelivery`` — zero bytes EVER received, past the acceptance
   window: the prompt was likely never accepted (argv truncation, a cmd-shim
   failure, an auth wall).
5. ``prompt_acceptance_timeout`` — the session was accepted (lifecycle events
   such as ``system``/``thread.started`` flowed) but no first content token
   arrived within the acceptance window.
6. ``heartbeat_stall`` — real output flowed, then the session went quiet past
   the idle/heartbeat window with the process still alive.

The ordering matters: a live trust prompt is the most actionable signal
(checked first), and a process that already exited on its own can never be a
delivery problem (checked before the delivery kinds). :func:`classify_stall`
and :func:`stall_report` NEVER raise — any classifier failure yields
``unknown`` with the raw evidence attached.
"""

from __future__ import annotations

from dataclasses import dataclass

STALL_TRUST_REQUIRED = "trust_required"
STALL_PROMPT_MISDELIVERY = "prompt_misdelivery"
STALL_PROMPT_ACCEPTANCE_TIMEOUT = "prompt_acceptance_timeout"
STALL_TRANSPORT_DEAD = "transport_dead"
STALL_WORKER_CRASHED = "worker_crashed"
STALL_HEARTBEAT_STALL = "heartbeat_stall"
STALL_UNKNOWN = "unknown"

STALL_KINDS = (
    STALL_TRUST_REQUIRED,
    STALL_PROMPT_MISDELIVERY,
    STALL_PROMPT_ACCEPTANCE_TIMEOUT,
    STALL_TRANSPORT_DEAD,
    STALL_WORKER_CRASHED,
    STALL_HEARTBEAT_STALL,
    STALL_UNKNOWN,
)

# How long a session may run without delivering a first content token before
# "accepted but silent" / "never delivered" becomes the working hypothesis.
# The effective window is clamped by the session's own idle timeout (a shorter
# idle guard is the tighter, operative budget).
PROMPT_ACCEPTANCE_WINDOW_S = 90.0

# Interactive trust/approval prompts the headless CLIs emit when a folder or
# action needs a human answer. Matched case-insensitively against the bounded
# output tail — the last thing the agent said before going quiet.
_TRUST_PROMPT_PHRASES = (
    "do you trust the files in this folder",
    "trust the files in this folder",
    "trust this folder",
    "do you want to proceed",
    "do you want to allow",
    "press enter to continue",
    "press enter to confirm",
    "enter to confirm",
    "are you sure you want to continue",
    "permission required",
    "requires approval",
    "waiting for approval",
    "approve? (y/n)",
    "[y/n]",
    "(y/n)",
)

# Crash signatures, matched case-insensitively against the stderr tail only
# (assistant prose legitimately discusses tracebacks; the process's own stderr
# is the crash channel).
_CRASH_MARKERS = (
    "traceback (most recent call last)",
    "panic:",
    "fatal error",
    "segmentation fault",
    "core dumped",
    "unhandled exception",
    "out of memory",
    "oomkilled",
)

_TAIL_LIMIT = 240


@dataclass
class StallEvidence:
    """Bounded bundle captured when a stall is detected. All fields are plain
    scalars/strings so the bundle can ride logs and run metadata safely."""

    provider: str = ""
    model: str = ""
    attempt: int = 0
    # Lifecycle state at detection (the cli_execution receipt's exit_status /
    # termination_reason, e.g. "streaming", "idle_timeout", "exited").
    lifecycle_state: str = ""
    bytes_received: int = 0
    events_received: int = 0
    # Parsed stream events that carry agent CONTENT (anything outside the
    # lifecycle set below) — the "first token" signal for acceptance.
    content_events: int = 0
    seconds_since_last_output: float = 0.0
    # time.monotonic() when the prompt went out (0 when unknown).
    prompt_sent_at: float = 0.0
    process_alive: bool = True
    exit_code: int | None = None
    idle_timeout: float = 0.0
    # Bounded, masked tails. output_tail feeds trust-phrase detection (stdout
    # prose + raw lines + stderr); stderr_tail feeds crash-marker detection.
    output_tail: str = ""
    stderr_tail: str = ""


def _acceptance_window(ev: StallEvidence) -> float:
    try:
        idle = float(ev.idle_timeout or 0)
    except (TypeError, ValueError):
        idle = 0.0
    if idle > 0:
        return min(PROMPT_ACCEPTANCE_WINDOW_S, idle)
    return PROMPT_ACCEPTANCE_WINDOW_S


def _nonzero_exit(exit_code) -> bool:
    return (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
    )


def _matched_phrase(text: str, phrases) -> str:
    for phrase in phrases:
        if phrase in text:
            return phrase
    return ""


def _quiet_s(ev: StallEvidence) -> float:
    try:
        quiet = float(ev.seconds_since_last_output or 0)
    except (TypeError, ValueError):
        return 0.0
    # NaN fails every comparison below; normalize it to "no wait observed".
    return quiet if quiet == quiet else 0.0


def _classify(ev: StallEvidence) -> str:
    """The ordered rules. Kept total: every input lands on exactly one kind."""
    tail = str(ev.output_tail or "").lower()
    if _matched_phrase(tail, _TRUST_PROMPT_PHRASES):
        return STALL_TRUST_REQUIRED
    stderr = str(ev.stderr_tail or "").lower()
    if (not ev.process_alive and _nonzero_exit(ev.exit_code)) or _matched_phrase(
        stderr, _CRASH_MARKERS
    ):
        return STALL_WORKER_CRASHED
    if not ev.process_alive and int(ev.bytes_received or 0) > 0:
        return STALL_TRANSPORT_DEAD
    quiet = _quiet_s(ev)
    window = _acceptance_window(ev)
    if int(ev.bytes_received or 0) <= 0 and quiet >= window:
        return STALL_PROMPT_MISDELIVERY
    if int(ev.content_events or 0) <= 0 and quiet >= window:
        return STALL_PROMPT_ACCEPTANCE_TIMEOUT
    if ev.process_alive and int(ev.content_events or 0) > 0:
        return STALL_HEARTBEAT_STALL
    return STALL_UNKNOWN


def classify_stall(evidence) -> str:
    """Classify a stall bundle into its kind. Never raises."""
    try:
        return _classify(evidence)
    except Exception:  # noqa: BLE001 - classification must never break a build
        return STALL_UNKNOWN


def _reason(kind: str, ev: StallEvidence) -> str:
    """The human-readable evidence string for a classified kind (bounded)."""
    quiet = _quiet_s(ev)
    window = _acceptance_window(ev)
    tail = str(ev.output_tail or "")[-_TAIL_LIMIT:].strip()
    stderr = str(ev.stderr_tail or "")[-_TAIL_LIMIT:].strip()
    if kind == STALL_TRUST_REQUIRED:
        phrase = _matched_phrase(tail.lower(), _TRUST_PROMPT_PHRASES)
        return (
            f"interactive trust/approval prompt detected ({phrase!r}); process "
            f"alive after {quiet:.0f}s quiet; tail: {tail[-120:]!r}"
        )
    if kind == STALL_WORKER_CRASHED:
        marker = _matched_phrase(stderr, _CRASH_MARKERS)
        detail = f"crash marker {marker!r} in stderr" if marker else "nonzero exit"
        return (
            f"worker crashed: exit_code={ev.exit_code} ({detail}); "
            f"{ev.bytes_received}B/{ev.events_received} events before death; "
            f"stderr tail: {stderr[-120:]!r}"
        )
    if kind == STALL_TRANSPORT_DEAD:
        return (
            f"transport died mid-stream: process exited (code {ev.exit_code}) "
            f"after {ev.bytes_received}B/{ev.events_received} events with no "
            f"terminal result event"
        )
    if kind == STALL_PROMPT_MISDELIVERY:
        return (
            f"zero bytes received {quiet:.0f}s after prompt send (acceptance "
            f"window {window:.0f}s); prompt likely never accepted; "
            f"process_alive={ev.process_alive}"
        )
    if kind == STALL_PROMPT_ACCEPTANCE_TIMEOUT:
        return (
            f"session accepted ({ev.bytes_received}B/{ev.events_received} "
            f"lifecycle events) but no first content token within "
            f"{window:.0f}s acceptance window"
        )
    if kind == STALL_HEARTBEAT_STALL:
        return (
            f"had output ({ev.bytes_received}B/{ev.events_received} events, "
            f"{ev.content_events} content) then quiet {quiet:.0f}s beyond the "
            f"idle window {float(ev.idle_timeout or 0):.0f}s; process_alive="
            f"{ev.process_alive}"
        )
    return f"unclassified stall; raw evidence: {ev!r}"[:_TAIL_LIMIT * 2]


def stall_report(evidence) -> dict:
    """``{stall_kind, stall_evidence}`` for an evidence bundle. Never raises;
    a classifier failure degrades to ``unknown`` with the raw evidence."""
    try:
        kind = _classify(evidence)
        return {"stall_kind": kind, "stall_evidence": _reason(kind, evidence)}
    except Exception:  # noqa: BLE001 - never raise into the build
        try:
            raw = repr(evidence)[: _TAIL_LIMIT * 2]
        except Exception:  # noqa: BLE001
            raw = "<unrepresentable evidence>"
        return {"stall_kind": STALL_UNKNOWN, "stall_evidence": raw}
