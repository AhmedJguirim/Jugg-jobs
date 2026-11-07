"""Prompts for grading a job posting from anywhere in the world.

A posting can need Mandarin, a particular citizenship, a security clearance, a
time zone, or hiring only in three named countries. The prompt asks the model to
*report* those facts, each with a quote from the advert; whether they bar the
candidate is decided in code (`global_grader.py`).

The discipline: one job per call, a hard output format, the failure modes seen
in practice forbidden by name, and a verbatim quote for every restriction,
because a 4B model otherwise infers restrictions from how a job "feels". The
code then checks the quote.

**Bump GLOBAL_ASSESSMENT_PROMPT_VERSION when you change a prompt**: it is what
marks existing grades stale.
"""

from __future__ import annotations

GLOBAL_ASSESSMENT_PROMPT_VERSION = "assess-global-v1"

GLOBAL_SCHEMA_DESCRIPTION = """\
{
  "fit_score": <integer 0-100>,
  "verdict": "<one of: strong_match, possible, weak, irrelevant>",
  "seniority": "<one of: intern, junior, mid, senior, lead, unknown>",
  "seniority_fit": "<one of: under_qualified, good, over_qualified, unknown>",
  "remote": "<one of: onsite, hybrid, remote, unknown>",
  "remote_scope": "<one of: worldwide, countries, region, unknown>",
  "remote_places": ["<country or region the advert limits the role to, e.g. 'Poland', 'EU', 'UK'>"],
  "location_evidence": "<the exact sentence or phrase that limits where the person must be, or an empty string>",
  "languages_required": [
    {"language": "<language name in English>", "level": "<one of: native, fluent, working, basic, preferred>", "evidence": "<the exact sentence from the advert>"}
  ],
  "citizenship_required": <true or false>,
  "citizenship_nationalities": ["<nationality or country the advert demands, e.g. 'Russian', 'US'>"],
  "citizenship_evidence": "<the exact sentence from the advert, or an empty string>",
  "security_clearance_required": <true or false>,
  "clearance_evidence": "<the exact sentence from the advert, or an empty string>",
  "sponsorship": "<one of: offered, not_offered, unknown>",
  "requires_existing_permit": <true or false>,
  "permit_evidence": "<the exact sentence from the advert, or an empty string>",
  "other_restrictions": ["<any other hard condition, e.g. 'must work US Pacific hours', 'must hold a driving licence'>"],
  "summary": "<2-3 sentence plain-English summary of the role>",
  "matched_requirements": ["<requirement the vacancy asks for that the profile has>"],
  "missing_requirements": ["<requirement the vacancy asks for that the profile lacks>"],
  "red_flags": ["<concrete concern, e.g. 'needs 8+ years', 'requires Mandarin'>"],
  "reasoning": "<1-2 sentences explaining the score>"
}\
"""

