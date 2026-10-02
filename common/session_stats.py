"""
Conversation-length visibility — NOT a fix for the context-window growth
problem (every ADK agent resends a session's FULL event history to the
model on every turn — see contents.py's _ContentLlmRequestProcessor,
agent.include_contents == "default"), just a way to see whether that's
actually biting in real conversations before building compaction/trimming
for a problem usage data might show barely matters at this project's
current scale.

Reads directly from the tables ADK's own DatabaseSessionService already
created in Neon (see common/config.py's ADK_SESSION_DB_URL) — `sessions`
and `events` — rather than duplicating storage. No new table, no writes,
this module is read-only.

One thing this file has to get right that's easy to get wrong: the
agent_slug used everywhere else in this project ("tribe-app", "pfc" — see
routers/admin.py's _AGENTS) is NOT the same string ADK uses as `app_name`
in its own sessions/events tables. ADK's AgentLoader names each app after
its FOLDER (tribe_app/, pfc/ — see google.adk.cli.utils.agent_loader,
confirmed by reading its source), so real chat traffic through
get_fast_api_app() is stored under app_name="tribe_app" (underscore),
while this project's admin/KB code uses the slug "tribe-app" (hyphen).
_APP_NAME_BY_SLUG below is the one place that mapping lives — update it
if an agent folder is ever renamed.
"""

import psycopg
from psycopg.rows import dict_row

from common.config import DATABASE_URL

_APP_NAME_BY_SLUG = {
    "tribe-app": "tribe_app",
    "pfc": "pfc",
    "ai-coach": "ai_coach",
    "member-goal-setter": "member_goal_setter",
}


def get_session_length_stats(agent_slug: str, limit: int = 20) -> list[dict]:
    """Returns the longest real conversations for one agent, most events
    first. `approx_chars`/`approx_tokens` are a cheap proxy, not a billing-
    accurate count: `event_data` is the raw JSON ADK stores per turn
    (text, tool calls, tool results), and ~4 characters per token is the
    usual rough rule of thumb for English text — good enough to spot
    which sessions are ballooning, not meant to match Gemini's own
    tokenizer exactly. For an exact number, usage_logs (common/kb.py's
    log_usage) already has real input_tokens per call, but only summed
    across an agent/model, not broken out per session — this fills that
    specific gap.

    Only ever reads (SELECT), and only touches ADK's own tables — never
    the KB tables, never usage_logs. A missing/unknown agent_slug returns
    an empty list rather than raising, since this is a diagnostics panel,
    not a critical path."""
    app_name = _APP_NAME_BY_SLUG.get(agent_slug)
    if app_name is None:
        return []

    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
                events.session_id,
                events.user_id,
                COUNT(*) AS event_count,
                SUM(LENGTH(events.event_data::text)) AS approx_chars,
                MIN(events.timestamp) AS started_at,
                MAX(events.timestamp) AS last_activity_at
            FROM events
            WHERE events.app_name = %s
            GROUP BY events.session_id, events.user_id
            ORDER BY event_count DESC
            LIMIT %s
            """,
            (app_name, limit),
        )
        rows = cur.fetchall()
    conn.close()

    for row in rows:
        row["approx_tokens"] = round((row["approx_chars"] or 0) / 4)
    return rows
