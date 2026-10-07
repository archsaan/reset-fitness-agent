"""
Weekly session planner — V2, using a member's REAL last-week attendance.

This is the richer version sketched (and deferred) in
docs/weekly_goal_rules_DRAFT.md's "V2" section — it was put off there for
lack of real last-week/wellness data. Your colleague's prompt shows
exactly the shape of data that's actually available (goal + last week's
per-category session counts), so this builds that V2 now.

SAME PRINCIPLE AS recommend_weekly_goal() (the V1 function in agent.py)
AND EVERYWHERE ELSE IN THIS PROJECT: this is a bounded rule-based/
constraint-satisfaction problem — respecting REAL per-category booking
slot caps, nudging only the goal-relevant categories, never jumping more
than one session/week — so it's a plain function, not an LLM call. An
LLM asked to do this arithmetic directly (as in the colleague's prompt)
can silently produce a number above a category's real slot cap, isn't
reproducible (same input, different output on a retry), and isn't
auditable (no real mechanism to point to if a member or your team asks
"why 3 Strength sessions?"). This function is deterministic, so the
answer to that question is always "here's the exact rule," not "the
model guessed."

The one place AI still earns a spot in this flow: turning these finished
numbers into the 30-40 word rationale sentence the colleague's prompt
also asked for. See rationale_v2() at the bottom — it has a non-LLM
fallback (always correct, a bit dry) and shows exactly what a short LLM
call should receive if you want a warmer sentence: the numbers only,
never asked to invent or change them.
"""

from __future__ import annotations

# Real per-category weekly booking slot caps — from the class catalog
# your colleague included (e.g. "Cardio Sessions (1 slot/week)"). Update
# this table if ProfitConnect's real caps ever change; nothing else in
# this file needs to change alongside it.
CATEGORY_CAPS: dict[str, int] = {
    "Cardio": 1,
    "Strength": 2,
    "Shape": 2,
    "Strength & Conditioning": 2,
    "Wellness": 2,
    "Red Light Therapy": 5,
}

# Red Light Therapy is recovery/recharge support, not tied to a specific
# fitness goal the way the others are — it's held steady at whatever the
# member is already doing (or a modest default if they've never booked
# one), never actively grown just because of their stated goal.
_SUPPORTIVE_CATEGORIES = {"Red Light Therapy"}
_DEFAULT_SUPPORTIVE_BASELINE = 2

# Goal -> which of the REAL bookable categories above are relevant. A
# separate table from member_goal_setter/agent.py's _GOAL_TO_CATEGORIES
# on purpose: that one maps onto the full class-catalog categories
# (Mind & Body, Bounce, etc. — see docs/class_recommendation_rules_DRAFT
# .md), this one only ever needs to reference the 6 categories that
# actually have a weekly slot cap above.
_GOAL_TO_V2_CATEGORIES: dict[str, list[str]] = {
    "Weight loss": ["Cardio", "Shape"],
    "Lose fat (inches)": ["Cardio", "Shape"],
    "Muscle tone": ["Shape", "Strength"],
    "Build strength": ["Strength", "Strength & Conditioning"],
    "Improve flexibility": ["Wellness"],
    "Improve cardio performance": ["Cardio"],
    "Improve stamina": ["Cardio", "Strength & Conditioning"],
    "Gain weight/muscle": ["Strength", "Strength & Conditioning"],
    "Improve overall health": ["Cardio", "Strength", "Wellness"],
    "Boost energy": ["Cardio", "Wellness"],
}
_DEFAULT_V2_CATEGORIES = ["Cardio", "Strength"]


def recommend_weekly_sessions_v2(fitness_goal: str, last_week_sessions: dict[str, int]) -> dict:
    """Recommends this week's per-category session counts from a
    member's stated goal and their REAL last week's attendance.
    Deterministic — same two inputs always produce the same output.

    Args:
        fitness_goal: one of the exact questionnaire option strings
            (e.g. "Weight loss") — see MEMBER_GOAL_SETTER_SYSTEM_PROMPT
            in common/config.py for the full valid list. An unrecognized
            value falls back to a safe default rather than erroring.
        last_week_sessions: {category_name: session_count} for however
            many of the 6 CATEGORY_CAPS categories the member actually
            attended last week. A missing category is treated as 0; a
            count above that category's real cap is clamped down to the
            cap (never trust an input number over the real limit).

    Returns:
        A dict with:
          - goal_type: the goal as given (or "General fitness" if unrecognized)
          - total_sessions_per_week: sum across all categories
          - category_targets: {category_name: sessions_per_week}, only
            categories with a non-zero recommendation
          - category_caps: the real slot-cap table, handed back so a
            caller (UI, rationale text) can show "2 of 2 slots used"
            without importing this module's internals separately.

    Rules applied, in order:
      1. Any goal-relevant category grows by AT MOST +1 session over last
         week, capped at its real weekly slot limit — never a bigger
         jump in one week (same safety principle as
         docs/weekly_goal_rules_DRAFT.md's V2 section: a jump bigger than
         this is meant for a trainer to decide, not to auto-apply).
      2. A non-goal category holds steady at last week's count — never
         actively grown (not what the member asked to focus on), never
         force-zeroed either (don't cancel something they're already
         doing consistently).
      3. Red Light Therapy (recovery support) always holds steady at
         last week's count, or starts at a modest default if they've
         never booked one — it's not goal-driven growth.
    """
    goal_categories = set(_GOAL_TO_V2_CATEGORIES.get(fitness_goal, _DEFAULT_V2_CATEGORIES))
    targets: dict[str, int] = {}

    for category, cap in CATEGORY_CAPS.items():
        last = max(0, int(last_week_sessions.get(category, 0) or 0))
        last = min(last, cap)  # never trust an input above the real cap

        if category in _SUPPORTIVE_CATEGORIES:
            targets[category] = last if last > 0 else min(_DEFAULT_SUPPORTIVE_BASELINE, cap)
        elif category in goal_categories:
            targets[category] = min(last + 1, cap)
        else:
            targets[category] = last

    return {
        "goal_type": fitness_goal or "General fitness",
        "total_sessions_per_week": sum(targets.values()),
        "category_targets": {k: v for k, v in targets.items() if v > 0},
        "category_caps": dict(CATEGORY_CAPS),
    }


def rationale_v2(fitness_goal: str, last_week_sessions: dict[str, int], result: dict) -> str:
    """Non-LLM fallback rationale — always correct (built straight from
    the same numbers above), just a bit mechanical/dry. Use this as-is,
    OR pass fitness_goal/last_week_sessions/result's category_targets to
    a short LLM call whose ONLY job is rephrasing this into a warmer
    30-40 word sentence — never asked to change or invent the numbers
    themselves. That split (code decides, model explains) is the same
    one used in member_goal_setter/agent.py's MEMBER_GOAL_SETTER_SYSTEM_
    PROMPT.
    """
    grown = [
        cat for cat, target in result["category_targets"].items()
        if target > last_week_sessions.get(cat, 0) and cat not in _SUPPORTIVE_CATEGORIES
    ]
    if grown:
        growth_text = " and ".join(grown)
        return (
            f"Building on last week, {growth_text} increased to keep progressing toward "
            f"your {result['goal_type'].lower()} goal, while other sessions held steady to "
            f"keep the week sustainable."
        )
    return (
        f"Your sessions are staying at last week's level — a solid, sustainable pace for "
        f"your {result['goal_type'].lower()} goal."
    )