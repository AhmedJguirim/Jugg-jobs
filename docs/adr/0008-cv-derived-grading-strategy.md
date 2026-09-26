# 0008 - CV-derived grading strategy with tiered skills

Status: Accepted - 2026-09-26

Supersedes the scoring rubric in [ADR-0007](0007-local-llm-enrichment.md); the
GPU and storage decisions there still stand.

## Context

The first profile (`profiles/default.json`) was **inferred, not asked for**. It
was assembled from this project's own stack and one stated fact (the user does
not read Dutch). When the actual CV arrived, three of its central assumptions
turned out to be wrong:

| Inferred profile said | The CV says |
|---|---|
| Python/FastAPI is the primary stack | **PHP/Laravel/Symfony** is; Python is "familiar" only |
| Targets mid/senior roles | Graduated Sep 2025; ~1 year full-time plus internships |
| Based in the Netherlands | **Sousse, Tunisia** - no EU work permit |

The consequences were not cosmetic. Python vacancies were scoring **85-92
"strong match"** on a profile whose owner has never used Python in production,
while the ranking said nothing about work authorisation - the single largest
practical barrier for this candidate.

A flat list of skills cannot express this. "Knows Python" and "ships Laravel
daily" are both just keyword hits.

## Decision

### 1. Skills are tiered, and the tier sets the score band

`profiles/<name>.json` now carries `skills.core`, `skills.proficient` and
`skills.familiar`. Each maps to a fixed band:

| Tier | Meaning | Band |
|---|---|---|
| `core` | used in production, daily | 75-95 |
| `proficient` | comfortable, not the main stack | 55-75 |
| `familiar` | touched, no production depth | 30-55 |
| `unrelated` | a stack in no tier | 10-30 |
| `non_technical` | not a software role | 0-10 |

The model scores *within* the band; the final score is clamped into it. A small
model that ignores its instructions cannot promote a Ruby job into the Laravel
band.

### 2. The tier is computed in code, not by the model

`app/llm/service.classify_stack()` decides the tier by matching the profile's
skills against the vacancy. This was not the first design. The model was asked
to do it, given the tiers, an explicit rule ("a technology in FAMILIAR is
familiar, never unrelated") and a worked example - and still labelled a Python
vacancy `unrelated` on a profile that lists Python under familiar. It conflated
"not their strongest stack" with "not in their skill set".

Tier membership is an exact lookup, so it is computed exactly. The model keeps
the part that needs judgement: where inside the band the vacancy sits.

The matcher is **weighted, not first-match**, because first-match is wrong on
real vacancies: a Python role that mentions PHP once among its nice-to-haves was
classified as core PHP work. A skill in the *title* weighs 10, a skill in the
body 2, with a small bonus for repeats. Matching is whole-word: without `\b`
anchors "Java" fires inside "JavaScript" and "Go" inside "Going".

### 3. Eligibility is reported, never folded into the score

Extending ADR-0007's split, the assessment now records:

- `dutch_required` - an explicit language requirement, not merely a Dutch text.
- `sponsorship_required` - **new**: true when the role needs the candidate
  physically in the Netherlands, because they hold no EU work permit. False only
  for fully remote roles doable from outside the EU.
- `seniority_fit` - `under_qualified` / `good` / `over_qualified`, read as "the
  candidate is ___ for this vacancy". The first version used `below`/`above`,
  which the model applied in both directions on the same vacancy; the names are
  now unambiguous.
- `blocked` - any of the above that makes the role unreachable.

`stack_match` and `seniority_fit` are real columns, not just `details` keys,
because "show me every core-stack role" is the query this pipeline exists to
answer.

### 4. The profile is derived from the CV, minus the personal details

`profiles/ahmed.json` carries skills, differentiators, domains, languages and
work authorisation. It deliberately omits phone number, email and address:
grading needs what the candidate can do and where they may work, not how to
contact them.

The profile also names **differentiators** - production debugging, performance
optimisation, queue and background-job work, third-party API integration - which
the prompt treats as a small bonus when a vacancy explicitly asks for them. This
is what a skills list alone misses about a candidate whose CV leads with "cut
failed API submissions by 90%".

## Amendment - 2026-09-26: education

The first version of the profile captured skills, languages, location and
domains from the CV but **dropped the education section entirely**. Dutch
vacancies state a level rather than a named degree - MBO (vocational), HBO
(university of applied sciences, roughly a bachelor's), WO (research
university) - so the model saw "HBO required", found nothing about education in
the profile, and reported it as a qualification the candidate lacks. It did so
on **343 of 546 assessments**, for someone holding a bachelor's and a master's.

The profile now records the degrees held and, more importantly, which Dutch
levels they satisfy, and the prompt explains what those levels mean. The
dealbreaker "requires a professional qualification the candidate does not hold"
was also being read as covering education levels; it now names licensed
professions explicitly (nursing/BIG, medical, legal bar), which is what it
always meant.

Prompt version: assess-v3.

## Consequences

- Scores mean something specific: a 45 is "your familiar tier, under-qualified",
  not a vague middling number.
- Python vacancies fell from 85-92 to ~45, and every vacancy now carries a visa
  flag. The previous ranking was confidently wrong.
- Grading a vacancy is cheap to redo (~5s) because translations are keyed
  separately and reused; changing the profile does not re-run the GPU on
  translation.
- Assessments are keyed by `(job_id, profile_name)`, so re-grading under `ahmed`
  left the earlier `default` rows intact and the two can be compared directly.
- The tier matcher is keyword-based and therefore literal: a vacancy that
  describes Laravel work without naming it will be under-tiered. The profile's
  skill list is the lever - adding synonyms improves matching.
- `LLM_DEFAULT_PROFILE` selects the active profile, so a second profile (a
  different target role) is a file plus a re-grade.
