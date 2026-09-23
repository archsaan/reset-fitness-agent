"""
Tools for the ADK version of the booking agent.

Deliberately mirrors the tool shapes from the real (LangGraph) Reset
Fitness agent — same field names (calendar_schedule_id, class_name,
target_date, start_time, lead_name) — so this is a fair architecture
comparison, not a different problem.

Schedule data here is MOCKED (a small in-memory list), not the live
ProfitConnect API. This is a learning sandbox — keep it that way. If you
ever want to point this at the real PFC API, copy the httpx calls from
app/agent/tools/schedule_tools.py and booking_tools.py in the main
project rather than rebuilding them from scratch.
"""

from datetime import date, timedelta

_MOCK_SCHEDULE = [
    {
        "calendar_schedule_id": 101,
        "class_name": "Sunrise Yoga",
        "discipline": "Yoga",
        "start_time": "07:00",
        "end_time": "08:00",
        "coach": "Amara K.",
        "capacity": 15,
        "booked": 15,
    },
    {
        "calendar_schedule_id": 102,
        "class_name": "HIIT Blast",
        "discipline": "HIIT",
        "start_time": "18:00",
        "end_time": "18:45",
        "coach": "Marco D.",
        "capacity": 20,
        "booked": 12,
    },
    {
        "calendar_schedule_id": 103,
        "class_name": "Strength Fundamentals",
        "discipline": "Strength",
        "start_time": "19:00",
        "end_time": "20:00",
        "coach": "Priya S.",
        "capacity": 12,
        "booked": 4,
    },
]


def _day_label(target_date: str) -> str:
    target = date.fromisoformat(target_date)
    today = date.today()
    if target == today:
        return "today"
    if target == today + timedelta(days=1):
        return "tomorrow"
    return target.strftime("%A, %B %d")


def get_schedule(target_date: str = "") -> list:
    """Get the full mock class schedule for a given date (YYYY-MM-DD). If
    target_date is left blank, defaults to today."""
    if not target_date:
        target_date = date.today().isoformat()
    return [
        {
            **cls,
            "spots_left": max(cls["capacity"] - cls["booked"], 0),
            "date": target_date,
        }
        for cls in _MOCK_SCHEDULE
    ]


def check_availability(class_name: str, target_date: str = "", max_days_ahead: int = 3) -> list:
    """Check if a specific class has open spots on a given date (YYYY-MM-DD),
    also checking the next few days. Each result includes a 'day_label'."""
    if not target_date:
        target_date = date.today().isoformat()

    start = date.fromisoformat(target_date)
    results = []
    for i in range(max_days_ahead + 1):
        check_date = start + timedelta(days=i)
        for cls in _MOCK_SCHEDULE:
            if class_name.lower() in cls["class_name"].lower():
                spots_left = max(cls["capacity"] - cls["booked"], 0)
                results.append({
                    "date": check_date.isoformat(),
                    "day_label": _day_label(check_date.isoformat()),
                    "calendar_schedule_id": cls["calendar_schedule_id"],
                    "class_name": cls["class_name"],
                    "start_time": cls["start_time"],
                    "spots_left": spots_left,
                    "coach": cls["coach"],
                })
    return results


def book_class(calendar_schedule_id: int, class_name: str, target_date: str, start_time: str, lead_name: str) -> dict:
    """Book a member into a class. Only call after check_availability has
    confirmed there is space — pass the exact calendar_schedule_id that
    check_availability returned for the class/time the member chose.

    This function body is the REAL booking logic (mocked here — swap in
    real HTTP calls to book against an actual system when you're ready).
    Note what's absent: no confirmation-check code. That's the point of
    ADK's require_confirmation=True (set where this tool is registered
    in agent.py) — the framework itself withholds this function from
    ever running until a human has approved the call. There's no
    "did they say yes" branch to get wrong here, unlike a hand-rolled
    gate would need.
    """
    return {
        "success": True,
        "booking": {
            "calendar_schedule_id": calendar_schedule_id,
            "class_name": class_name,
            "date": target_date,
            "start_time": start_time,
            "lead_name": lead_name,
        },
    }
