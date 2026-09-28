"""
Prompt-injection AWARENESS — deliberately not a blocker.

Why detect-and-log instead of detect-and-block: a regex/keyword filter on
free-text member messages produces real false positives (a member typing
"ignore the cancellation fee, can I still book?" is a normal question, not
an attack), and a real attack rarely uses the exact phrases below anyway.
Silently blocking a legitimate member over a false positive is a worse
outcome than letting a flagged message through — the actual safety net
against an attack succeeding is RULES in common/config.py (the model is
told never to comply with an embedded instruction) and the booking gate
in booking_gate.py (a real booking can never happen without a real
ProfitConnect validate+book call, no matter what the model says). This
module exists purely for VISIBILITY: so you can grep Render's logs for
"possible prompt injection" and see who's trying, and refine RULES or add
a harder block later if a real pattern of abuse shows up — the same
build-the-cheap-version-first reasoning as main.py's health check and
request logging.
"""

import logging

logger = logging.getLogger("reset_fitness_adk.safety")

# Deliberately broad and cheap (substring match, not an LLM call) — this
# is a tripwire for logging, not a security boundary, so false positives
# here cost nothing (a log line) while false negatives just mean an
# attack attempt goes unlogged, not unblocked (RULES/booking gate still
# apply regardless of whether this catches it).
_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "ignore the above",
    "ignore all previous",
    "disregard previous",
    "disregard the above",
    "disregard your instructions",
    "forget your instructions",
    "forget the above",
    "new instructions:",
    "system prompt",
    "you are now",
    "act as a",
    "act as if",
    "pretend you are",
    "pretend to be",
    "reveal your instructions",
    "reveal your prompt",
    "repeat your instructions",
    "print your instructions",
    "what are your instructions",
    "what is your system prompt",
    "developer mode",
    "jailbreak",
    "override your",
    "bypass your",
    "do anything now",
)


def detect_injection_attempt(text: str) -> str | None:
    """Returns the matched pattern if `text` looks like an attempt to
    manipulate the agent's instructions, else None. Case-insensitive
    substring match — cheap enough to run on every single turn."""
    if not text:
        return None
    lowered = text.lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def log_if_injection_attempt(agent_slug: str, session_id: str, user_text: str) -> None:
    """Call once per incoming member/staff message. Logs a WARNING (so it
    shows up distinctly in Render's log viewer, separate from routine
    INFO request logs) with a truncated snippet — truncated so a very
    long pasted message doesn't blow up log lines, and so this file never
    accidentally becomes a place real member PII gets dumped in full."""
    matched = detect_injection_attempt(user_text)
    if matched:
        snippet = user_text.strip().replace("\n", " ")[:200]
        logger.warning(
            "Possible prompt injection attempt — agent=%s session=%s matched=%r text=%r",
            agent_slug, session_id, matched, snippet,
        )