GLOBAL_ASSESSMENT_SYSTEM = """\
You are a precise recruitment analyst. You compare one job vacancy against one \
candidate profile and return a structured assessment. The vacancy can be from \
any company in any country and is written in English.

You must return a single JSON object matching this shape exactly:
{schema}

## How to score

"fit_score" measures ONE thing: how well THE WORK matches the candidate's \
skills and target roles. Score the job, not whether the candidate can take it. \
A perfect-stack role the candidate cannot take still scores high; whether it is \
out of reach is reported separately in the fields below.

The vacancy's technology tier is already decided for you in the user message \
and gives the score range. Use the range to express how good a match it is: \
higher when the advert wants the candidate's particular strengths, lower when \
the seniority is a poor fit.

Set "seniority_fit" by comparing what the vacancy demands with the candidate's \
years of experience: "under_qualified" when it wants clearly more ("senior", \
"lead", "5+ years") - and add a red flag naming the gap; "good" when the level \
matches; "over_qualified" when it is clearly more junior than the candidate.

## Restrictions - report them, never let them change fit_score

Report only what the advert actually says. For every restriction below you MUST \
copy the exact sentence or phrase from the advert into the matching evidence \
field. If you cannot copy one, the advert did not say it: leave the field \
empty / false / "unknown". Never infer a restriction from the company, the \
country, or how the job feels.

- "remote": where the work is done. "unknown" when the advert does not say.
- "remote_scope" and "remote_places": for a remote role, is it open to \
applicants anywhere ("worldwide"), only in named countries ("countries"), or \
only in a named region such as the EU ("region")? List the places. Phrases like \
"remote within Poland", "EU remote", "we can only hire in the UK and select EU \
countries" are restrictions; copy them into "location_evidence". A role titled \
or labelled "Remote" with no place named is "unknown", not "worldwide".
- "languages_required": every language, other than English, that the advert \
says the person must speak, with the level it asks for. "fluent" for fluent, \
native, business-level or professional proficiency; "working" for working \
knowledge or conversational; "basic" for basic; "preferred" for nice-to-have \
or an advantage. English alone is not listed. A role merely being written in \
English, or the company being based somewhere, is NOT a language requirement.
- "citizenship_required": true only when the advert says applicants must be a \
citizen or national of a named country (for example "must be a US citizen", \
"Russian citizenship required"). Living in, or having the right to work in, a \
country is a PERMIT matter, not citizenship.
- "security_clearance_required": true only when the advert requires holding or \
obtaining a government security clearance, and quote it in \
"clearance_evidence".
- "sponsorship": "offered" when the advert offers visa sponsorship or \
relocation support, "not_offered" when it says it does not sponsor, otherwise \
"unknown".
- "requires_existing_permit": true ONLY when the advert says outright that \
applicants must already have the right to work in a named country (or already \
live there), and you can quote it in "permit_evidence". A role simply being \
on site does not qualify.
- "other_restrictions": other hard conditions the advert states (working hours \
tied to a time zone, a required licence, travel, a professional registration).

## Rules

1. Judge ONLY on what the vacancy text says. Never invent requirements.
2. "matched_requirements" and "missing_requirements" must quote or closely \
paraphrase things the vacancy actually asks for. Keep each item under 12 words.
3. Leave an array empty rather than padding it with vague entries.
4. Add a short entry to "red_flags" for every restriction you report above, \
and for anything else a candidate should know before applying.
5. Only list a "not interested in" topic as a red flag if the vacancy is \
actually about that topic, not because it mentions it in passing.
6. Output the JSON object only. No markdown, no code fences, no commentary.\
"""


def global_assessment_messages(
    *,
    title: str,
    company: str | None,
    facts: dict[str, str],
    description: str,
    profile_text: str,
    stack_match: str,
    matched_skills: list[str],
    band: tuple[int, int],
) -> list[dict[str, str]]:
    """The message list for one assessment call.

    `facts` are the labelled details the platform shows beside the description
    (Location, Employment Type, Compensation...); they are passed as lines, not
    prose, which helps a small model keep them straight.
    """
    skills_line = ", ".join(matched_skills) or "none of the candidate's listed skills"
    fact_lines = "\n".join(f"{label}: {value}" for label, value in facts.items() if value) or "none"
    user = f"""\
CANDIDATE PROFILE
{profile_text}

STACK TIER (already determined - do not re-decide it)
This vacancy's stack falls in the candidate's "{stack_match}" tier, matching: {skills_line}.
Therefore "fit_score" MUST be between {band[0]} and {band[1]}.

VACANCY
Title: {title}
Company: {company or "unknown"}
{fact_lines}

Vacancy text:
<text>
{description}
</text>

Return the JSON assessment now."""
    return [
        {"role": "system", "content": GLOBAL_ASSESSMENT_SYSTEM.format(schema=GLOBAL_SCHEMA_DESCRIPTION)},
        {"role": "user", "content": user},
    ]


# Sent as a follow-up when the first reply will not parse as the expected object.
JSON_REPAIR_SYSTEM = """\
You fix malformed JSON. The user sends text that was supposed to be a single \
JSON object. Return only that JSON object, corrected and valid. Do not add \
fields, do not explain, do not wrap it in code fences.\
"""


def json_repair_messages(broken: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": JSON_REPAIR_SYSTEM},
        {"role": "user", "content": broken},
    ]
