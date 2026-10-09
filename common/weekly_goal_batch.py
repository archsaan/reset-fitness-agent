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
                source TEXT NOT NULL DEFAULT 'synthetic',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # One-time migration for a table created before `source` existed -
        # ADD COLUMN IF NOT EXISTS is a no-op on a table that already has
        # it, safe to run on every startup like the rest of this file's
        # CREATE TABLE IF NOT EXISTS statements.
        cur.execute("ALTER TABLE weekly_goal_runs ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'synthetic'")
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
# Test members sourced from docs/member_wellness_preferences.xlsx
# ---------------------------------------------------------------------------

# This workbook maps Test ID -> real name/DB member_id (see its "ID key"
# tab) and says so explicitly in its own notes: "This workbook contains
# real member names. Do not paste names into model prompts. Use the Test
# ID instead." workbook_members() below only ever reads the Test ID plus
# numeric/category data - never the name or DB member_id columns - so
# that guarantee holds no matter what the caller does with the result
# (store it, put it in an LLM prompt, etc).
#
# Its "Last week attendance" tab itself says its session counts are
# made-up sample data, not real check-ins, so this isn't real attendance
# history either - what's real here is the GOAL each test member is
# assigned (from the "Member goals" tab, sourced from an actual
# member_fitness_goals screenshot for most rows - a few are marked
# SAMPLE in that tab for members the screenshot didn't cover).
WORKBOOK_PATH = "docs/member_wellness_preferences.xlsx"
_PROMPT_INPUT_SHEET = "Prompt input"

# Profile fields pulled from the "Prompt input" sheet, in the exact order
# the "Prompt template (optimized)" tab's own formula emits them - see
# _build_optimized_prompt() below, which is a line-for-line port of that
# formula. Most of these are None/not-yet-available for every current
# test member (see that sheet's own note: "Yellow cells are not
# available in the data supplied yet") - a blank one is simply left out
# of the prompt, costing no tokens, same as the spreadsheet does.
_PROFILE_FIELDS = [
    "Gender", "Age", "Fitness goal", "Current activities", "Exercise frequency",
    "Active", "Conditions", "Medications", "Medical treatment", "Smoker", "Alcohol consumption",
]


def workbook_members(limit: int | None = None) -> list[dict]:
    """[{"id", "goal", "last_week_sessions", "profile"}, ...], sourced
    from the workbook's "Prompt input" tab - anonymized to Test ID only
    (never the name/DB member_id columns on other tabs). "profile" holds
    the richer per-member fields (age, exercise frequency, smoker, ...)
    that _build_optimized_prompt() needs for llm-only mode; "goal" and
    "last_week_sessions" alone are all hybrid mode / the QA checks use.
    Raises FileNotFoundError if the workbook isn't present (e.g. a
    deploy that doesn't bundle docs/)."""
    from openpyxl import load_workbook

    wb = load_workbook(WORKBOOK_PATH, read_only=True, data_only=True)
    sheet = wb[_PROMPT_INPUT_SHEET]
    rows = sheet.iter_rows(values_only=True)
    header = [str(c).strip() if c is not None else "" for c in next(rows)]
    col = {name: i for i, name in enumerate(header)}

    members = []
    for row in rows:
        test_id = row[col["Test ID"]]
        # The sheet's footer (blank spacer, notes paragraphs) has no
        # Test ID / isn't one of our m<N> rows - stop at the first row
        # that isn't real data rather than trying to filter notes text.
        if not test_id or not isinstance(test_id, str) or not test_id.startswith("m"):
            continue
        goal = row[col["Fitness goal"]] or ""
        last_week = {cat: int(row[col[cat]] or 0) for cat in CATEGORY_CAPS}
        profile = {field: row[col[field]] for field in _PROFILE_FIELDS}
        members.append({"id": test_id, "goal": goal, "last_week_sessions": last_week, "profile": profile})
        if limit is not None and len(members) >= limit:
            break
    wb.close()
    return members


_optimized_template_cache: tuple[str, str] | None = None


def _load_optimized_prompt_template() -> tuple[str, str]:
    """(header, footer) fixed text, read live from the "Prompt template
    (optimized)" tab (cells A2 and A5) rather than hardcoded in code, so
    editing the wording in the workbook takes effect on the next run
    with no code change - matches this same file's goal/category rules
    already living in docs/ rather than being duplicated in Python.
    Cached for the process lifetime since this text never changes
    mid-run."""
    global _optimized_template_cache
    if _optimized_template_cache is None:
        from openpyxl import load_workbook

        wb = load_workbook(WORKBOOK_PATH, read_only=True, data_only=True)
        sheet = wb["Prompt template (optimized)"]
        header = sheet["A2"].value
        footer = sheet["A5"].value
        wb.close()
        _optimized_template_cache = (header, footer)
    return _optimized_template_cache


