"""
Twilio WhatsApp webhook for Riley - replaces the n8n "Riley - Twilio
WhatsApp" workflow (webhook -> IF has body -> Gemini -> Twilio REST send).

Same flow, minus the moving parts:
  - Twilio POSTs the inbound message here (configure the sandbox's "When a
    message comes in" URL to <PUBLIC_BASE_URL>/whatsapp/twilio).
  - We reply in the webhook response itself as TwiML, so there is no
    outbound Twilio REST call and no Account SID needed. Only the auth
    token, used to verify that the request really came from Twilio.
  - Conversations persist per phone number in the same Postgres session
    store as the other agents (the n8n version had no memory at all).

The route is public, so the X-Twilio-Signature check is what stops anyone
who finds the URL from spending model quota. It fails closed: with no
TWILIO_AUTH_TOKEN configured, requests are refused (503) unless
TWILIO_VALIDATE_SIGNATURE=false is set explicitly for local testing.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
from collections import OrderedDict
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from common.config import (
    ADK_SESSION_DB_URL,
    PUBLIC_BASE_URL,
    TWILIO_AUTH_TOKEN,
    TWILIO_VALIDATE_SIGNATURE,
)
from riley_whatsapp.agent import root_agent

logger = logging.getLogger("reset_fitness_adk.whatsapp")

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

APP_NAME = "riley_whatsapp"

# Twilio gives a webhook ~15s to answer before it gives up and retries.
REPLY_TIMEOUT_SECONDS = 12
# WhatsApp messages cap at 1600 characters through Twilio.
MAX_REPLY_CHARS = 1500
FALLBACK_REPLY = "Thanks for your message! Our team will be in touch with you very shortly! 😊"

_session_service: DatabaseSessionService | None = None
_runner: Runner | None = None

# One lock per phone number, so two messages sent back-to-back are handled
# in order instead of racing on the same session's state.
_locks: dict[str, asyncio.Lock] = {}

# Twilio retries a webhook it considers failed, with the same MessageSid;
# remembering recent ones stops a retry from getting a second reply.
_seen_sids: OrderedDict[str, None] = OrderedDict()
_SEEN_SIDS_MAX = 1000


def _get_runner() -> Runner:
    global _session_service, _runner
    if _runner is None:
        _session_service = DatabaseSessionService(db_url=ADK_SESSION_DB_URL)
        _runner = Runner(
            agent=root_agent,
            app_name=APP_NAME,
            session_service=_session_service,
            auto_create_session=True,
        )
    return _runner


def compute_twilio_signature(url: str, params: list[tuple[str, str]], auth_token: str) -> str:
    """Twilio's documented scheme: HMAC-SHA1 over the full request URL
    followed by every POST param as key+value, params sorted by key,
    base64-encoded."""
    data = url + "".join(k + v for k, v in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _signature_is_valid(request: Request, params: list[tuple[str, str]]) -> bool:
    if not TWILIO_AUTH_TOKEN:
        return False
    # Behind a proxy the app sees http://internal-host; Twilio signed the
    # public URL it called, so PUBLIC_BASE_URL has to stand in for it.
    url = (PUBLIC_BASE_URL + request.url.path) if PUBLIC_BASE_URL else str(request.url)
    if request.url.query:
        url += "?" + request.url.query
    expected = compute_twilio_signature(url, params, TWILIO_AUTH_TOKEN)
    provided = request.headers.get("X-Twilio-Signature", "")
    return hmac.compare_digest(expected, provided)


def _twiml(text: str | None) -> Response:
    body = f"<Message>{escape(text)}</Message>" if text else ""
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )


async def _ask_riley(sender: str, message: str) -> str:
    runner = _get_runner()
    # whatsapp:+9715... -> a stable id; ADK's session ids are plain strings.
    session_id = "wa:" + sender.removeprefix("whatsapp:")
    reply = ""
    async with _locks.setdefault(session_id, asyncio.Lock()):
        async for event in runner.run_async(
            user_id=sender,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        ):
            if event.content and event.content.role == "model" and event.content.parts:
                texts = [p.text for p in event.content.parts if p.text]
                if texts:
                    reply = "".join(texts)
    return reply.strip()


@router.post("/twilio")
async def twilio_webhook(request: Request):
    form = await request.form()
    params = [(k, str(v)) for k, v in form.multi_items()]

    if TWILIO_VALIDATE_SIGNATURE:
        if not TWILIO_AUTH_TOKEN:
            logger.error("TWILIO_AUTH_TOKEN is not set - refusing WhatsApp webhook")
            raise HTTPException(status_code=503, detail="WhatsApp webhook is not configured")
        if not _signature_is_valid(request, params):
            logger.warning("Rejected WhatsApp webhook with invalid Twilio signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    body = (form.get("Body") or "").strip()
    sender = form.get("From") or ""
    sid = form.get("MessageSid") or ""

    # Delivery receipts, media-only messages etc. - nothing to answer.
    if not body or not sender:
        return _twiml(None)

    if sid:
        if sid in _seen_sids:
            return _twiml(None)
        _seen_sids[sid] = None
        if len(_seen_sids) > _SEEN_SIDS_MAX:
            _seen_sids.popitem(last=False)

    try:
        reply = await asyncio.wait_for(_ask_riley(sender, body), REPLY_TIMEOUT_SECONDS)
    except Exception:
        # Never leave a lead on read: quota exhaustion, DB blip or a slow
        # model all end up here, and the handoff line is always safe to send.
        logger.exception("Riley failed to answer WhatsApp message sid=%s", sid)
        reply = FALLBACK_REPLY

    return _twiml((reply or FALLBACK_REPLY)[:MAX_REPLY_CHARS])
