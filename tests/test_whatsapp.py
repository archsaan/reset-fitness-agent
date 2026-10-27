"""Offline tests for routers/whatsapp.py - no Gemini or Twilio calls."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import whatsapp

TOKEN = "12345"
URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
PARAMS = [
    ("CallSid", "CA1234567890ABCDE"),
    ("Caller", "+14158675309"),
    ("Digits", "1234"),
    ("From", "+14158675309"),
    ("To", "+18005551212"),
]


def test_signature_matches_twilio_documented_example():
    # Known-answer vector from Twilio's webhook security docs.
    assert whatsapp.compute_twilio_signature(URL, PARAMS, TOKEN) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(whatsapp, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(whatsapp, "TWILIO_VALIDATE_SIGNATURE", True)
    monkeypatch.setattr(whatsapp, "PUBLIC_BASE_URL", "https://example.test")
    whatsapp._seen_sids.clear()
    calls = []

    async def fake_ask(sender, message):
        calls.append((sender, message))
        return "Hi <there> & welcome"

    monkeypatch.setattr(whatsapp, "_ask_riley", fake_ask)
    app = FastAPI()
    app.include_router(whatsapp.router)
    c = TestClient(app)
    c.calls = calls
    return c


def _post(client, data, signed=True):
    headers = {}
    if signed:
        headers["X-Twilio-Signature"] = whatsapp.compute_twilio_signature(
            "https://example.test/whatsapp/twilio", list(data.items()), TOKEN
        )
    return client.post("/whatsapp/twilio", data=data, headers=headers)


def test_valid_message_gets_escaped_twiml_reply(client):
    r = _post(client, {"Body": "hi", "From": "whatsapp:+971500000000", "MessageSid": "SM1"})
    assert r.status_code == 200
    assert "<Message>Hi &lt;there&gt; &amp; welcome</Message>" in r.text
    assert client.calls == [("whatsapp:+971500000000", "hi")]


def test_unsigned_and_badly_signed_requests_are_rejected(client):
    data = {"Body": "hi", "From": "whatsapp:+971500000000"}
    assert _post(client, data, signed=False).status_code == 403
    r = client.post("/whatsapp/twilio", data=data, headers={"X-Twilio-Signature": "nope"})
    assert r.status_code == 403
    assert client.calls == []


def test_missing_token_fails_closed(client, monkeypatch):
    monkeypatch.setattr(whatsapp, "TWILIO_AUTH_TOKEN", "")
    assert _post(client, {"Body": "hi", "From": "whatsapp:+1"}).status_code == 503


def test_empty_body_and_duplicate_sid_get_no_reply(client):
    r = _post(client, {"Body": "", "From": "whatsapp:+971500000000", "MessageSid": "SM2"})
    assert "<Message>" not in r.text
    _post(client, {"Body": "hi", "From": "whatsapp:+971500000000", "MessageSid": "SM3"})
    r = _post(client, {"Body": "hi", "From": "whatsapp:+971500000000", "MessageSid": "SM3"})
    assert "<Message>" not in r.text
    assert len(client.calls) == 1


def test_model_failure_still_replies(client, monkeypatch):
    async def boom(sender, message):
        raise RuntimeError("quota")

    monkeypatch.setattr(whatsapp, "_ask_riley", boom)
    r = _post(client, {"Body": "hi", "From": "whatsapp:+971500000000", "MessageSid": "SM4"})
    assert r.status_code == 200
    assert "Our team will be in touch" in r.text
