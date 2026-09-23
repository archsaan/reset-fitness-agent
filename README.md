# Reset Fitness Booking Agent — ADK rebuild (learning project)

Same problem as the main (LangGraph) `reset-fitness-agent` project —
check class schedules, book a class only after explicit member
confirmation — rebuilt with Google's Agent Development Kit, to compare
frameworks. Uses **mocked** schedule data, not the live ProfitConnect
API. Keep it that way unless you deliberately decide otherwise.

## Quickest way to see it running

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Get an Anthropic API key at https://console.anthropic.com (this
   agent runs on Claude by default, same model family as the real
   project), then:
   ```
   cp booking_agent/.env.example booking_agent/.env
   # paste your key into ANTHROPIC_API_KEY= in that file
   ```

3. From this folder, run ADK's own dev UI — no frontend to build:
   ```
   adk web
   ```
   Open the URL it prints, pick `booking_agent` from the dropdown, and
   chat with it directly in the browser.

That's it — no FastAPI app, no dashboard, no Postgres needed for this
to work. That's deliberate: the point of this project is to compare the
agent frameworks, not to rebuild the whole stack a second time.

## What to actually test

1. Ask it what's on today, or for a specific class ("what yoga classes
   are on this week?") — exercises `get_schedule`/`check_availability`.
2. Ask it to book you into a class, give your name when asked.
3. Watch what happens next: the chat will show a **pending confirmation**
   before the booking tool actually runs — that's ADK's built-in
   `require_confirmation=True` pausing the tool call, the same idea as
   the `interrupt()` gate in the real project, done a different way.
   Approve or reject it in the UI and see the tool either run for real
   or get skipped entirely.

## Where to look in the code

- `booking_agent/agent.py` — the ADK equivalent of `app/agent/graph.py`
  in the main project. Read the comments there first — they call out
  where ADK's approach diverges from the LangGraph one.
- `booking_agent/tools.py` — same tool shapes as the real project
  (`calendar_schedule_id`, `class_name`, etc.), mocked data instead of
  live HTTP calls. `book_class`'s docstring explains why its body
  contains zero confirmation-checking code, unlike the real project's
  `confirm_booking_node`.

## Switching back to Gemini

Claude is the default here (via LiteLlm) for a closer comparison to the
real project, which also runs on Claude. If you want the fastest
possible path instead (no LiteLlm, no extra install, just a free key),
swap `agent.py`'s `model=` line back to `"gemini-2.5-flash"` and put
`GOOGLE_API_KEY` in `.env` instead — see the commented block in
`agent.py`.

## Known ADK limitation worth knowing

The tool-confirmation mechanism used here (`require_confirmation=True`)
does **not** support the `DatabaseSessionService` or `VertexAiSessionService`
— only `InMemorySessionService` (what `adk web`/`adk run` use by
default). That means, as of this ADK version, confirmations don't
survive a process restart the way the LangGraph project's Postgres
checkpointer does. This is exactly why the full port below (`common/`,
`tribe_app/`, `pfc/`) does NOT use `require_confirmation=True` — it needs
Postgres-backed sessions for real member conversations, so it hand-rolls
the same gate LangGraph gives you via `interrupt()`, using plain ADK
callbacks instead. See `common/booking_gate.py`.

---

# Full port — tribe_app + pfc (production-shaped)

This is the real port of the whole `reset-fitness-agent` (LangGraph)
project onto ADK: both agents, live ProfitConnect schedule/booking APIs,
Neon-backed knowledge base retrieval (RAG), and Postgres-persisted
sessions — built after `booking_agent/` above as a learning sandbox,
alongside it rather than replacing it.

```
common/
  config.py         # every constant/knob — mirrors app/core/config.py
  kb.py             # KB docs + chunking + Voyage embeddings + pgvector search
  schedule_tools.py # get_schedule, check_availability — real PFC API
  booking_tools.py  # book_class (proposes only) + execute_booking (real HTTP calls)
  booking_gate.py   # the hand-rolled approval gate — read this first
tribe_app/agent.py  # Riley — member-facing agent
pfc/agent.py        # staff-facing agent
routers/admin.py    # KB upload/list/toggle/delete (the one thing ADK's
                     # own generated app doesn't give you for free)
main.py             # FastAPI entrypoint: ADK's get_fast_api_app() + admin_router
```

## Why this doesn't use `require_confirmation=True`

Covered above — it's incompatible with `DatabaseSessionService`. Instead,
`common/booking_gate.py` implements the same two-step pattern the main
project's `confirm_booking_node` gets from LangGraph's `interrupt()`:

1. **`before_tool_callback`** (`intercept_book_class`) — runs the instant
   the model tries to call `book_class`. Never executes a real booking.
   Stashes the proposed booking in session state and hands back a
   confirmation question as the "tool result" instead, which the model
   relays to the member.
2. **`before_agent_callback`** (`resolve_pending_booking`) — runs at the
   start of the *next* turn, before the model sees anything. If a
   booking is pending, it reads the member's new message directly,
   decides yes/no with the exact same plain keyword check as the main
   project (`_is_affirmative` — deliberately not an LLM judgment call),
   and — only if confirmed — calls `execute_booking()` for real. No
   model involvement in that decision at all, same as the main project.
   Otherwise, it refreshes this turn's `{kb_context}` (RAG retrieval)
   before letting the model run normally.

Both callbacks were verified directly against the installed
`google-adk==2.9.2` package's actual field types and `Context` API (not
guessed from docs) — see the inline comments in `booking_gate.py`.

## Run it

```
pip install -r requirements.txt
cp .env.example .env
nano .env   # same keys as the main project: ANTHROPIC_API_KEY, SCHEDULE_API_KEY,
            # DATABASE_URL (Neon), VOYAGE_API_KEY
```

Two ways to run:

**A. ADK's own dev UI (fastest, matches `adk web` from the learning build):**
```
adk web
```
Pick `tribe_app` or `pfc` from the dropdown. Sessions are still
in-memory this way — fine for quick manual testing.

**B. The full FastAPI app (production-shaped, Postgres-persisted sessions):**
```
uvicorn main:app --host 0.0.0.0 --port 8080
```
Then open `http://localhost:8080/dev-ui` for the same chat UI, but now
backed by `DatabaseSessionService` (Neon) — conversations survive a
restart. Knowledge-base admin routes live at `/admin/agents/{tribe-app|pfc}/knowledge-base`
(same auth pattern as the main project — `Authorization: Bearer <MOCK_ADMIN_TOKEN>`).

## What to actually test

1. Ask about today's schedule or a specific class — exercises the real
   PFC `get_schedule`/`check_availability` calls.
2. Ask to book a class, give your name. Confirm you get asked to
   confirm (not an immediate booking).
3. Reply "no" — confirm nothing gets booked and the pending state clears
   (ask to book something else right after; it should work cleanly).
4. Reply "yes" — confirm the real PFC validate→book HTTP flow runs.
5. Restart the server mid-conversation (only meaningful on the FastAPI
   path, B above) and confirm the conversation history is still there —
   that's the whole point of `DatabaseSessionService` over
   `InMemorySessionService`.
6. Upload a KB doc via `POST /admin/agents/tribe-app/knowledge-base` and
   ask a question it should answer — confirms Voyage + pgvector
   retrieval is wired correctly.
