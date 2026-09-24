"""
DB-backed, live-editable agent configuration — the piece that makes the
dashboard's Config panel "Save changes" button actually do something,
instead of erroring (see the old note in routers/admin.py's module
docstring).

Deliberate design choice, worth calling out since it's a real
agentic-app best practice: only the PERSONA half of each agent's
instruction is editable here (tone, personality, how it talks about
itself). The safety-critical RULES text (never guess a
calendar_schedule_id, only book after check_availability confirms
space, book_class never books immediately) stays hardcoded in
common/config.py's RULES constant and is appended in code, not stored
here — an admin fat-fingering the Config panel, or a future non-technical
user, should not be able to accidentally delete the one paragraph that
keeps the booking gate honest. Editable = tone. Fixed = safety.

Same lazy-init pattern as common/kb.py's _ensure_ready(), for the same
reason: `adk web` never runs main.py's explicit init call, so every
function here creates its own table on first use if it's missing.
"""

import psycopg

from common.config import DATABASE_URL

_schema_ready = False


def _ensure_ready() -> None:
    global _schema_ready
    if not _schema_ready:
        init_agent_config_store()
        _schema_ready = True


def init_agent_config_store() -> None:
    """Creates agent_configs if missing. Safe to call on every startup."""
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_configs (
                agent_slug TEXT PRIMARY KEY,
                system_prompt TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    conn.commit()
    conn.close()


def get_system_prompt(agent_slug: str, default: str) -> str:
    """Returns the admin-edited persona prompt for this agent, or
    `default` (the hardcoded one from common/config.py) if nobody has
    ever saved a custom one. Called fresh every turn by each agent's
    instruction provider — an admin's edit takes effect on the very
    next message, no redeploy, same guarantee the KB already has."""
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("SELECT system_prompt FROM agent_configs WHERE agent_slug = %s", (agent_slug,))
        row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_system_prompt(agent_slug: str, system_prompt: str) -> None:
    """Upserts the admin-edited persona prompt. An empty/whitespace-only
    string is rejected by the caller (routers/admin.py), not here —
    kept here as a pure data-layer function."""
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO agent_configs (agent_slug, system_prompt, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (agent_slug) DO UPDATE
                SET system_prompt = EXCLUDED.system_prompt, updated_at = NOW()
        """, (agent_slug, system_prompt))
    conn.commit()
    conn.close()


def reset_system_prompt(agent_slug: str) -> None:
    """Deletes any saved override, reverting the agent to its hardcoded
    default from common/config.py. Used by the dashboard's (optional)
    "Reset to default" action."""
    _ensure_ready()
    conn = psycopg.connect(DATABASE_URL)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_configs WHERE agent_slug = %s", (agent_slug,))
    conn.commit()
    conn.close()
