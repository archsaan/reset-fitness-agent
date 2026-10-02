"""
How to test member_goal_setter.

This agent changed since the first version: it now ASKS the member their
fitness goal and workout frequency through conversation, instead of
expecting them pre-set in session state. That means the earlier limitation
("adk web doesn't work well here") no longer applies — you can now just
run:

    adk web

from the project root, pick member_goal_setter from the dropdown, and
actually chat with it: "I want to lose weight, I currently work out maybe
twice a week" — it should ask any clarifying questions it needs, map your
answer to the exact questionnaire options, call recommend_weekly_goal, and
tell you the result.

This script covers the other half — the part that doesn't need a live
model at all: the deterministic recommend_weekly_goal() function itself.
Run it directly:

    python3 tests/test_member_goal_setter.py

Edit GOAL and FREQUENCY below to try other combinations instantly, no API
key needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from member_goal_setter.agent import recommend_weekly_goal

# ---- Edit these to try different combinations ----
GOAL = "Weight loss"
FREQUENCY = "3-4 times / week"
# ---------------------------------------------------


def test_pure_function():
    print("=== recommend_weekly_goal() — instant, no LLM, no API key ===")
    result = recommend_weekly_goal(GOAL, FREQUENCY)
    print(f"Input:  goal={GOAL!r}, frequency={FREQUENCY!r}")
    print(f"Output: {result}")
    print()
    print("Try other combinations by editing GOAL/FREQUENCY above, e.g.:")
    for goal, freq in [
        ("Build strength", "5-6 times / week"),
        ("Improve flexibility", "Never"),
        ("Boost energy", "Daily"),
    ]:
        print(f"  {goal!r:25} + {freq!r:20} -> {recommend_weekly_goal(goal, freq)}")


if __name__ == "__main__":
    test_pure_function()
    print(
        "\nTo test the conversational half (asking the questions, mapping "
        "free-text answers), run `adk web` from the project root and chat "
        "with member_goal_setter directly — see this file's module "
        "docstring for details."
    )
