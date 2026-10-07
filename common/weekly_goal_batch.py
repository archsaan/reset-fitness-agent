"""
Weekly goal batch runner (Member Goal Setter only) — generates this
week's per-category session recommendation for N synthetic test members
and measures the REAL token cost of doing so, so the two ways this
agent could work can be compared on cost and on how often free-text
model output goes out of bounds:

  - "hybrid"   — recommend_weekly_sessions_v2() (a plain function, see
                 member_goal_setter/weekly_session_planner_v2.py) decides
                 the numbers deterministically, for $0 and zero LLM
                 calls. The rationale sentence uses that file's
                 rationale_v2() non-LLM fallback — also $0. This is the
                 cheap, auditable path real traffic should use.
  - "llm-only" — a single prompt asks the model to decide BOTH the
                 per-category numbers and the rationale itself, with no
                 code-side correction. Exists purely to measure what
                 letting the model freelance would cost and how often
                 its own numbers would violate a real category cap
                 (see _qa_check) — a concrete number to point to when
                 deciding whether "hybrid" is worth keeping, instead of
                 just asserting it is.

`dry_run=True` skips the real model call entirely (for "llm-only") and
estimates tokens from prompt length instead — lets the dashboard panel
be exercised/tested without spending anything.

Each run (mode, model, dry_run, per-member rows, aggregate summary) is
persisted to Postgres so the dashboard's "SCHEDULED & SENT"-style run
history list and per-run drill-down have something to read. Table names
are new (weekly_goal_runs / weekly_goal_run_rows) — this is the first
thing in this project that needs its own run history, nothing to
collide with.
"""

from __future__ import annotations

import json
import logging
import re

import psycopg
from psycopg.rows import dict_row

from common.config import DATABASE_URL
from common.kb import log_usage
from member_goal_setter.weekly_session_planner_v2 import (
    CATEGORY_CAPS,
    _GOAL_TO_V2_CATEGORIES,
    rationale_v2,
    recommend_weekly_sessions_v2,
)

logger = logging.getLogger("reset_fitness_adk.weekly_goal_batch")

AGENT_SLUG = "member-goal-setter"

# Capped well under anything that could run long/cost real money by
# accident from a dashboard click — raise this deliberately, not by
# fat-fingering a big number into the Members field.
MAX_MEMBERS_PER_RUN = 25

_GOALS = list(_GOAL_TO_V2_CATEGORIES.keys())
_CATEGORIES = list(CATEGORY_CAPS.keys())

# Same PAID-tier $/million-token figures admin.py's usage panel uses for
# other models — gemini-3.5-flash-lite isn't in that table yet (it's $0
# on the current free AI Studio key), so this uses gemini-2.5-flash-lite's
# paid rate as the closest stand-in, same reasoning admin.py already
# documents for grok-*: "what this would cost on a paid key", not what
# today's actual bill is. Thinking tokens are billed as output tokens by
# Gemini, so they're priced at the output rate too.
_RATE_PER_MILLION = {"input": 0.10, "output": 0.40}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_schema_ready = False


def _ensure_ready() -> None:
    global _schema_ready
    if not _schema_ready:
        init_weekly_goal_batch_store()
        _schema_ready = True