def _build_optimized_prompt(member: dict) -> str:
    """Line-for-line port of the workbook's "Optimized prompt (formula)"
    column on the "Prompt input" tab - same field order, same "Not
    provided"/blank fields omitted entirely (so they cost no tokens),
    same JSON-only output spec with NO rationale. See that sheet for the
    original Excel formula this mirrors."""
    header, footer = _load_optimized_prompt_template()
    profile = member.get("profile") or {}

    def present(field: str) -> bool:
        value = profile.get(field)
        return bool(value) and not str(value).startswith("Not")

    lines = ["Member:"]
    gender, age = profile.get("Gender"), profile.get("Age")
    if gender or age:
        lines.append("Profile: " + ", ".join(str(v) for v in (gender, age) if v))
    if present("Fitness goal"):
        lines.append(f"Goal: {profile['Fitness goal']}")
    elif member.get("goal"):
        lines.append(f"Goal: {member['goal']}")
    if present("Current activities"):
        lines.append(f"Activities: {profile['Current activities']}")
    if present("Exercise frequency"):
        lines.append(f"Exercise frequency: {profile['Exercise frequency']}")
    if present("Active"):
        lines.append(f"Training history: {profile['Active']}")
    if present("Conditions"):
        lines.append(f"Conditions: {profile['Conditions']}")
    if present("Medications"):
        lines.append(f"Medications: {profile['Medications']}")
    if present("Medical treatment"):
        lines.append(f"Medical treatment: {profile['Medical treatment']}")
    if present("Smoker"):
        lines.append(f"Smoker: {profile['Smoker']}")
    if present("Alcohol consumption"):
        lines.append(f"Alcohol: {profile['Alcohol consumption']}")

    last_week = member["last_week_sessions"]
    lines.append("Last week: " + ", ".join(f"{cat}={last_week.get(cat, 0)}" for cat in CATEGORY_CAPS))

    return f"{header}\n\n" + "\n".join(lines) + f"\n\n{footer}"


# ---------------------------------------------------------------------------
# QA — did the model's own numbers respect the real constraints?
# ---------------------------------------------------------------------------

def _qa_check(goal: str, category_targets: dict, rationale: str) -> dict:
    over_cap = any(category_targets.get(cat, 0) > cap for cat, cap in CATEGORY_CAPS.items())
    goal_categories = set(_GOAL_TO_V2_CATEGORIES.get(goal, []))
    missing_category = bool(goal_categories) and not (goal_categories & set(category_targets.keys()))
    # The workbook's prompt format (llm-only mode) asks for numbers only,
    # no rationale - rationale is "" there, not a failed/too-short one.
    # An empty rationale means "not applicable," not "out of range," so
    # it's excluded from rationale_in_range entirely rather than counted
    # as a miss (which would make every llm-only run show a permanent,
    # meaningless 0/N on that stat).
    word_count = len(rationale.split()) if rationale else 0
    rationale_in_range = (30 <= word_count <= 40) if rationale else None
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


