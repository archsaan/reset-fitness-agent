"""
CLI wrapper for common/weekly_goal_batch.py — run a weekly-goal batch
from the command line instead of the dashboard, e.g. for a one-off cost
check before wiring up the admin panel, or from a cron job.

This file previously contained an accidental full copy of
routers/admin.py (a stray paste, unrelated to this script's name) —
replaced here with what the name actually promises.

Usage:
    python -m scripts.weekly_goal_batch --count 10 --mode hybrid
    python -m scripts.weekly_goal_batch --count 5 --mode llm-only --dry-run
"""

import argparse
import json

from common import weekly_goal_batch
from common.config import MEMBER_GOAL_SETTER_MODEL


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10, help="Max number of test members")
    parser.add_argument("--mode", choices=["hybrid", "llm-only"], default="hybrid")
    parser.add_argument("--dry-run", action="store_true", help="Estimate tokens instead of calling the model")
    parser.add_argument("--save", action="store_true", help="Persist this run (same as the dashboard's Run batch)")
    args = parser.parse_args()

    if not 1 <= args.count <= weekly_goal_batch.MAX_MEMBERS_PER_RUN:
        parser.error(f"--count must be between 1 and {weekly_goal_batch.MAX_MEMBERS_PER_RUN}")

    members = weekly_goal_batch.workbook_members(limit=args.count)
    if not members:
        parser.error(f"No usable rows found in {weekly_goal_batch.WORKBOOK_PATH}")

    rows = weekly_goal_batch.run_batch(
        members, args.mode, MEMBER_GOAL_SETTER_MODEL, dry_run=args.dry_run, log_usage_rows=args.save
    )
    summary = weekly_goal_batch.summarize(rows)

    print(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))

    if args.save:
        run_id = weekly_goal_batch.save_run(args.mode, MEMBER_GOAL_SETTER_MODEL, args.dry_run, summary, rows)
        print(f"\nSaved as run #{run_id}")


if __name__ == "__main__":
    main()
