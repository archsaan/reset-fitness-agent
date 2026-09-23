"""
Booking — real ProfitConnect integration, same validate-then-book flow as
app/agent/tools/booking_tools.py in the main project.

Approval gate lives in common/booking_gate.py, NOT here and NOT via ADK's
require_confirmation=True (see the top-level README for why: that
mechanism doesn't support DatabaseSessionService, which this project
needs for Postgres-backed session persistence). Instead:
  - book_class (below) is the tool the model calls. It NEVER executes a
    real booking itself — every call is intercepted by
    booking_gate.intercept_book_class (a before_tool_callback) before
    this function body would ever run.
  - execute_booking (below) is the plain function that actually does the
    HTTP calls. Only booking_gate.py's before_agent_callback calls it,
    and only after a member's next message reads as a clear "yes".

member_id is hardcoded to TEST_MEMBER_ID for the same TEMPORARY reason as
the main project — no real member auth wired into /chat yet. Search for
TEST_MEMBER_ID before this goes near real members.
"""

import httpx

from common.config import (
    BOOKING_API_KEY,
    BOOKING_CREATE_API_URL,
    BOOKING_VALIDATE_API_URL,
    FACILITY_ID,
    TEST_MEMBER_ID,
)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {BOOKING_API_KEY}"} if BOOKING_API_KEY else {}


def _extract_failure_reason(data: dict) -> str:
    for key in ("message", "error", "reason", "detail"):
        if data.get(key):
            return str(data[key])
    return "The booking system declined this booking."


def execute_booking(calendar_schedule_id: int, class_name: str, target_date: str, start_time: str, lead_name: str) -> dict:
    """The real validate -> book HTTP flow. Call this directly only from
    booking_gate.py, only once a member has explicitly confirmed. Never
    call this from the book_class tool body — that's the whole point of
    the approval gate."""
    member_id = TEST_MEMBER_ID  # TEMPORARY — see module docstring.

    try:
        validate_resp = httpx.post(
            BOOKING_VALIDATE_API_URL,
            json={"calendar_schedule_id": calendar_schedule_id, "facility_id": FACILITY_ID, "member_id": member_id},
            headers=_auth_headers(),
            timeout=10,
        )
        validate_resp.raise_for_status()
        validate_data = validate_resp.json()
    except httpx.HTTPError as exc:
        return {"success": False, "reason": f"Couldn't reach the booking system to validate: {exc}"}

    if validate_data.get("valid") is False or validate_data.get("success") is False:
        return {"success": False, "reason": _extract_failure_reason(validate_data)}

    try:
        book_resp = httpx.post(
            BOOKING_CREATE_API_URL,
            json={
                "calendar_schedule_id": calendar_schedule_id,
                "facility_id": FACILITY_ID,
                "member_id": [member_id],
                "planStatus": "Active",
                "status": "Booked",
                "validateBy": "Membership",
            },
            headers=_auth_headers(),
            timeout=10,
        )
        book_resp.raise_for_status()
        book_data = book_resp.json()
    except httpx.HTTPError as exc:
        return {"success": False, "reason": f"Validated but couldn't complete the booking: {exc}"}

    if book_data.get("success") is False:
        return {"success": False, "reason": _extract_failure_reason(book_data)}

    return {
        "success": True,
        "booking": {
            "class_name": class_name,
            "date": target_date,
            "start_time": start_time,
            "lead_name": lead_name,
            "calendar_schedule_id": calendar_schedule_id,
        },
        "raw_response": book_data,
    }


def book_class(calendar_schedule_id: int, class_name: str, target_date: str, start_time: str, lead_name: str) -> dict:
    """Propose booking a member into a class. Only call after
    check_availability has confirmed there is space — pass the exact
    calendar_schedule_id it returned for the class/time the member chose.
    Calling this does NOT book the class yet. It returns a confirmation
    question; relay that question to the member verbatim and wait for
    their reply — do not call this tool again until they've answered."""
    # This body never actually runs in the live flow — booking_gate.py's
    # before_tool_callback intercepts every book_class call first (see
    # tribe_app/agent.py and pfc/agent.py, where it's registered). Kept
    # here only so the tool has a real, correctly-typed function to wrap,
    # and so it behaves sensibly if ever invoked directly (e.g. a test).
    return {
        "success": False,
        "awaiting_confirmation": True,
        "reason": "This should have been intercepted by the confirmation gate before reaching here.",
    }
