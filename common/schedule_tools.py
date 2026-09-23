"""
Schedule tools — real ProfitConnect integration, identical logic to
app/agent/tools/schedule_tools.py in the main project. Read-only, so no
approval-gate concerns. ADK auto-wraps these as tools because they're
plain Python functions with type hints and a docstring — no @tool
decorator or FunctionTool wrapper needed (that's LangChain's pattern).
"""

from datetime import date, datetime, timedelta

import httpx

from common.config import FACILITY_ID, SCHEDULE_API_KEY, SCHEDULE_API_URL


def _fetch_schedule(target_date: str | None = None) -> list:
    if not target_date:
        target_date = date.today().isoformat()

    payload = {"facility_id": FACILITY_ID, "room_id": "", "date": target_date}
    headers = {}
    if SCHEDULE_API_KEY:
        headers["Authorization"] = f"Bearer {SCHEDULE_API_KEY}"

    response = httpx.post(SCHEDULE_API_URL, json=payload, headers=headers, timeout=10)
    data = response.json()

    cleaned = []
    for cls in data.get("schedule", []):
        capacity = int(cls["capacity"])
        booked = int(cls["booked"])
        cancelled = int(cls.get("cancelled", 0))
        spots_left = max(capacity - (booked - cancelled), 0)
        cleaned.append({
            "calendar_schedule_id": cls["id"],
            "class_name": cls["class_name"],
            "discipline": cls["discipline_name"],
            "start_time": cls["start_time"],
            "end_time": cls["end_time"],
            "coach": f'{cls["coach"][0]["firstname"]} {cls["coach"][0]["lastname"]}' if cls.get("coach") else None,
            "spots_left": spots_left,
            "class_type": cls["class_type"],
        })
    return cleaned


def get_schedule(target_date: str = "") -> list:
    """Get the full class schedule for Reset Fitness on a given date
    (YYYY-MM-DD). If target_date is left blank, defaults to today."""
    return _fetch_schedule(target_date or None)


def check_availability(class_name: str, target_date: str = "", max_days_ahead: int = 3) -> list:
    """Check if a specific class has open spots on a given date
    (YYYY-MM-DD), checking the next few days too if the requested date is
    full. Each result includes a 'day_label' field — always trust that
    label rather than calculating the day yourself."""
    if not target_date:
        target_date = date.today().isoformat()

    start = datetime.strptime(target_date, "%Y-%m-%d")
    today = date.today()

    results = []
    for i in range(max_days_ahead + 1):
        check_date_obj = (start + timedelta(days=i)).date()
        check_date = check_date_obj.isoformat()

        if check_date_obj == today:
            day_label = "today"
        elif check_date_obj == today + timedelta(days=1):
            day_label = "tomorrow"
        else:
            day_label = check_date_obj.strftime("%A, %B %d")

        schedule = _fetch_schedule(check_date)
        matches = [c for c in schedule if class_name.lower() in c["class_name"].lower()]

        for cls in matches:
            results.append({
                "date": check_date,
                "day_label": day_label,
                "calendar_schedule_id": cls["calendar_schedule_id"],
                "class_name": cls["class_name"],
                "start_time": cls["start_time"],
                "spots_left": cls["spots_left"],
                "coach": cls["coach"],
            })
    return results
