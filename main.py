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

Also sets up basic observability — a request-logging middleware and a
/health endpoint — see the comments below for why each exists. Neither
is optional polish: without them, a Render cold-start, a DB outage, or
a Gemini quota exhaustion (all things that already happened once each
during this project's own testing) fails completely silently from the
outside. You only find out when a user complains.
"""

import logging
import time

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.adk.cli.fast_api import get_fast_api_app

from common.agent_config import init_agent_config_store
from common.config import ADK_SESSION_DB_URL, DATABASE_URL
from common.kb import init_kb_store
from routers.admin import router as admin_router

# Structured-ish logging to stdout. Render (and most hosts) capture
# stdout directly into their log viewer, so this is the whole
# "observability pipeline" for a project this size — no separate
# logging service needed yet. INFO so request logs show up; bump to
# WARNING in production once this is noisy rather than useful.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("reset_fitness_adk.main")

init_kb_store()
init_agent_config_store()

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


@app.get("/healthz")
def health_check():
    """Cheap, no-auth liveness/readiness check — DB connectivity only.

    NOTE the path: get_fast_api_app() above already registers its own
    bare `/health` (returns {"status": "ok"} unconditionally, no real
    check behind it — confirmed by inspecting app.routes: ADK's own
    google.adk.cli.api_server health endpoint is registered before this
    one, so a same-named route here would be silently shadowed and dead
    code, exactly the kind of thing this whole feature exists to catch).
    `/healthz` avoids the collision and is a common enough convention
    (Kubernetes et al.) that most uptime monitors recognize it fine too.

    Point an uptime monitor (UptimeRobot, Render's own health check, a
    cron ping) at this — two real benefits, not just a formality:
      1. It tells you FAST when the DB connection breaks, instead of
         only finding out when a real chat request 500s.
      2. Pinging it periodically also keeps a Render free-tier instance
         from cold-starting (spins down after ~15 min idle), which was
         the exact "why is the first request so slow" issue hit earlier
         in this project.
    Deliberately does NOT call Gemini/Claude here — a model health check
    would burn real quota/cost on every single monitor ping, which is
    the wrong trade for a check that's supposed to be free and frequent.
    """
    db_ok = True
    db_error = None
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=5)
        conn.close()
    except Exception as exc:
        db_ok = False
        db_error = str(exc)
        logger.exception("Health check: database connection failed")

    status = "ok" if db_ok else "degraded"
    body = {
        "status": status,
        "database": "ok" if db_ok else "error",
        "database_error": db_error,
    }
    # A 200 with "degraded" buried in the JSON body is invisible to most
    # uptime monitors, which typically alert on status code alone, not
    # response content — so this has to actually fail the HTTP request
    # (503) when unhealthy, not just describe itself as unhealthy.
    if not db_ok:
        return JSONResponse(status_code=503, content=body)
    return body


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Every request, logged with method/path/status/duration. This is
    the difference between "a booking silently failed somewhere" and
    being able to grep Render's logs for the one request that 500'd and
    see exactly how long it took and what path it hit. Cheap enough
    (one log line) to leave on permanently rather than only turning on
    when something's already broken."""
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.monotonic() - start) * 1000
        logger.exception(
            "%s %s failed after %.0fms", request.method, request.url.path, duration_ms
        )
        raise
    duration_ms = (time.monotonic() - start) * 1000
    log_level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(
        log_level,
        "%s %s -> %s (%.0fms)",
        request.method, request.url.path, response.status_code, duration_ms,
    )
    return response
