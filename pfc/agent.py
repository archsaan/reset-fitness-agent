"""
The PFC (staff-facing) agent — ADK equivalent of the "pfc" row in the
main project's DEFAULT_AGENTS. Same tools and confirmation gate as
tribe_app, different system prompt/audience (trained staff, not members)
and its own isolated KB (kb_docs.agent_slug = "pfc" never mixes with
tribe-app's).
"""

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.utils.instructions_utils import inject_session_state

from common.agent_config import get_system_prompt
from common.booking_gate import make_intercept_book_class, make_log_usage, make_resolve_pending_booking
from common.booking_tools import book_class
from common.config import PFC_MODEL, PFC_SYSTEM_PROMPT, RULES, resolve_model
from common.rolling_summary import make_refresh_conversation_context
from common.schedule_tools import check_availability, get_schedule

AGENT_SLUG = "pfc"


async def build_instruction(readonly_context: ReadonlyContext) -> str:
    """See tribe_app/agent.py's build_instruction for the full rationale
    (live-editable persona from the dashboard, RULES always hardcoded,
    {conversation_context} as the stable rolling-summary alternative to
    ADK's experimental EventsCompactionConfig)."""
    persona = get_system_prompt(AGENT_SLUG, default=PFC_SYSTEM_PROMPT)
    template = (
        persona
        + "\n\n--- KNOWLEDGE BASE ---\n{kb_context}"
        + "\n\n--- CONVERSATION SO FAR ---\n{conversation_context}"
        + "\n\n--- RULES ---\n" + RULES
    )
    return await inject_session_state(template, readonly_context)


root_agent = Agent(
    name="reset_fitness_pfc",
    model=resolve_model(PFC_MODEL),
    instruction=build_instruction,
    description="Helps Reset Fitness studio staff with schedule lookups and CRM tasks.",
    tools=[get_schedule, check_availability, book_class],
    # See tribe_app/agent.py for the full rationale on include_contents
    # and the before_agent_callback ordering (identical here).
    include_contents="none",
    before_agent_callback=[
        make_resolve_pending_booking(AGENT_SLUG),
        make_refresh_conversation_context(AGENT_SLUG, PFC_MODEL),
    ],
    before_tool_callback=make_intercept_book_class(AGENT_SLUG),
    after_model_callback=make_log_usage(AGENT_SLUG, PFC_MODEL),
)