def _parse_llm_json(text: str) -> dict:
    """Workbook's prompt spec asks for ONLY
    {"Cardio":#,"Strength":#,"Shape":#,"Strength & Conditioning":#,
    "Wellness":#,"Red Light Therapy":#} - no wrapper object, no
    rationale. Keys are filtered to CATEGORY_CAPS so a stray/hallucinated
    key can't sneak into category_targets."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response")
    data = json.loads(match.group(0))
    if not any(cat in data for cat in CATEGORY_CAPS):
        raise ValueError("Model response had none of the expected category keys")
    return {cat: int(data[cat]) for cat in CATEGORY_CAPS if cat in data and data[cat] is not None}


def _run_llm_member(member: dict, model: str, dry_run: bool) -> dict:
    prompt = _build_optimized_prompt(member)

    if dry_run:
        # Stand-in, no real call: reuse the deterministic result as a
        # plausible shape, with rough token estimates (chars / 4 is the
        # usual ballpark for English text) purely so the UI has numbers
        # to render while testing. No rationale here either, matching
        # the real path below - this mode's prompt never asks for one.
        result = recommend_weekly_sessions_v2(member["goal"], member["last_week_sessions"])
        reply_text = json.dumps(result["category_targets"])
        return {
            "category_targets": result["category_targets"],
            "rationale": "",
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

    # No system_instruction here on purpose: the workbook's prompt is
    # self-contained (it has its own "You are a fitness consultant..."
    # instructions baked in), and this mode exists specifically to
    # measure that exact prompt's real token cost against the
    # spreadsheet's estimate - layering the agent's own persona on top
    # would inflate input tokens beyond what's being benchmarked.
    client = genai.Client()
    response = client.models.generate_content(model=bare_model, contents=prompt)
    reply_text = response.text or ""
    category_targets = _parse_llm_json(reply_text)

    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", None) or 0
    output_tokens = getattr(usage, "candidates_token_count", None) or 0
    thinking_tokens = getattr(usage, "thoughts_token_count", None) or 0

    return {
        "category_targets": category_targets,
        "rationale": "",
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

# Same cadence as the real feature: one interaction per member per week
# (every Sunday) - matches the "~4 interactions/week per 100 members"
# framing used in the cost-comparison email to management, so these
# numbers are directly comparable to that email's weekly/monthly figures.
WEEKS_PER_MONTH = 4

# The same 100/200/300-member scale points used in that email, so the
# dashboard's projection table lines up with it exactly instead of
# requiring someone to redo the arithmetic by hand.
PROJECTION_MEMBER_SCALES = [100, 200, 300]


def summarize(rows: list[dict]) -> dict:
    member_count = len(rows)
    total_input = sum(r["input_tokens"] for r in rows)
    total_output = sum(r["output_tokens"] for r in rows)
    total_thinking = sum(r["thinking_tokens"] for r in rows)
    total_cost = sum(r["cost_usd"] for r in rows)
    cost_per_member = total_cost / member_count if member_count else 0.0

    projections = [
        {
            "members": scale,
            "weekly_usd": round(cost_per_member * scale, 4),
            "monthly_usd": round(cost_per_member * scale * WEEKS_PER_MONTH, 4),
        }
        for scale in PROJECTION_MEMBER_SCALES
    ]

    return {
        "members": member_count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_thinking_tokens": total_thinking,
        "avg_input_tokens": round(total_input / member_count, 1) if member_count else 0,
        "avg_output_tokens": round(total_output / member_count, 1) if member_count else 0,
        "total_cost_usd": round(total_cost, 4),
        # This run's own cost projected to a monthly (x4 weekly) cadence -
        # meaningful even at a small test member_count, since it's a
        # straight multiple of what was actually measured, not a
        # different scale.
        "total_cost_usd_monthly": round(total_cost * WEEKS_PER_MONTH, 4),
        "cost_per_member_usd": round(cost_per_member, 6),
        # Projected cost of running this same batch weekly/monthly at 100
        # members - the scale figure the dashboard's summary cards lead
        # with, so a 10-member test run still answers "is this affordable
        # at scale."
        "cost_per_100_members_usd": round(cost_per_member * 100, 4),
        "cost_per_100_members_usd_monthly": round(cost_per_member * 100 * WEEKS_PER_MONTH, 4),
        # Full 100/200/300-member weekly+monthly table, same shape as the
        # cost-comparison email sent to management - lets this run's
        # numbers (for whichever model is configured) be dropped straight
        # into that same comparison.
        "projections": projections,
        # rationale_in_range is None (not False) on a row with no
        # rationale at all (the workbook's llm-only prompt never asks
        # for one) - rationale_checked_count is the real denominator for
        # rationale_in_range_count, so the dashboard can show "X/Y
        # checked" instead of implying every member failed a check that
        # was never applicable to them.
        "rationale_checked_count": sum(1 for r in rows if r["qa"]["rationale_in_range"] is not None),
        "rationale_in_range_count": sum(1 for r in rows if r["qa"]["rationale_in_range"]),
        "over_cap_count": sum(1 for r in rows if r["qa"]["over_cap"]),
        "missing_category_count": sum(1 for r in rows if r["qa"]["missing_category"]),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_run(mode: str, model: str, dry_run: bool, summary: dict, rows: list[dict], source: str = "synthetic") -> int:
    """Persists this run, then deletes every OTHER run — only the most
    recent run is ever kept (its rows cascade-delete with it via
    weekly_goal_run_rows' ON DELETE CASCADE). This is a dashboard
    test/benchmark tool, not an audit log, so there's no need to keep
    piling up old batches; if that ever changes, drop the DELETE below
    and this goes back to being a full run history."""
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO weekly_goal_runs (mode, model, dry_run, member_count, summary, source)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (mode, model, dry_run, summary["members"], json.dumps(summary), source),
        )
        run_id = cur.fetchone()[0]
        cur.execute("DELETE FROM weekly_goal_runs WHERE id != %s", (run_id,))
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
            SELECT id, mode, model, dry_run, source, member_count, summary, created_at
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
            SELECT id, mode, model, dry_run, source, member_count, summary, created_at
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
