"""
Central configuration for the full ADK port (tribe_app + pfc).

Mirrors app/core/config.py in the main (LangGraph) project on purpose —
same env vars, same defaults — so the two projects can point at the same
Neon database and the same ProfitConnect credentials without any
translation. This file is the only one that reads os.environ directly.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------
# External schedule API (ProfitConnect) — same as the main project
# ---------------------------------------------------------

SCHEDULE_API_URL = "https://crmapi.profitconnect.co/calendar/get/dayschedule"
SCHEDULE_API_KEY = os.environ.get("SCHEDULE_API_KEY", "")

FACILITY_ID = int(os.environ.get("FACILITY_ID", "2"))

# ---------------------------------------------------------
# Booking API (real — ProfitConnect)
# ---------------------------------------------------------

BOOKING_VALIDATE_API_URL = "https://crmapi.profitconnect.co/member/booking/validate"
BOOKING_CREATE_API_URL = "https://crmapi.profitconnect.co/member/booking/book"
BOOKING_API_KEY = SCHEDULE_API_KEY

# TODO — TEMPORARY, same caveat as the main project: real member_id must
# come from the logged-in member once member auth exists. Search for
# TEST_MEMBER_ID before this goes near real members.
TEST_MEMBER_ID = int(os.environ.get("TEST_MEMBER_ID", "6"))

# ---------------------------------------------------------
# Database (Neon / any Postgres) — shared by ADK sessions + the KB store
# ---------------------------------------------------------
# Two forms of the same database are needed:
#   DATABASE_URL      - plain libpq form, for psycopg (the KB store here,
#                        same driver style as the main project)
#   ADK_SESSION_DB_URL - SQLAlchemy form, for ADK's DatabaseSessionService
#                        (session persistence). Derived automatically from
#                        DATABASE_URL below unless you set it explicitly.

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to a .env file in this project's root "
        "(see .env.example) or set it as a real environment variable in your "
        "hosting platform's dashboard. Use the same Neon connection string as "
        "the main reset-fitness-agent project if you want them to share data."
    )


def _to_sqlalchemy_url(libpq_url: str) -> str:
    """ADK's DatabaseSessionService takes a SQLAlchemy-style URL
    (postgresql+psycopg://...), not the plain postgresql:// / postgres://
    form Neon gives you. Rewrites the scheme only — leaves host, creds,
    query string (sslmode=require etc.) untouched."""
    if libpq_url.startswith("postgresql+"):
        return libpq_url  # already in SQLAlchemy form
    if libpq_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + libpq_url[len("postgresql://"):]
    if libpq_url.startswith("postgres://"):
        return "postgresql+psycopg://" + libpq_url[len("postgres://"):]
    return libpq_url


ADK_SESSION_DB_URL = os.environ.get("ADK_SESSION_DB_URL") or _to_sqlalchemy_url(DATABASE_URL)

# ---------------------------------------------------------
# RAG / embeddings (knowledge base retrieval) — same as the main project
# ---------------------------------------------------------

VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
EMBEDDING_MODEL = "voyage-3-lite"
EMBEDDING_DIM = 512
KB_RETRIEVAL_TOP_K = 4

# ---------------------------------------------------------
# Auth (placeholder, same pattern as the main project's MOCK_ADMIN_TOKEN)
# ---------------------------------------------------------

MOCK_ADMIN_TOKEN = os.environ.get("MOCK_ADMIN_TOKEN", "dev-admin-token-change-me")

# ---------------------------------------------------------
# Models
# ---------------------------------------------------------
# Two families of model name are supported here:
#   - "gemini-..."         -> ADK's NATIVE Gemini support. No LiteLlm
#                              wrapper needed; ADK reads GOOGLE_API_KEY
#                              straight from the environment. Get a free
#                              key at https://aistudio.google.com/apikey —
#                              gemini-2.5-flash has a genuinely free tier
#                              (rate-limited, not a trial), which is why
#                              it's the default below.
#   - "anthropic/..." etc. -> everything else still goes through LiteLlm
#                              (google.adk.models.lite_llm), same as
#                              before. LiteLLM reads ANTHROPIC_API_KEY /
#                              XAI_API_KEY from the environment itself.
#
# Defaulted to Gemini so a fresh checkout costs nothing to run; set
# TRIBE_APP_MODEL / PFC_MODEL back to "anthropic/claude-sonnet-4-5" (or
# any other LiteLLM-style name) in .env to switch an agent back to Claude.
#
# NOTE on WHICH Gemini model: gemini-2.5-flash was retired for new API
# keys, and its replacement gemini-3.6-flash turned out to be capped at
# a stingy 20 requests/day on the free tier (confirmed directly against
# this project's own quota page, https://aistudio.google.com/rate-limit).
# Checking that page across every model showed a clear pattern: every
# plain "Flash" model (2.5, 3, 3.5, 3.6, 3.7, 3.8 Flash) is capped at
# 20 RPD, but the "Flash Lite" variants get 500 RPD instead — 25x more
# headroom for the same $0. gemini-3.5-flash-lite is confirmed working
# on this project's key, hence the default below. If Google reshuffles
# quotas again, re-check https://aistudio.google.com/rate-limit and
# override via TRIBE_APP_MODEL / PFC_MODEL in .env — no code change
# needed either way.

TRIBE_APP_MODEL = os.environ.get("TRIBE_APP_MODEL", "gemini-3.5-flash-lite")
PFC_MODEL = os.environ.get("PFC_MODEL", "gemini-3.5-flash-lite")


def resolve_model(model_name: str):
    """Turns a config model name into whatever ADK's Agent(model=...)
    actually expects: a plain string for native Gemini, or a LiteLlm
    instance for anything else (Claude, Grok, ...). Import of LiteLlm is
    local so agents that only ever use Gemini don't need it installed/
    importable at all."""
    if model_name.startswith("gemini-") or model_name.startswith("gemini/"):
        return model_name[len("gemini/"):] if model_name.startswith("gemini/") else model_name
    from google.adk.models.lite_llm import LiteLlm
    return LiteLlm(model=model_name)

# ---------------------------------------------------------
# Shared rules text — identical wording to the main project's RULES,
# minus the KB/RULES headers (ADK instructions get those appended by
# common/prompts.py instead).
# ---------------------------------------------------------

RULES = (
    "Only answer factual questions (pricing, location, policies, services) using the "
    "Knowledge Base below. Never guess or invent details not contained in it. "
    "If the answer isn't in the Knowledge Base, say: 'Our team will have all "
    "the details and will be in touch with you very shortly! \U0001F60A' "
    "\n\nFor anything related to class times, schedules, or availability on a specific "
    "date — NEVER use static text, and never guess. Always use the get_schedule or "
    "check_availability tool instead, since those pull real, live data. "
    "\n\nUse get_schedule when a lead asks generally what's on or wants to browse classes. "
    "Use check_availability when a lead names a specific class and wants to know if "
    "there's space, or wants to book — this tool also checks upcoming days automatically "
    "if the requested date is full. Each result includes a 'day_label' field — always "
    "trust and use that label rather than calculating the day yourself. "
    "\n\nUse book_class ONLY after check_availability has confirmed there is space, and only "
    "once you have the lead's name. Always pass the exact calendar_schedule_id that "
    "check_availability returned for the specific class/date/time the lead chose — never "
    "guess or invent this id. Calling book_class never books anything immediately — it only "
    "proposes the booking; the member must separately confirm before it actually happens."
)

TRIBE_APP_SYSTEM_PROMPT = (
    "You are Riley, the Reset Fitness AI Assistant. Your goal is to make every lead "
    "feel heard and excited about Reset Fitness — answering questions accurately and "
    "helping them book classes. You are warm, friendly, confident, and never robotic. "
    "If asked your name, say 'I am Riley, the Reset Fitness AI Assistant! \U0001F60A' "
    "If asked if you are human, say 'I am Riley, Reset Fitness' AI Assistant — here to "
    "help just like a real team member would! \U0001F60A' Never claim to be human. "
    "Keep responses concise (2-3 sentences where possible) and use 1-2 emojis, but "
    "never on complaints or sensitive topics."
)

PFC_SYSTEM_PROMPT = (
    "You are the Reset Fitness PFC Assistant, a support tool for Reset Fitness studio "
    "staff using the ProfitConnect (PFC) system. Your job is to help staff quickly find "
    "class schedule information, check class capacity, and support day-to-day front-desk "
    "and CRM tasks. You speak to trained staff, not members — be direct, concise, and "
    "skip the member-facing warmth and emojis. If asked to do something outside what "
    "your current tools support (e.g. editing a member's profile, processing a refund), "
    "say plainly that this isn't wired up yet rather than guessing at an answer."
)
