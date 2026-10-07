"""
Member Goal Setter — a conversational sub-agent that ASKS the member their
fitness goal and workout frequency, then uses a deterministic rule table
(not the LLM) to recommend their first weekly goal.

This replaces the earlier version of this file, which expected
fitness_goal/fitness_frequency to already be in session.state (set by some
external caller). Per your call, this agent now collects them itself
through conversation — "agent can ask the goal and my answers to
questionnaire then use rules recommend weekly goal."

WHY THIS NOW NEEDS AN LLM, WHEN THE DECISION STILL DOESN'T: asking "what's
your fitness goal?" in a chat gets free-text answers ("I wanna lose some
weight", "get stronger I guess") — matching that to one of the exact
questionnaire option strings (recommend_weekly_goal only understands
"Weight loss", not "lose some weight") is a real language-understanding
task. The RECOMMENDATION ITSELF is still a pure lookup — see
recommend_weekly_goal() below, completely unchanged from the deterministic
version, still independently testable with no LLM involved. The split is
exactly the same principle as everywhere else in this project: the model's
job stops at "figure out which exact option the member meant," then a
plain function makes the actual decision. See
MEMBER_GOAL_SETTER_SYSTEM_PROMPT in common/config.py for the instruction
that enforces this — it explicitly tells the model to call the tool rather
than invent a goal itself.

WHY NO BOOKING GATE / KB / ROLLING SUMMARY, UNLIKE tribe_app/pfc/ai_coach:
this agent's entire job is a short, bounded two-question exchange, not an
open-ended chat that could run for a long time or touch booking — the
machinery those agents need for long conversations and irreversible
actions doesn't apply here. If this agent's scope grows later (e.g. it
starts also helping book based on the goal), revisit this.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext

from common.agent_config import get_system_prompt
from common.booking_gate import make_log_usage
from common.config import MEMBER_GOAL_SETTER_MODEL, MEMBER_GOAL_SETTER_SYSTEM_PROMPT, resolve_model

AGENT_SLUG = "member-goal-setter"

# ---------------------------------------------------------------------------
# Step 1 — goal -> preferred categories, from docs/class_recommendation_rules
# _DRAFT.md's mapping table. DRAFT: review/replace alongside that doc.
# ---------------------------------------------------------------------------
_GOAL_TO_CATEGORIES: dict[str, list[str]] = {
    "Weight loss": ["Cardio", "Shape"],
    "Lose fat (inches)": ["Cardio", "Shape"],
    "Muscle tone": ["Shape", "Strength & Conditioning"],
    "Build strength": ["Strength", "Strength & Conditioning"],
    "Improve flexibility": ["Mind & Body", "Wellness"],
    "Improve cardio performance": ["Cardio"],
    "Improve stamina": ["Cardio", "Strength & Conditioning"],
    "Gain weight/muscle": ["Strength", "Strength & Conditioning"],
    "Improve overall health": ["Cardio", "Strength", "Mind & Body"],
    "Boost energy": ["Cardio", "Bounce", "Mind & Body"],
}
_DEFAULT_CATEGORIES = ["Cardio", "Strength"]  # fallback for an unrecognized/missing goal

# ---------------------------------------------------------------------------
# Step 4 — frequency -> total weekly sessions, from the same doc's frequency
# table. Picks one concrete number per answer (the doc gives ranges); for
# "Never" we start at 1, not 0, since this is what the member is being
# asked to aim for, not a restatement of their current habit.
# ---------------------------------------------------------------------------
_FREQUENCY_TO_SESSIONS: dict[str, int] = {
    "1-2 times / week": 2,
    "3-4 times / week": 3,
    "5-6 times / week": 5,
    "Daily": 5,  # capped below the literal "daily" ask — see safety bounds
    "Never": 1,
}
_DEFAULT_SESSIONS = 2  # used when frequency is missing/unrecognized

# Same hard safety bounds as docs/weekly_goal_rules_DRAFT.md's V2 rules,
# applied here too for consistency even though V1 is much simpler.
_MIN_TOTAL_SESSIONS = 1
_MAX_TOTAL_SESSIONS = 6
_MAX_PER_CATEGORY = 3


def recommend_weekly_goal(fitness_goal: str, fitness_frequency: str) -> dict:
    """Recommends a member's first weekly fitness goal from their stated
    goal and workout frequency. Deterministic lookup — always returns the
    same output for the same two inputs, never guesses or varies.

    Call this with the member's answers mapped to the EXACT option strings
    from the questionnaire (e.g. "Weight loss", not "lose weight") — see
    MEMBER_GOAL_SETTER_SYSTEM_PROMPT for the full valid lists. An
    unrecognized string still returns a safe default rather than erroring.

    Args:
        fitness_goal: the member's goal, as one of the exact questionnaire
            option strings (e.g. "Weight loss", "Build strength").
        fitness_frequency: how often the member currently works out, as one
            of the exact questionnaire option strings (e.g.
            "3-4 times / week").

    Returns:
        A dict with:
          - goal_type: the goal as given (or "General fitness" if unrecognized)
          - total_sessions_per_week: recommended total session count
          - category_targets: dict of {category_name: sessions_per_week}
    """
    categories = _GOAL_TO_CATEGORIES.get(fitness_goal, _DEFAULT_CATEGORIES)
    total = _FREQUENCY_TO_SESSIONS.get(fitness_frequency, _DEFAULT_SESSIONS)
    total = max(_MIN_TOTAL_SESSIONS, min(_MAX_TOTAL_SESSIONS, total))

    # Spread total sessions across the goal's categories round-robin, capped
    # per category — e.g. 3 sessions across [Cardio, Shape] -> Cardio: 2,
    # Shape: 1. Never exceeds _MAX_PER_CATEGORY even if total allows it.
    targets = {category: 0 for category in categories}
    remaining = total
    i = 0
    while remaining > 0 and any(v < _MAX_PER_CATEGORY for v in targets.values()):
        category = categories[i % len(categories)]
        if targets[category] < _MAX_PER_CATEGORY:
            targets[category] += 1
            remaining -= 1
        i += 1

    return {
        "goal_type": fitness_goal or "General fitness",
        "total_sessions_per_week": total - remaining if remaining else total,
        "category_targets": {k: v for k, v in targets.items() if v > 0},
    }


async def build_instruction(readonly_context: ReadonlyContext) -> str:
    """Instruction provider, not a static string — matches
    tribe_app/agent.py's pattern so this agent's persona is editable from
    the admin dashboard's Config panel (common/agent_config.py) and takes
    effect on the very next message, no redeploy. There's no RULES/KB/
    rolling-summary text to append here (see this file's module
    docstring on why this agent doesn't need that machinery) — the
    editable persona IS the whole instruction."""
    return get_system_prompt(AGENT_SLUG, default=MEMBER_GOAL_SETTER_SYSTEM_PROMPT)


root_agent = Agent(
    name="member_goal_setter",
    model=resolve_model(MEMBER_GOAL_SETTER_MODEL),
    instruction=build_instruction,
    description=(
        "Asks a member their fitness goal and workout frequency, then "
        "recommends their first weekly goal using a deterministic rule "
        "table (not the model's own judgment)."
    ),
    tools=[recommend_weekly_goal],
    # Same usage-logging callback tribe_app/pfc use — gives this agent a
    # row in the dashboard's Usage panel for free, no booking/KB
    # machinery required for it to apply.
    after_model_callback=make_log_usage(AGENT_SLUG, MEMBER_GOAL_SETTER_MODEL),
)