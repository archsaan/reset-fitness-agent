"""
The ADK equivalent of app/agent/graph.py's confirm_booking_node +
should_continue in the main (LangGraph) project — this is the approval
gate that stops a real booking from ever happening without an explicit
"yes" from the member, enforced in code, not left to the model.

Why not ADK's built-in require_confirmation=True? Because it only works
with InMemorySessionService — it does not support DatabaseSessionService,
which this project needs so conversations persist in Neon across
requests and restarts (see the top-level README). So this hand-rolls the
same two-callback pattern LangGraph's interrupt()/resume gives for free:

  1. before_tool_callback (intercept_book_class) — runs the instant the
     model tries to call book_class. Never lets the real HTTP calls run
     from here. Instead it stashes the proposed booking in session state
     and hands back a question for the model to relay, exactly like
     confirm_booking_node's `interrupt(...)` pausing before doing real work.

  2. before_agent_callback (resolve_pending_booking) — runs at the START
     of the *next* turn, before the model sees anything. If a booking is
     pending, this reads the member's new message directly (the ADK
     equivalent of `interrupt()`'s resume value), decides yes/no with the
     exact same plain keyword check as the main project's
     _is_affirmative (deliberately NOT an LLM judgment call — see that
     function's own comment for why), and — if confirmed — calls
     execute_booking() directly. No model involvement in that decision at
     all, same as the main project.

Both agents (tribe_app, pfc) register both callbacks identically; see
their agent.py files.
"""

import logging

from google.genai import types

from common.booking_tools import execute_booking
from common.kb import get_kb_context, log_usage

logger = logging.getLogger("reset_fitness_adk.booking_gate")

# Deny-by-default — identical wordlists to app/agent/graph.py's
# _is_affirmative, on purpose: this gates a real booking, so an ambiguous
# reply must never resolve to "go ahead".
_AFFIRMATIVE_PHRASES = {
    "yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "sure",
    "ok", "okay", "correct", "that's right", "sounds good", "go ahead",
    "book it", "please book it", "confirm it", "yes please", "yes book it",
}
_AFFIRMATIVE_FIRST_WORDS = {
    "yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "sure",
    "ok", "okay", "correct",
}


def _is_affirmative(reply: str) -> bool:
    normalized = (reply or "").strip().lower().rstrip(".!")
    if not normalized:
        return False
    if normalized in _AFFIRMATIVE_PHRASES:
        return True
    first_word = normalized.split()[0]
    return first_word in _AFFIRMATIVE_FIRST_WORDS


def _latest_user_text(callback_context) -> str:
    content = getattr(callback_context, "user_content", None)
    if content and content.parts:
        return "".join(part.text or "" for part in content.parts)
    return ""


def _format_booking_result(result: dict) -> str:
    if result.get("success"):
        b = result["booking"]
        return (
            f"You're booked! {b['class_name']} on {b['date']} at {b['start_time']} "
            f"for {b['lead_name']}. See you there! \U0001F389"
        )
    return f"I couldn't complete that booking: {result.get('reason', 'unknown error')}"


def make_intercept_book_class(agent_slug: str):
    """before_tool_callback. Stops book_class from ever running for
    real — stashes the proposed args in session state and returns the
    confirmation question as the tool's "result" instead, which the
    model relays to the member (its instruction tells it to)."""

    def intercept_book_class(tool, args, tool_context):
        if tool.name != "book_class":
            return None  # not our tool — let every other tool run normally

        tool_context.state["pending_booking"] = dict(args)
        CONFIRM_SENTINEL = "[confirm_buttons]"

        question = (
            f"Please confirm: book {args.get('class_name')} on {args.get('target_date')} "
            f"at {args.get('start_time')} for {args.get('lead_name')}? (yes/no)"
            f"\n{CONFIRM_SENTINEL}"
        )
        return {"success": False, "awaiting_confirmation": True, "question": question}

    return intercept_book_class


def make_resolve_pending_booking(agent_slug: str):
    """before_agent_callback. Runs before the model sees the member's
    next message. If a booking is pending, this decides the outcome
    itself — deterministically, from the plain-keyword check above, never
    an LLM judgment call — executes the real booking if confirmed, and
    returns the final reply directly, skipping the model turn entirely
    (the ADK equivalent of resuming a paused LangGraph interrupt straight
    into confirm_booking_node). Otherwise it just refreshes this turn's
    KB context and lets the model run as normal."""

    def resolve_pending_booking(callback_context):
        state = callback_context.state
        user_text = _latest_user_text(callback_context)

        pending = state.get("pending_booking")
        if pending:
            state["pending_booking"] = None  # clear regardless of outcome
            if _is_affirmative(user_text):
                try:
                    result = execute_booking(**pending)
                except Exception:
                    # A real HTTP call to ProfitConnect that times out or
                    # errors must never crash the whole turn — the member
                    # would just see a broken chat with no explanation.
                    # Logged loudly (this is exactly the kind of failure
                    # that needs a human to notice and check whether the
                    # booking actually went through on ProfitConnect's
                    # side despite the error), and degrades to a plain
                    # apologetic reply instead.
                    logger.exception(
                        "execute_booking raised for agent_slug=%s pending=%s", agent_slug, pending
                    )
                    result = {
                        "success": False,
                        "reason": "Something went wrong reaching the booking system. Please try again in a moment.",
                    }
            else:
                result = {
                    "success": False,
                    "reason": "The member did not confirm, so this booking was not made.",
                }
            reply_text = _format_booking_result(result)
            return types.Content(role="model", parts=[types.Part(text=reply_text)])

        # No pending booking — proceed to the model as normal, but first
        # refresh {kb_context} for this turn's instruction (the ADK
        # equivalent of build_system_prompt() re-running every request).
        state["kb_context"] = get_kb_context(agent_slug, user_text)
        return None

    return resolve_pending_booking


def make_log_usage(agent_slug: str, model_name: str):
    """after_model_callback — logs each real model call's token usage to
    usage_logs, the ADK equivalent of graph.py's log_usage() call in the
    main project (called from agent_node there, after every model
    invoke). Read-only w.r.t. the response: always returns None so the
    actual model output is never altered, only observed."""

    def log_usage_callback(callback_context, llm_response):
        usage = getattr(llm_response, "usage_metadata", None)
        if usage is None:
            return None
        session = getattr(callback_context, "session", None)
        session_id = getattr(session, "id", "unknown")
        try:
            log_usage(
                agent_slug,
                session_id,
                model_name,
                usage.prompt_token_count or 0,
                usage.candidates_token_count or 0,
            )
        except Exception:
            # Usage logging is an observability nice-to-have, never worth
            # failing a real chat turn over — but a silently swallowed
            # DB error is itself an observability gap (you'd never know
            # usage_logs stopped filling in), so it's logged, not passed.
            logger.exception("Failed to log usage for agent %s, session %s", agent_slug, session_id)
        return None

    return log_usage_callback
