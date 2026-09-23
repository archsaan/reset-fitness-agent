"""
App entrypoint — the ADK equivalent of app/main.py in the main project.

get_fast_api_app() (from ADK itself) does the heavy lifting: it scans
this directory for agent packages (tribe_app/, pfc/, booking_agent/ —
anything with a root_agent in its agent.py), wires up ADK's own
/apps/.../run and /run_sse endpoints for each, and — with
session_db_url set — persists conversations to Postgres via
DatabaseSessionService instead of losing them on restart. `web=True`
also mounts ADK's own dev UI at /dev-ui, same one `adk web` gives you
locally, so this deployed app is browser-testable exactly like your
local session was.

On top of that, admin_router below adds the one thing ADK's own app
doesn't give you: knowledge-base management per agent (upload/list/
toggle/delete docs, same shape as the main project's /admin routes).
"""

from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

from common.config import ADK_SESSION_DB_URL
from common.kb import init_kb_store
from routers.admin import router as admin_router

init_kb_store()

app: FastAPI = get_fast_api_app(
    agents_dir=".",
    session_service_uri=ADK_SESSION_DB_URL,
    allow_origins=["*"],
    web=True,
)

app.include_router(admin_router)


@app.get("/")
def read_root():
    return {"message": "ADK FastAPI app is running!"}
