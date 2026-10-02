"""
Stable, non-experimental alternative to ADK's EventsCompactionConfig.

google.adk.apps.app.EventsCompactionConfig and LlmEventSummarizer both
work, but ADK itself flags them: constructing an EventsCompactionConfig
prints `UserWarning: [EXPERIMENTAL] EventsCompactionConfig: This feature
is experimental and may change or be removed in future versions without
notice.` That's not something to depend on in production if there's a
stable way to get the same result — and there is.

The trick is Agent(include_contents="none") — confirmed a plain, stable,
NOT experimentally-flagged field on LlmAgent (a
`Literal['default', 'none']`, default `'default'`, in
google/adk/agents/llm_agent.py). With that set, ADK's own content-
assembly step (_ContentLlmRequestProcessor) sends the model NOTHING from
session history automatically — every turn starts blank except for the
current message and whatever the instruction text says.

So instead of letting ADK manage history rotation for us (experimental),
this file rebuilds exactly the context we want, by hand, and both
agents inject it into their instruction template as
{conversation_context} — the same mechanism already used for
{kb_context} (see booking_gate.py). This is more code to own, but all of
it sits on documented, stable ADK surface:
  - include_contents="none" (stable LlmAgent field)
  - before_agent_callback as a LIST (stable — LlmAgent.before_agent_callback
    accepts Callable | list[Callable]; the pipeline runs each in order and
    stops at the first one that returns a truthy Content — see
    google/adk/agents/base_agent.py's _handle_before_agent_callback /
    _stop_on_truthy). tribe_app/agent.py and pfc/agent.py register
    make_resolve_pending_booking first, then make_refresh_conversation_context
    from this file second — so on a turn where a booking is being
    resolved (resolve_pending_booking returns a Content and skips the
    model entirely), the context refresh below is skipped too, since
    there's no model turn to prepare it for.
  - session.events, a plain list already read elsewhere in this project
    (session_stats.py reads the same underlying `events` Postgres table
    that DatabaseSessionService populates) — not an internal/undocumented
    detail invented for this file.
  - A one-shot llm.generate_content_async(LlmRequest(...)) call — the
    exact same method ADK's own flow calls internally, just invoked here
    directly for a plain summarization prompt instead of a real agent
    turn (so it never itself becomes part of session history).

Per-turn behavior (see build_conversation_context):
  1. Take the session's real event history (still fully persisted in
     Postgres exactly as before — this only changes what's SENT to the
     model, never what's stored).
  2. If total events are still under ROLLING_SUMMARY_TRIGGER_EVENTS, just
     format the last ROLLING_SUMMARY_RECENT_EVENTS turns as plain text —
     no summarization call, keeps the common case (short conversations)
     free.
  3. Past that trigger, condense everything OLDER than the recent window
     into one short paragraph via a single small LLM call, and cache it
     in session.state["conversation_summary"] together with how many
     older events it covers (session.state persists to Postgres too, so
     this survives restarts and only re-summarizes once genuinely new
     older events accumulate — see _covers_enough).
  4. {conversation_context} becomes the cached summary (if any) followed
     by the verbatim recent turns — that's the model's entire view of
     history now, replacing what include_contents="default" used to send
     in full every time.
"""

import logging

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from common.config import (
    ROLLING_SUMMARY_RECENT_EVENTS,
    ROLLING_SUMMARY_TRIGGER_EVENTS,
    resolve_llm_instance,
)

logger = logging.getLogger("reset_fitness_adk.rolling_summary")

_SUMMARY_PROMPT_TEMPLATE = """Summarize the EARLIER part of a conversation between a Reset Fitness assistant and a member or staff user, in 3-5 plain sentences. Cover: who they are if mentioned, what they've asked about, checked, or booked, any stated preferences, and anything left unresolved. This summary will be handed back to the assistant as its only memory of this part of the conversation, so keep it factual and concrete rather than vague.

--- EARLIER CONVERSATION ---
{transcript}
--- END ---
"""


