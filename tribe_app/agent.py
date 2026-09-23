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

from common.booking_gate import make_intercept_book_class, make_log_usage, make_resolve_pending_booking
from common.booking_tools import book_class
from common.config import RULES, TRIBE_APP_MODEL, TRIBE_APP_SYSTEM_PROMPT, resolve_model
from common.schedule_tools import check_availability, get_schedule

AGENT_SLUG = "tribe-app"

# {kb_context} is filled in every turn by
# booking_gate.resolve_pending_booking (a before_agent_callback), the ADK
# equivalent of build_system_prompt() re-running per request in the main
# project — an admin editing this agent's KB docs takes effect on the
# very next message, no redeploy needed.
INSTRUCTION = (
    TRIBE_APP_SYSTEM_PROMPT
    + "\n\n--- KNOWLEDGE BASE ---\n{kb_context}"
    + "\n\n--- RULES ---\n" + RULES
)

root_agent = Agent(
    name="reset_fitness_tribe_app",
    model=resolve_model(TRIBE_APP_MODEL),
    instruction=INSTRUCTION,
    description="Helps Reset Fitness app members check class schedules and book classes.",
    tools=[get_schedule, check_availability, book_class],
    before_agent_callback=make_resolve_pending_booking(AGENT_SLUG),
    before_tool_callback=make_intercept_book_class(AGENT_SLUG),
    after_model_callback=make_log_usage(AGENT_SLUG, TRIBE_APP_MODEL),
)