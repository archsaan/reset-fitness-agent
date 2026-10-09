"""
Public, real-member chat endpoint for member-goal-setter ONLY.

This agent is excluded from get_fast_api_app()'s normal auto-discovery
(see common/excluding_agent_loader.py and main.py) specifically so its
real conversations do NOT get written to Postgres via the shared
DatabaseSessionService that tribe-app/pfc use — per your call going into
launch: "we don't need to keep user sessions as of now" for this agent.

So this router gives it the ONLY way to reach it in production: a plain
InMemoryRunner, same mechanism (and same real-reply, real-tool-call
behavior) as routers/admin.py's test-chat, but public (no admin bearer
token) and intentionally NOT the throwaway-session kind - a session_id
here IS a real member's conversation, it's just never persisted past
process memory. Restarting this service loses any in-progress 2-question
exchange; a member just starts over, which is low-stakes for an agent
whose entire job is a short bounded exchange (see
member_goal_setter/agent.py's module docstring on why it has no booking
gate / multi-turn state that would make that more costly).

If this agent's scope ever grows to need durable multi-turn state (e.g.
it starts also helping book, like tribe_app/pfc), revisit this - that's
exactly the kind of agent that DOES need the shared session service, and
the right fix then is removing it from the exclusion set in main.py, not
extending this in-memory approach.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException
from google.adk.runners import InMemoryRunner
from google.genai import types

from member_goal_setter.agent import root_agent

logger = logging.getLogger("reset_fitness_adk.live")

router = APIRouter(prefix="/live", tags=["live"])

APP_NAME = "member_goal_setter-live"
_runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
_sessions_created: set[str] = set()


@router.post("/member-goal-setter/chat")
async def member_goal_setter_chat(payload: dict):
    """Body: {"message": "...", "session_id": "..."}. session_id is
    optional on the first message (one is minted and returned); pass it
    back on every message after that in the SAME conversation, or each
    call starts a brand-new, memory-less exchange."""
    message = payload.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    session_id = payload.get("session_id") or f"live:{uuid.uuid4()}"
    user_id = payload.get("user_id") or "member"

    if session_id not in _sessions_created:
        await _runner.session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        _sessions_created.add(session_id)

    reply_text = ""
    async for event in _runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=message)]),
    ):
        if event.content and event.content.role == "model" and event.content.parts:
            texts = [p.text for p in event.content.parts if p.text]
            if texts:
                reply_text = "".join(texts)

    return {"reply": reply_text, "session_id": session_id}
