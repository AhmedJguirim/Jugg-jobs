# 0011 - Pause the GPU queue when the card runs hot

Status: Accepted - 2026-10-06

## Context

Enrichment runs back to back for hours on a laptop GPU (RTX 3070 Ti Laptop).
The user asked for a check between vacancies: above 80°C, wait three minutes
before starting the next one.

## Decision

- **Where:** inside `enrich_job_workflow`, after `prepare_text` has decided the
  vacancy needs the GPU and before the first model call. That workflow holds
  the single `llm` slot, so pausing there holds the whole queue - every path
  (single scrape, batch, CLI) goes through it.
- **How:** a step reads `nvidia-smi` (ships with the driver; no new package)
  together with the limits, then the workflow `DBOS.sleep`s. The sleep is
  durable: a restart mid-pause resumes the remaining time. The limits travel in
  the step's checkpoint so a recovered workflow replays what it ran with.
- **Re-check after each pause**, and give up after `GPU_MAX_COOLDOWNS` (5) so a
  stuck or misreporting sensor cannot stall the queue forever.
- **Strictly above** the limit (81°C pauses, 80°C does not). An unreadable
  sensor never counts as hot: the cooldown is a courtesy, not a gate.
- Vacancies that are already current skip the check - they cost no heat.
- The pause shows in the batch progress line ("vacancy 3/8 - GPU at 84°C -
  cooling down for 3 min") via the existing `stage` event.

Settings: `GPU_MAX_TEMP_C=80`, `GPU_COOLDOWN_SECONDS=180`,
`GPU_MAX_COOLDOWNS=5`.

## Consequences

- New steps in `enrich_job_workflow`, so `APPLICATION_VERSION` went to 0.4.0
  (with the usual wedge risk for work left PENDING at 0.3.0).
- NVIDIA only. On other hardware the reading is None and nothing pauses.
- The check is between vacancies, not during one: a long translation can still
  run hot until it finishes.
