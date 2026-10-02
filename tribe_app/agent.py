"""
Riley — the member-facing Tribe App agent. ADK equivalent of the
"tribe-app" row in the main project's `agents` table (app/core/config.py's
DEFAULT_AGENTS), rebuilt as ADK's Agent + tools instead of a DB-configured
LangGraph graph.

Real schedule + booking APIs (common/schedule_tools.py,
common/booking_tools.py), a hand-rolled confirmation gate for book_class
(common/booking_gate.py — see that file for why it's not
require_confirmation=True), and per-turn KB retrieval via {kb_context}.
"""

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.utils.instructions_utils import inject_session_state

from common.agent_config import get_system_prompt
from common.booking_gate import make_intercept_book_class, make_log_usage, make_resolve_pending_booking
from common.booking_tools import book_class
from common.config import RULES, TRIBE_APP_MODEL, TRIBE_APP_SYSTEM_PROMPT, resolve_model
from common.rolling_summary import make_refresh_conversation_context
from common.schedule_tools import check_availability, get_schedule

AGENT_SLUG = "tribe-app"


async def build_instruction(readonly_context: ReadonlyContext) -> str:
    """Instruction provider (ADK calls this fresh every turn, instead of
    using a static string) — two things need to be live per-turn, not
    baked in at import time:
      1. {kb_context}, filled by booking_gate.resolve_pending_booking
         (a before_agent_callback) into session state before this runs.
      2. {conversation_context}, filled by rolling_summary's
         refresh_conversation_context (the SECOND before_agent_callback
         below) — this agent uses include_contents="none" so ADK sends
         NO automatic history; this is the model's entire view of the
         conversation so far. See common/rolling_summary.py's module
         docstring for the full rationale (a stable alternative to ADK's
         experimental EventsCompactionConfig).
      3. The persona text itself, which an admin can now edit from the
         dashboard's Config panel (common/agent_config.py) and have it
         take effect on the very next message — no redeploy. Safety-
         critical RULES stay hardcoded below, appended after whatever
         persona is currently active, so an admin edit can never remove
         the guardrails around real bookings — see agent_config.py's
         module docstring for why that split matters."""
    persona = get_system_prompt(AGENT_SLUG, default=TRIBE_APP_SYSTEM_PROMPT)
    template = (
        persona
        + "\n\n--- KNOWLEDGE BASE ---\n{kb_context}"
        + "\n\n--- CONVERSATION SO FAR ---\n{conversation_context}"
        + "\n\n--- RULES ---\n" + RULES
    )
    return await inject_session_state(template, readonly_context)


root_agent = Agent(
    name="reset_fitness_tribe_app",
    model=resolve_model(TRIBE_APP_MODEL),
    instruction=build_instruction,
    description="Helps Reset Fitness app members check class schedules and book classes.",
    tools=[get_schedule, check_availability, book_class],
    # "none" — a stable, non-experimental LlmAgent field (see
    # rolling_summary.py) — turns off ADK's automatic full-history
    # resend; {conversation_context} above is this agent's only memory
    # of earlier turns, rebuilt every turn by the second callback below.
    include_contents="none",
    # Order matters: a list of before_agent_callbacks runs in order and
    # stops at the first one that returns a real reply (ADK's
    # _stop_on_truthy). make_resolve_pending_booking runs first and, on a
    # turn where it resolves a pending booking, returns the final reply
    # directly and skips the model turn entirely — in which case
    # refreshing {conversation_context} would be wasted work, so it never
    # runs that turn. On every other turn resolve_pending_booking returns
    # None and refresh_conversation_context runs next, preparing this
    # turn's history for the model.
    before_agent_callback=[
        make_resolve_pending_booking(AGENT_SLUG),
        make_refresh_conversation_context(AGENT_SLUG, TRIBE_APP_MODEL),
    ],
    before_tool_callback=make_intercept_book_class(AGENT_SLUG),
    after_model_callback=make_log_usage(AGENT_SLUG, TRIBE_APP_MODEL),
)