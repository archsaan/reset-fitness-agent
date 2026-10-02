# Class Recommendation Rules — DRAFT / PLACEHOLDER

> **Draft, pending your review** — especially the condition-based safety
> rules, which should be checked by a trainer/clinician before going live,
> not taken as final because an AI wrote them.

## What this feeds

Given a member's questionnaire answers (`fitness_goal`, `fitness_activity`,
`fitness_frequency`, `history.conditions`) plus their age, recommend which
**class categories** they should book to reach their goal, then let the
agent search the **real schedule** (`get_schedule` / `check_availability`,
already built) for actual bookable slots in those categories.

```
Questionnaire answers ──▶ Deterministic rule engine ──▶ Allowed/preferred
(goal, conditions, age)   (this doc)                    category list
                                                                │
                                                                ▼
                                              Agent calls get_schedule /
                                              check_availability (real,
                                              live data) to find actual
                                              slots in those categories
                                                                │
                                                                ▼
                                              Recommend to member (and
                                              book, if you want that —
                                              see open question at bottom)
```

No AI is needed for step 1 — it's a lookup table over categorical answers,
same as the weekly-goal rules. AI's job is only the live schedule-matching
and the natural-language recommendation, where real tool calls matter.

## Step 0 — New member with no assessment data

If the member has never completed `Full Body Mobility Assessment` or
`Full Body Composition Assessment`, recommend **that first**, before any
goal-based classes. This is also literally where the "functional testing"
data for the weekly-goal feature comes from — these two are real bookable
classes (category `Mobility Assessment` / `Composition Assessment`), not a
separate system.

## Step 1 — Goal → preferred categories (draft mapping)

| `fitness_goal` | Preferred categories |
|---|---|
| Weight loss | Cardio, Shape |
| Lose fat (inches) | Cardio, Shape |
| Muscle tone | Shape, Strength & Conditioning |
| Build strength | Strength, Strength & Conditioning |
| Improve flexibility | Mind & Body, Wellness |
| Improve cardio performance | Cardio |
| Improve stamina | Cardio, Strength & Conditioning |
| Gain weight/muscle | Strength, Strength & Conditioning |
| Improve overall health | Cardio, Strength, Mind & Body (rotate across all three) |
| Boost energy | Cardio, Bounce, Mind & Body |

Categories deliberately **never** recommended by this logic: `Corporate
Class`, `Studio Hire`, `Events`, `Specialty Class`, `On-Demand` (unless the
member has the On-Demand add-on — needs confirming) — these aren't
goal-driven categories.

## Step 2 — Teens classes (simplified — confirmed)

Teens classes are exclusively for kids, not an age-banding option a regular
adult member profile would ever need. So: **always exclude every class
whose name contains "Teens" (or "- Teens"), and `Teens Fundamentals` /
Fundamentals — Teen's Edition, from this engine entirely — no age check,
no branching, just a hard exclusion list.** This recommendation engine is
scoped to adult member profiles; if a parent/guardian ever sets up a
teen's own profile through the app, that's a separate flow to design later,
not something this logic needs to branch on today.

## Step 3 — Condition-based safety overrides (CONSERVATIVE DEFAULT — review this)

For **any** `history.conditions` answer other than `None`, this draft takes
the conservative position that a rule table (or an AI) should **not** be
the thing clearing a member with a real health condition for high-intensity
classes. Instead:

- Recommend `Personal Training (PT)` and/or a `Mobility Assessment` /
  `Composition Assessment` session first, where a real coach assesses them.
- Do **not** auto-recommend anything from `Strength & Conditioning` or the
  highest-intensity `Cardio` classes (Blaze, Firestarter, Inferno, Time
  Bomb, Magma) or high-impact `Bounce` classes until a trainer has signed
  off, regardless of what the goal says.
- `Mind & Body` and `Wellness` categories remain fine to recommend in the
  meantime — low-impact, trainer-description explicitly says "guided",
  "mindful", "restorative".

This is intentionally cautious and almost certainly over-broad for some
conditions (e.g. "Dental Problems" has nothing to do with exercise
capacity) — **you or a trainer should refine this into a per-condition
table** rather than one blanket rule. Treat this draft as the safe
starting point, not the final rule.

## Step 4 — Frequency → how many classes to recommend

Use `fitness_frequency` directly:
| Answer | Sessions/week to recommend |
|---|---|
| 1-2 times / week | 2 |
| 3-4 times / week | 3-4 |
| 5-6 times / week | 5 |
| Daily | 5-6 (studio capacity permitting) |
| Never | Start with 1-2 — don't recommend a frequency the member hasn't signaled readiness for |

(If the weekly-goal feature is also live, its computed target should take
precedence over this raw questionnaire answer, since it's recalculated
weekly from real performance — this table is the fallback for a member who
hasn't been through that yet.)

## Scope — recommend only (confirmed)

This feature **recommends** classes/times — it does not book them. The
agent surfaces real slots via `get_schedule` / `check_availability` and the
member taps to book themselves in the app, same as browsing the schedule
normally. `book_class` and its confirmation gate are **not** part of this
feature — simpler and lower-risk than wiring in booking, since there's no
member-initiated "yes, book it" moment to gate in the first place.