def init_weekly_goal_batch_store() -> None:
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weekly_goal_runs (
                id SERIAL PRIMARY KEY,
                mode TEXT NOT NULL,
                model TEXT NOT NULL,
                dry_run BOOLEAN NOT NULL DEFAULT FALSE,
                member_count INTEGER NOT NULL,
                summary JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weekly_goal_run_rows (
                id SERIAL PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES weekly_goal_runs(id) ON DELETE CASCADE,
                member_id TEXT NOT NULL,
                goal TEXT NOT NULL,
                category_targets JSONB NOT NULL,
                rationale TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                thinking_tokens INTEGER NOT NULL,
                cost_usd NUMERIC NOT NULL,
                qa JSONB NOT NULL,
                prompt TEXT,
                reply TEXT
            )
        """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Synthetic test members
# ---------------------------------------------------------------------------

def synthetic_members(count: int) -> list[dict]:
    """Deterministic (not random) so two runs with the same count are
    directly comparable — same goal/last-week mix every time, only the
    model's own output can differ between runs."""
    members = []
    for i in range(count):
        goal = _GOALS[i % len(_GOALS)]
        # Spread a plausible, varied last week across the real
        # categories without ever exceeding that category's real cap -
        # i % 3 gives 0/1/2 which naturally clamps against every cap
        # below (the lowest real cap is 1).
        last_week = {cat: min((i + j) % 3, cap) for j, (cat, cap) in enumerate(CATEGORY_CAPS.items())}
        members.append({
            "id": f"synthetic-{i + 1:03d}",
            "goal": goal,
            "last_week_sessions": last_week,
        })
    return members


# ---------------------------------------------------------------------------
# QA — did the model's own numbers respect the real constraints?
# ---------------------------------------------------------------------------

def _qa_check(goal: str, category_targets: dict, rationale: str) -> dict:
    over_cap = any(category_targets.get(cat, 0) > cap for cat, cap in CATEGORY_CAPS.items())
    goal_categories = set(_GOAL_TO_V2_CATEGORIES.get(goal, []))
    missing_category = bool(goal_categories) and not (goal_categories & set(category_targets.keys()))
    word_count = len(rationale.split())
    rationale_in_range = 30 <= word_count <= 40
    return {
        "over_cap": over_cap,
        "missing_category": missing_category,
        "rationale_word_count": word_count,
        "rationale_in_range": rationale_in_range,
    }


# ---------------------------------------------------------------------------
# Per-member recommendation — hybrid (free) vs llm-only (real/estimated call)
# ---------------------------------------------------------------------------

def _run_hybrid_member(member: dict) -> dict:
    result = recommend_weekly_sessions_v2(member["goal"], member["last_week_sessions"])
    rationale = rationale_v2(member["goal"], member["last_week_sessions"], result)
    return {
        "category_targets": result["category_targets"],
        "rationale": rationale,
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "prompt": None,
        "reply": None,
    }


def _build_llm_prompt(member: dict) -> str:
    return (
        "You are recommending this week's workout session counts for a fitness studio member.\n\n"
        f"Member's stated goal: {member['goal']}\n"
        f"Last week's sessions per category: {json.dumps(member['last_week_sessions'])}\n"
        f"Real weekly slot caps per category (never exceed these): {json.dumps(CATEGORY_CAPS)}\n\n"
        "Decide this week's target session count for each category yourself, and write a short "
        "30-40 word rationale explaining the plan to the member.\n\n"
        "Respond with ONLY this JSON shape, no other text: "
        '{"category_targets": {"<category>": <int>, ...}, "rationale": "<30-40 word sentence>"}'
    )


def _parse_llm_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response")
    data = json.loads(match.group(0))
    if "category_targets" not in data or "rationale" not in data:
        raise ValueError("Model response missing category_targets or rationale")
    return data


def _run_llm_member(member: dict, model: str, dry_run: bool) -> dict:
    prompt = _build_llm_prompt(member)

    if dry_run:
        # Stand-in, no real call: reuse the deterministic result as a
        # plausible shape, with rough token estimates (chars / 4 is the
        # usual ballpark for English text) purely so the UI has numbers
        # to render while testing.
        result = recommend_weekly_sessions_v2(member["goal"], member["last_week_sessions"])
        rationale = rationale_v2(member["goal"], member["last_week_sessions"], result)
        reply_text = json.dumps({"category_targets": result["category_targets"], "rationale": rationale})
        return {
            "category_targets": result["category_targets"],
            "rationale": rationale,
            "input_tokens": max(1, len(prompt) // 4),
            "output_tokens": max(1, len(reply_text) // 4),
            "thinking_tokens": 0,
            "prompt": prompt,
            "reply": reply_text,
        }

    if not (model.startswith("gemini-") or model.startswith("gemini/")):
        raise ValueError(
            f"Weekly goal batch's llm-only mode only supports Gemini models directly, got {model!r} — "
            "use dry_run to test the UI, or add a non-Gemini code path first."
        )
    bare_model = model[len("gemini/"):] if model.startswith("gemini/") else model

    from google import genai

    client = genai.Client()
    response = client.models.generate_content(model=bare_model, contents=prompt)
    reply_text = response.text or ""
    data = _parse_llm_json(reply_text)

    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) or 0
    output_tokens = getattr(usage, "candidates_token_count", None) or 0
    thinking_tokens = getattr(usage, "thoughts_token_count", None) or 0

    return {
        "category_targets": data["category_targets"],
        "rationale": data["rationale"],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "prompt": prompt,
        "reply": reply_text,
    }


def _cost_usd(input_tokens: int, output_tokens: int, thinking_tokens: int) -> float:
    billed_output = output_tokens + thinking_tokens
    return (input_tokens / 1_000_000 * _RATE_PER_MILLION["input"]) + (
        billed_output / 1_000_000 * _RATE_PER_MILLION["output"]
    )


def run_batch(members: list[dict], mode: str, model: str, dry_run: bool = False, log_usage_rows: bool = True) -> list[dict]:
    rows = []
    for member in members:
        if mode == "hybrid":
            outcome = _run_hybrid_member(member)
        else:
            outcome = _run_llm_member(member, model, dry_run)

        cost = _cost_usd(outcome["input_tokens"], outcome["output_tokens"], outcome["thinking_tokens"])
        qa = _qa_check(member["goal"], outcome["category_targets"], outcome["rationale"])

        if log_usage_rows and not dry_run and outcome["input_tokens"] + outcome["output_tokens"] > 0:
            try:
                log_usage(AGENT_SLUG, f"weekly-batch:{member['id']}", model, outcome["input_tokens"], outcome["output_tokens"])
            except Exception:
                logger.exception("Failed to log usage for weekly goal batch member %s", member["id"])

        rows.append({
            "member": member["id"],
            "goal": member["goal"],
            "category_targets": outcome["category_targets"],
            "rationale": outcome["rationale"],
            "input_tokens": outcome["input_tokens"],
            "output_tokens": outcome["output_tokens"],
            "thinking_tokens": outcome["thinking_tokens"],
            "cost_usd": round(cost, 6),
            "qa": qa,
            "prompt": outcome["prompt"],
            "reply": outcome["reply"],
        })
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def summarize(rows: list[dict]) -> dict:
    member_count = len(rows)
    total_input = sum(r["input_tokens"] for r in rows)
    total_output = sum(r["output_tokens"] for r in rows)
    total_thinking = sum(r["thinking_tokens"] for r in rows)
    total_cost = sum(r["cost_usd"] for r in rows)
    cost_per_member = total_cost / member_count if member_count else 0.0

    return {
        "members": member_count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_thinking_tokens": total_thinking,
        "avg_input_tokens": round(total_input / member_count, 1) if member_count else 0,
        "avg_output_tokens": round(total_output / member_count, 1) if member_count else 0,
        "total_cost_usd": round(total_cost, 4),
        "cost_per_member_usd": round(cost_per_member, 6),
        # Projected cost of running this same batch weekly at 100 members -
        # the scale figure the dashboard's summary cards lead with, so a
        # 10-member test run still answers "is this affordable at scale."
        "cost_per_100_members_usd": round(cost_per_member * 100, 4),
        "rationale_in_range_count": sum(1 for r in rows if r["qa"]["rationale_in_range"]),
        "over_cap_count": sum(1 for r in rows if r["qa"]["over_cap"]),
        "missing_category_count": sum(1 for r in rows if r["qa"]["missing_category"]),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_run(mode: str, model: str, dry_run: bool, summary: dict, rows: list[dict]) -> int:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO weekly_goal_runs (mode, model, dry_run, member_count, summary)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (mode, model, dry_run, summary["members"], json.dumps(summary)),
        )
        run_id = cur.fetchone()[0]
        for row in rows:
            cur.execute(
                """
                INSERT INTO weekly_goal_run_rows
                    (run_id, member_id, goal, category_targets, rationale, input_tokens,
                     output_tokens, thinking_tokens, cost_usd, qa, prompt, reply)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id, row["member"], row["goal"], json.dumps(row["category_targets"]),
                    row["rationale"], row["input_tokens"], row["output_tokens"], row["thinking_tokens"],
                    row["cost_usd"], json.dumps(row["qa"]), row["prompt"], row["reply"],
                ),
            )
    conn.commit()
    conn.close()
    return run_id


def list_runs() -> list[dict]:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT id, mode, model, dry_run, member_count, summary, created_at
            FROM weekly_goal_runs ORDER BY id DESC
        """)
        rows = cur.fetchall()
    conn.close()
    return rows


def get_run(run_id: int) -> dict | None:
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT id, mode, model, dry_run, member_count, summary, created_at
            FROM weekly_goal_runs WHERE id = %s
        """, (run_id,))
        run = cur.fetchone()
        if run is None:
            conn.close()
            return None
        cur.execute("""
            SELECT member_id AS member, goal, category_targets, rationale, input_tokens,
                   output_tokens, thinking_tokens, cost_usd, qa, prompt, reply
            FROM weekly_goal_run_rows WHERE run_id = %s ORDER BY id
        """, (run_id,))
        run["rows"] = cur.fetchall()
    conn.close()
    return run
