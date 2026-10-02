# Weekly Goal-Setting Rules — DRAFT / PLACEHOLDER

> **This is a placeholder, not the real business rule.** You said the real
> rule for setting next week's goal will come later. This draft exists so
> the Sunday 2PM goal-planning job has *something* concrete to run and be
> tested against in the meantime — replace the logic in step 3 below once
> you have the real rule, everything else (inputs, safety bounds, missing-
> data handling) should stay regardless of what the real rule turns out to
> be.

## Scope decision — V1 vs V2

**V1 (build this now):** a member states their `fitness_goal` (and
optionally `fitness_frequency`) on the questionnaire, and the system
recommends their **first** weekly goal from that alone — no attendance
history, wellness report, or functional testing involved yet, because a
new member doesn't have any of that yet. This is just the Step 1 + Step 4
mapping from `class_recommendation_rules_DRAFT.md` (goal → category
weighting, frequency → session count) applied to produce a starting weekly
target. No AI needed — pure lookup, same reasoning as everywhere else in
this doc.

**V2 (later):** once a member has at least one week of real attendance
data (and eventually wellness/functional testing), the full Sunday 2PM job
below kicks in — adjusting that baseline goal up/down based on actual
performance. Everything below this point describes V2; it doesn't block
building V1 today.

## What this feeds

Every Sunday at 2PM, a scheduled job generates each member's goal for the
*upcoming* week — an overall goal type (e.g. "Weight Loss") and a session
target per category (Cardio, Shape, S&C, Strength, Wellness, Red Light
Therapy). That goal then drives:
- The category checklist and progress bars on the Attendance screen.
- The daily "outlook" sentence (see `coach_activity_api_spec.md`-style
  design discussed separately).

## Inputs (assumed shape — confirm with backend dev)

| Input | Assumed shape | Confirm? |
|---|---|---|
| `last_week_performance` | sessions completed per category + total, vs. last week's target | Should already exist from attendance data |
| `wellness_report` | e.g. self-reported energy/soreness/sleep quality, body composition trend | **Need real schema** — free text or structured fields? |
| `functional_testing` | e.g. strength/mobility/cardio fitness scores from periodic assessments | **Need real schema** — and how often is this actually re-tested? |

If `wellness_report` or `functional_testing` is missing or older than 30
days for a member, **ignore that input** for this run and fall back to the
attendance-only rule below — don't let a stale or absent assessment silently
skew a goal. Flag that member as "needs fresh assessment" instead of
guessing.

## Draft rule logic (placeholder — replace step 3)

**1. Baseline** — start from last week's target. If the member has no prior
target (new member / first run), default to a studio-standard baseline:
4 total sessions/week, spread evenly across the goal type's relevant
categories.

**2. Adjust total volume based on last week's completion:**
- Completed ≥ 100% of target → increase total weekly sessions by 1
  (progressive overload)
- Completed 70–99% → keep the same total
- Completed < 70% → decrease total by 1 (don't punish a hard week)

**3. [PLACEHOLDER — replace with real rule] Adjust using wellness report +
functional testing:**
- If the wellness report flags high fatigue, poor sleep, or soreness →
  do not increase volume this week even if rule 2 says to (cap at last
  week's total).
- If a functional-testing score has dropped in a specific category →
  reduce that category's target and add a Wellness/recovery session in
  its place.
- *(This is the part you said you'll give the real rule for later —
  everything above and below this step is meant to still apply once you
  swap it in.)*

**4. Split the total into per-category targets** using a placeholder
mapping by goal type (e.g. "Weight Loss" → emphasize Cardio + Strength;
"Mobility" → emphasize S&C + Wellness) — needs the real category-weighting
logic from you too, this is a rough placeholder distribution.

## Safety bounds (hard limits — enforced in code, never left to the LLM alone)

Same principle as `booking_gate.py` never trusting the model with identity
fields: whatever a model-assisted step proposes, these are checked and
clamped server-side before anything is stored or shown to a member.

- Minimum 2 total sessions/week, maximum 6.
- No single category above 3 sessions/week.
- Any week-over-week jump of more than 2 sessions total gets flagged for a
  trainer to glance at before it goes live, rather than reaching the member
  unreviewed. (You can turn this off later once you trust the rule, but
  worth having for the first rollout given this touches exercise load.)

## Still needed from you / backend before building

1. The real step-3 rule (wellness + functional testing → goal adjustment).
2. Real schema for `wellness_report` and `functional_testing` (structured
   fields vs. free text — changes whether this is a numeric rule or an LLM
   interpreting prose).
3. Real per-goal-type category weighting for step 4.
4. Whether trainer review-before-publish is wanted at launch, or only as a
   later safeguard.
