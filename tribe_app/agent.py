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
from common.schedule_tools import check_availability, get_schedule

AGENT_SLUG = "tribe-app"


async def build_instruction(readonly_context: ReadonlyContext) -> str:
    """Instruction provider (ADK calls this fresh every turn, instead of
    using a static string) — two things need to be live per-turn, not
    baked in at import time:
      1. {kb_context}, filled by booking_gate.resolve_pending_booking
         (a before_agent_callback) into session state before this runs.
      2. The persona text itself, which an admin can now edit from the
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
        + "\n\n--- RULES ---\n" + RULES
    )
    return await inject_session_state(template, readonly_context)


root_agent = Agent(
    name="reset_fitness_tribe_app",
    model=resolve_model(TRIBE_APP_MODEL),
    instruction=build_instruction,
    description="Helps Reset Fitness app members check class schedules and book classes.",
    tools=[get_schedule, check_availability, book_class],
    before_agent_callback=make_resolve_pending_booking(AGENT_SLUG),
    before_tool_callback=make_intercept_book_class(AGENT_SLUG),
    after_model_callback=make_log_usage(AGENT_SLUG, TRIBE_APP_MODEL),
)
