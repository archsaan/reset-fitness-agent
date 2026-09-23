"""
The PFC (staff-facing) agent — ADK equivalent of the "pfc" row in the
main project's DEFAULT_AGENTS. Same tools and confirmation gate as
tribe_app, different system prompt/audience (trained staff, not members)
and its own isolated KB (kb_docs.agent_slug = "pfc" never mixes with
tribe-app's).
"""

from google.adk.agents import Agent

from common.booking_gate import make_intercept_book_class, make_log_usage, make_resolve_pending_booking
from common.booking_tools import book_class
from common.config import PFC_MODEL, PFC_SYSTEM_PROMPT, RULES, resolve_model
from common.schedule_tools import check_availability, get_schedule

AGENT_SLUG = "pfc"

INSTRUCTION = (
    PFC_SYSTEM_PROMPT
    + "\n\n--- KNOWLEDGE BASE ---\n{kb_context}"
    + "\n\n--- RULES ---\n" + RULES
)

root_agent = Agent(
    name="reset_fitness_pfc",
    model=resolve_model(PFC_MODEL),
    instruction=INSTRUCTION,
    description="Helps Reset Fitness studio staff with schedule lookups and CRM tasks.",
    tools=[get_schedule, check_availability, book_class],
    before_agent_callback=make_resolve_pending_booking(AGENT_SLUG),
    before_tool_callback=make_intercept_book_class(AGENT_SLUG),
    after_model_callback=make_log_usage(AGENT_SLUG, PFC_MODEL),
)