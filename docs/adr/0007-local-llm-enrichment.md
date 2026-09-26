# 0007 - Local LLM enrichment on a single GPU

Status: Accepted - 2026-09-26

## Context

Most ingested vacancies are in Dutch (65 of 68 measured), which the user does
not read. They should be translated to English and graded for fit, using a local
model (`qwen3.5:4b` via Ollama) rather than a hosted API.

The hard constraint is hardware: **one GPU**. Running two model instances at
once risks exhausting VRAM and, in the user's words, "toasting" the card. The
user's prior solution was a Laravel Horizon queue pinned to a single worker.

Measurements taken before designing anything (see the probes in the session
transcript):

| Finding | Value | Consequence |
|---|---|---|
| Model throughput | 92 tok/s output, 555 tok/s prompt | ~16s per average vacancy |
| Thinking mode (on by default) | 4547 tokens / 51s vs 163 / 29s, same quality | disable it |
| Model defaults | `temperature=1`, `presence_penalty=1.5` | wrong for translation; override |
| Descriptions | avg 4.3k chars, max 10.1k cleaned | chunk, do not truncate |
| HTML tags present | only `p, ul, ol, li, b, i` | a focused parser beats a dependency |
| Entity encoding | **61 of 68 double-encoded** (`&amp;eacute;`) | unescape repeatedly |
| Language mix | 65 Dutch, 3 English | detect and skip; cheapest saving available |

## Decision

### Serialise every model call on a DBOS queue

All LLM work is enqueued on the `llm` queue, registered with:

    worker_concurrency=1   # one job inside this process
    global_concurrency=1   # one job across ALL processes

`global_concurrency` is the reason this is better than a single Horizon worker:
it is enforced in the system database, so a second app instance, a stray CLI
run, or a forgotten background process still cannot put two jobs on the GPU.
`worker_concurrency` alone would only bind one process.

Page fetching is network-bound and goes on a separate `job-ingest` queue with
`worker_concurrency=3`, so scraping is parallel while inference stays serial.

Verified, not assumed: enqueueing six jobs simultaneously produced a peak
observed concurrency of 1 and a strictly sequential timeline.

`register_queue` must be called **after** `DBOS.launch()` - it writes the
concurrency limits to the system database and raises if called earlier.

### One workflow per vacancy, checkpointed

`enrich_job_workflow` runs four steps: `prepare_text` (no GPU), `translate_step`
(GPU), `assess_step` (GPU), `persist_enrichment` (no GPU). At ~16s of GPU per
vacancy, a 68-vacancy run is ~20 minutes, so the run must survive interruption.
Because each step is checkpointed, a crash resumes at the step it reached rather
than re-spending GPU time, and one failing vacancy does not sink the batch.

Persisting is a separate step from inference so a database hiccup re-runs the
*write*, not the model.

### Sanitise before prompting

`app/text/sanitize.py` strips markup, decodes entities repeatedly, drops short
repeated boilerplate lines, and normalises exotic whitespace - 11% fewer
characters (~8.5k tokens) across the corpus, with zero markup or entities
surviving. It also detects language, so English vacancies never reach the model.

Long text is **chunked, never truncated**: a 4B model handed 10k characters
starts summarising instead of translating.

### Store derived data in its own tables

`job_translations` and `job_assessments` are separate from `jobs`, each carrying
`source_hash`, `model` and `prompt_version`. This gives:

- **re-ingest safety**: updating a vacancy never destroys GPU work;
- **an off switch**: if the cleaned text's hash is unchanged, nothing re-runs;
- **cheap prompt iteration**: a new `prompt_version` re-runs only older rows;
- **room to grow**: a second language or scoring profile is a row, not a migration.

### Separate role fit from eligibility

`fit_score` rates the *work only*. Eligibility barriers live in `blocked`,
`dutch_required` and `red_flags`.

This came out of testing. The first prompt scored a Python backend role at
**20/100 "irrelevant"** purely because it required Dutch - collapsing it to the
same score as a nursing vacancy and destroying the ranking's usefulness. After
the split, the same vacancy scores **85 "strong_match" + blocked=true**: "a great
job you cannot take", which is a different and more actionable fact than "a bad
job". The shortlist hides blocked rows by default and can show them on request.

### Prompts are versioned constants written for a small model

In `app/llm/prompts.py`: one task per call, output format stated as a hard rule,
and the specific observed failure modes forbidden explicitly (adding preambles,
answering the vacancy's rhetorical questions, translating technology names,
summarising). The assessment call additionally uses Ollama's `format="json"`, so
valid JSON comes from constrained decoding rather than the model's goodwill, and
every field is coerced into range before it reaches the database.

### The scoring profile is data

`profiles/<name>.json`, rendered into the prompt as labelled lines (a 4B echoes
raw JSON back). The profile name is stored on every assessment so scores from
different profiles are never compared by accident.

## Consequences

- The GPU runs exactly one model at a time, enforced by the database rather than
  by convention.
- A long enrichment run is interruptible and resumable, and costs no GPU time
  for work already done.
- `app/integrations/ollama/client.py` is the only code that occupies the GPU, so
  the serialisation guarantee has one place to hold. Calling it outside a queued
  workflow bypasses that - scripts therefore enqueue rather than call.
- Translation quality depends on the sanitiser. If a new job board introduces
  tags outside the six seen here, the parser handles them generically but should
  be re-checked.
- **`profiles/default.json` was seeded by inference, not by asking.** It was
  derived from this project's own stack and the stated fact that the user does
  not read Dutch. Every `fit_score` is only as good as that file, which is why
  the profile carries a comment saying so.
- The 4B model is the weakest link in quality. The prompts, JSON constraints and
  field coercion contain that, but a larger model would need only a settings
  change - the queue keeps it safe either way.