def _event_text(event) -> str:
    content = getattr(event, "content", None)
    if not content or not content.parts:
        return ""
    return "".join(part.text or "" for part in content.parts if getattr(part, "text", None))


def _format_turn(event) -> str:
    # ADK events use `author`: "user" for the human side, the agent's own
    # name (e.g. "reset_fitness_tribe_app") for model turns.
    speaker = "Member" if getattr(event, "author", "user") == "user" else "Assistant"
    text = _event_text(event)
    return f"{speaker}: {text}" if text else ""


def _transcript(events) -> str:
    return "\n".join(t for t in (_format_turn(e) for e in events) if t)


async def _summarize_events(events, model_name: str) -> str:
    transcript = _transcript(events)
    if not transcript:
        return ""
    llm = resolve_llm_instance(model_name)
    prompt = _SUMMARY_PROMPT_TEMPLATE.format(transcript=transcript)
    request = LlmRequest(
        model=model_name,
        contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
    )
    # A plain one-shot generation call, bypassing the agent/tool-call loop
    # entirely — this is a summarization request, not a real conversation
    # turn, and must never itself get appended to session history.
    chunks = []
    async for response in llm.generate_content_async(request):
        if response.content and response.content.parts:
            chunks.append(
                "".join(p.text or "" for p in response.content.parts if p.text)
            )
    return "".join(chunks).strip()


async def build_conversation_context(session, model_name: str) -> str:
    """Returns text ready to drop straight into {conversation_context} in
    an instruction template. Also updates session.state in place with a
    cached summary when one is (re)computed — caller is responsible for
    actually persisting the callback_context.state it read this from
    (ADK does that automatically for a before_agent_callback's state
    writes, same as {kb_context})."""
    events = list(getattr(session, "events", None) or [])
    if not events:
        return ""

    if len(events) <= ROLLING_SUMMARY_RECENT_EVENTS:
        recent, older = events, []
    else:
        recent = events[-ROLLING_SUMMARY_RECENT_EVENTS:]
        older = events[: -ROLLING_SUMMARY_RECENT_EVENTS]

    summary = ""
    if len(events) >= ROLLING_SUMMARY_TRIGGER_EVENTS and older:
        state = session.state
        cached_summary = state.get("conversation_summary", "")
        cached_covers = state.get("conversation_summary_covers", 0)
        if cached_covers >= len(older):
            # Nothing new has aged out of the recent window since we last
            # summarized — reuse the cached text instead of re-calling the
            # model every single turn.
            summary = cached_summary
        else:
            try:
                summary = await _summarize_events(older, model_name)
                state["conversation_summary"] = summary
                state["conversation_summary_covers"] = len(older)
            except Exception:
                # Never worth failing/blanking a real turn over — degrade
                # to whatever summary (possibly none) was already cached.
                logger.exception("Rolling summary generation failed for model=%s", model_name)
                summary = cached_summary

    recent_transcript = _transcript(recent)
    parts = []
    if summary:
        parts.append(f"Summary of earlier conversation:\n{summary}")
    if recent_transcript:
        parts.append(f"Recent messages:\n{recent_transcript}")
    return "\n\n".join(parts)


def make_refresh_conversation_context(agent_slug: str, model_name: str):
    """before_agent_callback (second in the list, after
    make_resolve_pending_booking — see this module's docstring for why
    order matters). Populates {conversation_context} for this turn's
    instruction and always returns None, so it never itself short-
    circuits the model turn."""

    async def refresh_conversation_context(callback_context):
        session = getattr(callback_context, "session", None)
        if session is None:
            return None
        try:
            callback_context.state["conversation_context"] = await build_conversation_context(
                session, model_name
            )
        except Exception:
            logger.exception("Failed to refresh conversation_context for agent %s", agent_slug)
            callback_context.state.setdefault("conversation_context", "")
        return None

    return refresh_conversation_context