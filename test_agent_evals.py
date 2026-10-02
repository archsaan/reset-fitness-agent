"""
pytest entrypoint for ADK's native eval framework (google.adk.evaluation).

Run with: pytest eval/ -v
(from the project root, with the venv/deps this project already uses, plus
`pip install "google-adk[eval]" pytest pytest-asyncio` — see eval/README.md
for why the [eval] extra specifically is required, not just [extensions]).

Each test below points AgentEvaluator at one agent's `.test.json` files.
AgentEvaluator.evaluate():
  - auto-discovers every `*.test.json` file in the given directory
  - auto-loads that directory's `test_config.json` for the pass/fail
    thresholds (tool_trajectory_avg_score, response_match_score — see
    eval/tribe_app/test_config.json for the values and why they're not
    the ADK defaults)
  - actually RUNS the real agent (real Gemini call per turn, real
    ProfitConnect schedule/validate calls for tool-using cases) and
    compares against each case's expected tool_uses / final_response
  - raises an AssertionError (which pytest reports as a normal test
    failure) if any case misses its threshold

This means running this file costs real Gemini quota and hits the real
ProfitConnect schedule API on every run — it is NOT free, and NOT
instant. It's meant for "did my prompt/model change break anything",
run manually or in CI before a deploy, not on every save.

book_class is never actually invoked for real here even though the
booking-gate eval cases exercise it: common/booking_gate.py's
before_tool_callback intercepts every book_class call before it can run,
exactly like it does for a real member — so this suite is safe to run
against your real ProfitConnect credentials with zero risk of creating a
real booking.
"""

import os

import pytest
from google.adk.evaluation.agent_evaluator import AgentEvaluator

_EVAL_DIR = os.path.dirname(__file__)


@pytest.mark.asyncio
async def test_tribe_app_evals():
    await AgentEvaluator.evaluate(
        agent_module="tribe_app.agent",
        eval_dataset_file_path_or_dir=os.path.join(_EVAL_DIR, "tribe_app"),
    )


@pytest.mark.asyncio
async def test_pfc_evals():
    await AgentEvaluator.evaluate(
        agent_module="pfc.agent",
        eval_dataset_file_path_or_dir=os.path.join(_EVAL_DIR, "pfc"),
    )
