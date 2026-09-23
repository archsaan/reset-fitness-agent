"""
Root agent definition — the ADK equivalent of app/agent/graph.py in the
main (LangGraph) project.

Structural comparison to the real project, so it's worth reading side by
side with app/agent/graph.py:
  - LangGraph: you build the graph by hand (nodes, edges, a conditional
    router) and the approval gate is a node you write yourself, calling
    interrupt()/Command(resume=...) explicitly.
  - ADK: Agent + tools is closer to "batteries included" — the
    agent loop, tool-calling, and (via require_confirmation=True on a
    tool) the pause/resume gate are all framework behavior. Less code to
    write; also less visibility into exactly how the loop works under
    the hood. Worth noticing which style you prefer once you've run both.

Model: Claude (via LiteLlm), for a closer comparison to the real
(LangGraph) project, which also runs on Claude. This needs
`pip install "google-adk[extensions]"` and an ANTHROPIC_API_KEY in
booking_agent/.env — see the commented block below for switching back
to Gemini (no extra install, free key) if you want the fastest path
instead.
"""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import FunctionTool

from booking_agent.tools import book_class, check_availability, get_schedule

INSTRUCTION = (
    "You are Riley, the Reset Fitness AI Assistant (ADK learning build). "
    "Help members check the class schedule and book classes. Be warm, "
    "concise, and never invent schedule information — always use "
    "get_schedule or check_availability, which return real (mocked) data. "
    "Use book_class only after check_availability has confirmed there is "
    "space, and only once you have the member's name. Always pass the "
    "exact calendar_schedule_id that check_availability returned."
)

# book_class needs the confirmation gate; the read-only tools don't.
# require_confirmation=True means ADK's own tool-calling loop pauses
# before this function ever runs, and only runs it once a human has
# approved — see tools.py's book_class docstring for what this buys you.
book_class_tool = FunctionTool(func=book_class, require_confirmation=True)

root_agent = Agent(
    name="reset_fitness_booking_agent",
    model=LiteLlm(model="anthropic/claude-sonnet-4-5"),
    # --- To switch back to Gemini (fastest path, no extra install) ---
    # 1. Remove the model= line above and the LiteLlm import at the top
    # 2. model="gemini-2.5-flash",
    # 3. Put GOOGLE_API_KEY in booking_agent/.env instead
    instruction=INSTRUCTION,
    description="Helps Reset Fitness members check class schedules and book classes.",
    tools=[get_schedule, check_availability, book_class_tool],
)
