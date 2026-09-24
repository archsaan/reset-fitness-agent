"""
Admin routes for the reset-fitness-dashboard Next.js app.

Scope, deliberately narrower than the main (LangGraph) project's admin
routes: agents here are defined in code (tribe_app/agent.py, pfc/agent.py)
and deployed via Cloud Run, not created/edited/deleted through the API —
that's a conscious choice (asked for directly), not a missing feature.
So this router implements:
  - POST   /admin/login                              (mock auth, same token as main project)
  - GET    /admin/agents                              (read-only list of the two code-defined agents)
  - GET    /admin/agents/{agent_slug}                  (read-only detail, incl. system_prompt/model)
  - GET    /admin/agents/{agent_slug}/knowledge-base           (unchanged from before)
  - POST   /admin/agents/{agent_slug}/knowledge-base           (unchanged from before)
  - PUT    /admin/agents/{agent_slug}/knowledge-base/{doc_id}/toggle
  - DELETE /admin/agents/{agent_slug}/knowledge-base/{doc_id}
  - GET    /admin/usage                                (global usage summary)
  - GET    /admin/agents/{agent_slug}/usage                     (per-agent usage summary)
  - POST   /admin/agents/{agent_slug}/test-chat                 (throwaway ADK session, real reply)

NOT implemented on purpose: POST/DELETE /admin/agents (create/delete —
manage by adding/removing agent folders and redeploying) and changing
`model` via PUT (still env/code-controlled — see common/config.py's
resolve_model, switching models has real cost/quota implications so
it's deliberately not a one-click dashboard action).

PUT /admin/agents/{agent_slug} DOES work now, for system_prompt only:
it's stored in Postgres (common/agent_config.py, agent_configs table)
and read fresh on every turn by each agent's instruction provider
(tribe_app/agent.py, pfc/agent.py's build_instruction) — an edit here
takes effect on the very next message, no redeploy. Only the PERSONA
half of the instruction is editable this way; the safety-critical RULES
text (never guess a calendar_schedule_id, only book after
check_availability, etc.) stays hardcoded in common/config.py and is
always appended after whatever persona is active, so this endpoint can
never be used to weaken the booking gate.
"""

import logging
import uuid

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from google.genai import types

from common.agent_config import get_system_prompt, reset_system_prompt, set_system_prompt
from common.config import MOCK_ADMIN_TOKEN, PFC_MODEL, PFC_SYSTEM_PROMPT, TRIBE_APP_MODEL, TRIBE_APP_SYSTEM_PROMPT
from common.kb import (
    delete_kb_doc,
    get_usage_summary,
    index_kb_doc,
    insert_kb_doc,
    list_kb_docs,
    toggle_kb_doc,
)

