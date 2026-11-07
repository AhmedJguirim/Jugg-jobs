"""The single GPU slot: the `llm` queue, the temperature cooldown, the model check.

**The GPU rule.** There is one GPU and one model. Every model call is enqueued on
the `llm` queue, registered with:

    global_concurrency=1   # at most one LLM job across every process
    worker_concurrency=1   # at most one LLM job inside this process

`global_concurrency` is enforced in the DBOS system database, so starting a
second copy of the app (or a stray CLI run against the same system database)
still cannot put two jobs on the GPU. `worker_concurrency` alone would only bind
one process.

Fetching pages is network-bound and runs on its own queue
(`app/workflows/listing_fetch.py`), so it never waits on grading.
"""

from __future__ import annotations

from dbos import DBOS

from app.config import get_settings
from app.integrations.gpu import gpu_temperature
from app.logging_setup import log

# The DBOS application version, shared by every process that runs these
# workflows (main.py, scripts/process_listing.py). Bump it whenever a workflow's
# step sequence changes: DBOS replays a recovered workflow against the current
# code and aborts with DBOSUnexpectedStepError when the recorded steps no longer
# line up. A `DBOS.set_event` counts as a step.
#
# 0.5.0: the listing workflow gained mark_step and a known grading-step id (ADR-0016).
APPLICATION_VERSION = "0.5.0"

LLM_QUEUE = "llm"

# One at a time. Do not raise this without a second GPU.
LLM_CONCURRENCY = 1

# Workflow event the UI polls to say which stage a GPU job is on, including the
# cooldown message while the card is hot.
STAGE_EVENT = "stage"

_queues_registered = False


def register_queues() -> None:
    """Register the `llm` queue. Call once, AFTER `DBOS.launch()`.

    Ordering matters and is not obvious: `register_queue` writes the concurrency
    limits to the DBOS system database, which raises "System database accessed
    before DBOS was launched" if called earlier. Call it immediately after
    launch and before enqueueing anything, so the limits bind every job.

    Idempotent, so an accidental second call (tests, reload) is harmless.
    """
    global _queues_registered
    if _queues_registered:
        return

    DBOS.register_queue(
        LLM_QUEUE,
        # Both limits are set deliberately: worker_concurrency protects this
        # process, global_concurrency protects the GPU from a second process.
        worker_concurrency=LLM_CONCURRENCY,
        global_concurrency=LLM_CONCURRENCY,
        on_conflict="always_update",
    )
    _queues_registered = True
    log.info("queue registered: %s (concurrency=%s, GPU-serialised)", LLM_QUEUE, LLM_CONCURRENCY)


@DBOS.step()
def read_gpu_temperature() -> dict:
    """Read the GPU temperature and the cooldown settings, as one checkpoint.

    The settings travel with the reading so a recovered workflow replays the
    limits it actually ran with, not whatever .env says after a restart.
    """
    settings = get_settings()
    temperature = gpu_temperature()
    return {
        "temperature": temperature,
        "hot": temperature is not None and temperature > settings.gpu_max_temp_c,
        "max_temp": settings.gpu_max_temp_c,
        "cooldown_seconds": settings.gpu_cooldown_seconds,
        "max_cooldowns": settings.gpu_max_cooldowns,
    }


def cool_down_gpu() -> int:
    """Pause while the GPU is above its temperature limit. Returns pauses taken.

    Called at the start of a workflow that holds the single `llm` slot, so the
    pause holds the whole queue: the next listing cannot start until this one has
    cooled down and finished. `DBOS.sleep` is durable - a restart mid-pause
    resumes the remaining time rather than starting over.

    Re-reads after each pause, and gives up after `max_cooldowns` so a stuck
    sensor cannot stall the queue for good. Without `nvidia-smi` the reading is
    None, never hot, and this returns at once.
    """
    reading = read_gpu_temperature()
    pauses = 0
    while reading["hot"] and pauses < reading["max_cooldowns"]:
        seconds = reading["cooldown_seconds"]
        wait = f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"
        message = f"GPU at {reading['temperature']}°C - cooling down for {wait}"
        log.info("%s (limit %s°C)", message, reading["max_temp"])
        DBOS.set_event(STAGE_EVENT, message)
        DBOS.sleep(reading["cooldown_seconds"])
        pauses += 1
        reading = read_gpu_temperature()

    if reading["hot"]:
        log.warning(
            "GPU still at %s°C after %s pause(s); carrying on",
            reading["temperature"],
            pauses,
        )
    return pauses


@DBOS.step()
def llm_available() -> dict:
    """Is the model server up, with the configured model installed?

    Checked before a listing is queued for the GPU. Without this, an Ollama that
    is simply not running turns into one failed workflow per listing, each
    burning its retries first, for what is really one fact - the server is down.
    """
    from app.integrations.ollama import health

    ok, message = health()
    if not ok:
        log.warning("skipping grading: %s", message)
    return {"ok": ok, "message": message}