logger = logging.getLogger("reset_fitness_adk.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(authorization: str | None) -> None:
    if authorization != f"Bearer {MOCK_ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="Admin authentication required")


@router.post("/login")
def admin_login(payload: dict):
    """Mock login — identical shape to the main project's. Accepts any
    non-empty username/password for now."""
    username = payload.get("username", "")
    password = payload.get("password", "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    # TODO: replace with real credential check against your auth system
    return {"token": MOCK_ADMIN_TOKEN}


# ---------------------------------------------------------
# Agents — read-only, sourced from code (tribe_app/agent.py, pfc/agent.py),
# not a database. "id" is the slug (a string) rather than an integer, but
# the dashboard only ever echoes it back into URLs, so that's transparent
# to it.
# ---------------------------------------------------------

# Static agent registry — kept here (not imported from tribe_app/pfc
# directly) to avoid a circular import, since those modules import from
# common/, not the other way around. Update this list if you rename an
# agent slug or add a new agent folder.
_AGENTS = [
    {
        "id": "tribe-app",
        "slug": "tribe-app",
        "name": "Tribe App Agent",
        "description": "Helps Reset Fitness app members book classes and check membership status.",
        "active_model": TRIBE_APP_MODEL,
    },
    {
        "id": "pfc",
        "slug": "pfc",
        "name": "PFC Agent",
        "description": "Helps Reset Fitness studio staff with CRM/ProfitConnect activities.",
        "active_model": PFC_MODEL,
    },
]

# Hardcoded fallback persona per agent — what get_system_prompt() returns
# until an admin saves a custom one, and what reset_system_prompt() reverts
# to. Keep in sync with _AGENTS' slugs.
_DEFAULT_PROMPTS = {
    "tribe-app": TRIBE_APP_SYSTEM_PROMPT,
    "pfc": PFC_SYSTEM_PROMPT,
}


def _get_agent_or_404(agent_slug: str) -> dict:
    for agent in _AGENTS:
        if agent["slug"] == agent_slug:
            return agent
    raise HTTPException(status_code=404, detail="Agent not found")


def _root_agent_for(agent_slug: str):
    if agent_slug == "tribe-app":
        from tribe_app.agent import root_agent
        return root_agent
    if agent_slug == "pfc":
        from pfc.agent import root_agent
        return root_agent
    raise HTTPException(status_code=404, detail="Agent not found")


@router.get("/agents")
def get_agents(authorization: str = Header(None)):
    _require_admin(authorization)
    return _AGENTS


@router.get("/agents/{agent_slug}")
def get_agent_route(agent_slug: str, authorization: str = Header(None)):
    _require_admin(authorization)
    agent = _get_agent_or_404(agent_slug)
    return {
        **agent,
        # The dashboard's Config panel reads this to prefill the textarea.
        # This is the editable PERSONA half only (DB-backed if an admin
        # has saved one, the hardcoded default otherwise) — the RULES
        # text that guards real bookings is never included here and
        # can't be edited through this endpoint. See the module
        # docstring for the reasoning.
        "system_prompt": get_system_prompt(agent_slug, default=_DEFAULT_PROMPTS[agent_slug]),
    }


@router.put("/agents/{agent_slug}")
def update_agent_route(agent_slug: str, payload: dict, authorization: str = Header(None)):
    """Saves a new persona prompt for this agent. Takes effect on the
    agent's very next message (build_instruction in tribe_app/agent.py
    or pfc/agent.py reads it fresh every turn) — no redeploy, no
    restart. Does NOT accept a `model` field on purpose — see the
    module docstring."""
    _require_admin(authorization)
    _get_agent_or_404(agent_slug)
    system_prompt = payload.get("system_prompt")
    if system_prompt is None:
        raise HTTPException(status_code=400, detail="system_prompt is required")
    system_prompt = system_prompt.strip()
    if not system_prompt:
        raise HTTPException(status_code=400, detail="system_prompt cannot be empty")

    try:
        set_system_prompt(agent_slug, system_prompt)
    except Exception:
        # Don't leak DB internals to the client, but don't swallow it
        # silently either — a failed save that looks like it succeeded
        # is exactly the kind of bug that's invisible until someone
        # notices the agent never changed behavior.
        logger.exception("Failed to save system_prompt for agent %s", agent_slug)
        raise HTTPException(status_code=500, detail="Failed to save system prompt")

    return {"status": "updated", "system_prompt": system_prompt}


@router.delete("/agents/{agent_slug}/system-prompt")
def reset_agent_prompt_route(agent_slug: str, authorization: str = Header(None)):
    """Reverts this agent's persona to the hardcoded default in
    common/config.py, discarding any admin-saved override."""
    _require_admin(authorization)
    _get_agent_or_404(agent_slug)
    try:
        reset_system_prompt(agent_slug)
    except Exception:
        logger.exception("Failed to reset system_prompt for agent %s", agent_slug)
        raise HTTPException(status_code=500, detail="Failed to reset system prompt")
    return {"status": "reset", "system_prompt": _DEFAULT_PROMPTS[agent_slug]}


# ---------------------------------------------------------
# Knowledge base (scoped to an agent slug) — unchanged
# ---------------------------------------------------------

@router.get("/agents/{agent_slug}/knowledge-base")
def list_kb_docs_route(agent_slug: str, authorization: str = Header(None)):
    _require_admin(authorization)
    rows = list_kb_docs(agent_slug)
    return [{"id": r["id"], "filename": r["filename"], "active": bool(r["active"]), "uploaded_at": r["uploaded_at"]} for r in rows]


@router.post("/agents/{agent_slug}/knowledge-base")
async def upload_kb_doc_route(agent_slug: str, file: UploadFile = File(...), authorization: str = Header(None)):
    _require_admin(authorization)
    content_bytes = await file.read()
    content_text = content_bytes.decode("utf-8", errors="ignore")
    doc_id = insert_kb_doc(agent_slug, file.filename, content_text)

    try:
        chunk_count = index_kb_doc(doc_id, agent_slug, content_text)
    except Exception as exc:
        return {"status": "uploaded", "filename": file.filename, "indexed": False, "index_error": str(exc)}

    return {"status": "uploaded", "filename": file.filename, "indexed": True, "chunks": chunk_count}


@router.put("/agents/{agent_slug}/knowledge-base/{doc_id}/toggle")
def toggle_kb_doc_route(agent_slug: str, doc_id: int, authorization: str = Header(None)):
    _require_admin(authorization)
    new_state = toggle_kb_doc(doc_id)
    if new_state is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "updated", "active": bool(new_state)}


@router.delete("/agents/{agent_slug}/knowledge-base/{doc_id}")
def delete_kb_doc_route(agent_slug: str, doc_id: int, authorization: str = Header(None)):
    _require_admin(authorization)
    delete_kb_doc(doc_id)
    return {"status": "deleted"}


# ---------------------------------------------------------
# Usage — reads usage_logs, populated by each agent's after_model_callback
# (common/booking_gate.py's make_log_usage), wired in tribe_app/agent.py
# and pfc/agent.py.
# ---------------------------------------------------------

def _summarize_usage(rows) -> list[dict]:
    # $ per million tokens — same figures as the main project. Grok rates
    # are approximate placeholders; check https://console.x.ai for current
    # pricing before trusting estimated_cost_usd for grok-* models.
    rate_per_million = {
        "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
        "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
        "grok-4": {"input": 3.0, "output": 15.0},
        "grok-3-mini": {"input": 0.3, "output": 0.5},
        # Gemini via AI Studio's free tier is $0 — these are the PAID-tier
        # rates only, so estimated_cost_usd will overstate cost while
        # you're on the free key. Update once you're on a paid Gemini key
        # (or if Google changes pricing) — see https://ai.google.dev/pricing
        # gemini-2.5-flash retired for new API keys — kept here in case
        # older usage_logs rows still reference it. gemini-3.6-flash is
        # the current default; its per-million rate isn't filled in yet
        # (check https://ai.google.dev/pricing), so its estimated cost
        # will show as $0 until you fill it in here.
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
        "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
        "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
        "gemini-3.6-flash": {"input": 0, "output": 0},
        "gemini-3.5-flash-lite": {"input": 0, "output": 0},
    }
    summary = []
    for model, requests, total_input, total_output in rows:
        total_input = total_input or 0
        total_output = total_output or 0
        # LiteLlm model names are provider-prefixed (e.g.
        # "anthropic/claude-sonnet-4-5"); native Gemini names (e.g.
        # "gemini-2.5-flash") have no prefix. Strip one if present so
        # this matches the rate table either way.
        bare_model = model.split("/", 1)[-1] if model else model
        rates = rate_per_million.get(bare_model, {"input": 0, "output": 0})
        est_cost = (total_input / 1_000_000 * rates["input"]) + (total_output / 1_000_000 * rates["output"])
        summary.append({
            "model": model,
            "requests": requests,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "estimated_cost_usd": round(est_cost, 4),
        })
    return summary


@router.get("/usage")
def get_global_usage(authorization: str = Header(None)):
    _require_admin(authorization)
    return _summarize_usage(get_usage_summary())


@router.get("/agents/{agent_slug}/usage")
def get_agent_usage(agent_slug: str, authorization: str = Header(None)):
    _require_admin(authorization)
    _get_agent_or_404(agent_slug)
    return _summarize_usage(get_usage_summary(agent_slug))


# ---------------------------------------------------------
# Test chat (scoped to an agent, throwaway session — same guarantee as
# the main project's test-chat: never touches real member/staff history)
# ---------------------------------------------------------

class _TestChatBody:
    """Not a Pydantic model on purpose — matches the raw-dict pattern
    already used for /admin/login above. The dashboard sends
    {"message": "...", "session_id": "..."} — session_id is optional on
    the FIRST message of a test conversation (one is created and handed
    back in the response) but required on every message after that, or
    multi-turn flows like the booking confirmation gate can't work — see
    the module-level comment above _test_runners for why."""


# ---------------------------------------------------------
# Test-chat runners/sessions, kept alive across requests (module-level,
# process lifetime). This matters for anything that spans more than one
# turn — most importantly common/booking_gate.py's confirmation gate:
# "book X" -> "please confirm (yes/no)" -> "yes" only works if turn 2
# lands in the SAME session as turn 1, so the pending_booking stashed in
# session state is still there to resolve. The previous version of this
# endpoint created a brand-new InMemoryRunner (and therefore a brand-new
# InMemorySessionService) on every single call, so there was no way to
# ever test that flow through the dashboard — every message started a
# fresh, empty session. Fixed by caching one runner per agent_slug and
# only calling create_session the first time a given session_id is seen.
# ---------------------------------------------------------

_test_runners: dict = {}
_test_sessions_created: set = set()


def _get_test_runner(agent_slug: str):
    if agent_slug not in _test_runners:
        from google.adk.runners import InMemoryRunner
        root_agent = _root_agent_for(agent_slug)
        _test_runners[agent_slug] = InMemoryRunner(agent=root_agent, app_name=f"{agent_slug}-admin-test")
    return _test_runners[agent_slug]


@router.post("/agents/{agent_slug}/test-chat")
async def test_chat_route(agent_slug: str, payload: dict, authorization: str = Header(None)):
    _require_admin(authorization)
    _get_agent_or_404(agent_slug)
    message = payload.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    # Reuse the session_id the dashboard sends back to us; only mint a
    # new one if this is the first message of a test conversation.
    session_id = payload.get("session_id") or f"admin-test:{uuid.uuid4()}"
    user_id = "admin-test-user"

    runner = _get_test_runner(agent_slug)
    session_key = f"{agent_slug}:{session_id}"
    if session_key not in _test_sessions_created:
        await runner.session_service.create_session(app_name=runner.app_name, user_id=user_id, session_id=session_id)
        _test_sessions_created.add(session_key)

    reply_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=message)]),
    ):
        if event.content and event.content.role == "model" and event.content.parts:
            texts = [p.text for p in event.content.parts if p.text]
            if texts:
                reply_text = "".join(texts)

    # session_id is new here on purpose: the dashboard needs to send it
    # back on the NEXT message of this same test conversation, or the
    # confirmation gate (and anything else multi-turn) can't be tested.
    return {"reply": reply_text, "session_id": session_id}
