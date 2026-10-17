"""Durable workflows for scraping and LLM enrichment.

**The GPU rule.** There is one GPU and one model. All LLM work is enqueued on
the `llm` queue, registered with:

    global_concurrency=1   # at most one LLM job across every process
    worker_concurrency=1   # at most one LLM job inside this process

This is the equivalent of a Laravel Horizon queue pinned to a single worker,
with one difference worth having: `global_concurrency` is enforced in the
database, so starting a second copy of the app (or a stray CLI run) still cannot
put two jobs on the GPU. `worker_concurrency` alone would only bind one process.

Page fetching is network-bound and safe to parallelise, so it gets its own queue
with a normal concurrency. Batch company scrapes get a third queue, so a batch
waiting on the GPU never holds the page-fetch slots.

**Why a workflow per vacancy.** Each vacancy is one queued child workflow whose
steps are checkpointed. A crash, a Ctrl-C, or an Ollama restart mid-run resumes
at the step it reached instead of re-spending GPU time on work already done, and
a single failing vacancy does not take the run down with it.
"""

from __future__ import annotations

from typing import Any, Optional

from dbos import DBOS, SetWorkflowID

from app.config import get_settings
from app.db.models.job import SOURCE_NVB
from app.db.session import session_scope
from app.integrations.gpu import gpu_temperature
from app.integrations.nvb import fetch_jobs_page, to_job_creates
from app.llm import assess_job, load_profile, translate_text, translate_title
from app.llm.service import AssessmentUnparseable
from app.llm.prompts import ASSESSMENT_PROMPT_VERSION, TRANSLATION_PROMPT_VERSION
from app.logging_setup import log
from app.repositories import enrichment as enrichment_repo
from app.repositories import job as job_repo
from app.services.company_scrape import NoSearchQuery, query_for, record_scrape
from app.text import sanitize_description

# The DBOS application version, shared by every process that runs these
# workflows (main.py, scripts/enrich_jobs.py). Bump it whenever a workflow's
# step sequence changes: DBOS replays a recovered workflow against the current
# code and aborts with DBOSUnexpectedStepError when the recorded steps no longer
# line up. One constant, because two copies drifted: the script's
# `--clear-stuck` treats every other version as stranded, so a stale copy there
# would cancel healthy work.
#
# 0.3.0: progress events (`set_event`) in the company scrape and enrichment.
# 0.4.0: GPU temperature check and cooldown sleep before each vacancy's GPU work.
# 0.5.0: listing workflow gained mark_step and a known grading-step id (ADR-0016).
APPLICATION_VERSION = "0.5.0"

# Queue names. The GPU queue's concurrency is the safety property of this module.
LLM_QUEUE = "llm"
INGEST_QUEUE = "job-ingest"
COMPANY_QUEUE = "company-scrape"

# One at a time. Do not raise these without a second GPU.
LLM_CONCURRENCY = 1
INGEST_WORKER_CONCURRENCY = 3

# Companies in a batch scraped at once. Two, not more: the GPU is the
# bottleneck, so a second company only needs to be fetched and queued while the
# first is being graded. More would just hit the job board harder for nothing.
COMPANY_SCRAPE_CONCURRENCY = 2

# Ceiling on one batch. Each company can queue up to MAX_ENRICHED_PER_SCRAPE
# vacancies for the GPU, so 50 companies is already a very long night.
MAX_COMPANIES_PER_BATCH = 50

# Workflow events the UI polls for progress. `progress` is set by a company
# scrape as it moves through its phases; `stage` by one vacancy's enrichment,
# saying which GPU step it is on.
PROGRESS_EVENT = "progress"
STAGE_EVENT = "stage"

# Hard ceiling on how many result pages one scrape will walk.
#
# The board's search is full-text over the whole vacancy, so an employer whose
# name contains a common word matches half the site: "7 Solutions" returns 287
# vacancies across 12 pages, almost none of them that employer's. The cap bounds
# the damage in time, bandwidth and GPU. A caller may ask for fewer pages; it
# cannot ask for more.
MAX_PAGES_PER_SCRAPE = 20

# Ceiling on how many *new* vacancies one scrape will add.
#
# This is the cap that matters, because a new vacancy is what costs GPU time:
# already-known ones are re-seen constantly across searches and cost nothing,
# since their content hash is unchanged and enrichment skips them. Counting
# pages bounds the crawl; counting new vacancies bounds the bill.
MAX_NEW_VACANCIES_PER_SCRAPE = 50

# Ceiling on how many vacancies one scrape will queue for the GPU.
#
# Separate from the storage cap, and learned the hard way: re-scraping an
# employer whose vacancies are all already stored adds nothing new, so the
# storage cap never fires - but the scrape still queued every vacancy it saw for
# enrichment. On a 287-result query where most were not yet translated, that is
# roughly 95 minutes of GPU from one click.
MAX_ENRICHED_PER_SCRAPE = 50

_queues_registered = False


def register_queues() -> None:
    """Register the queues. Call once, AFTER `DBOS.launch()`.

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
    DBOS.register_queue(
        INGEST_QUEUE,
        worker_concurrency=INGEST_WORKER_CONCURRENCY,
        on_conflict="always_update",
    )
    DBOS.register_queue(
        COMPANY_QUEUE,
        worker_concurrency=COMPANY_SCRAPE_CONCURRENCY,
        on_conflict="always_update",
    )
    _queues_registered = True
    log.info(
        "queues registered: %s (concurrency=%s, GPU-serialised), %s (worker_concurrency=%s)",
        LLM_QUEUE,
        LLM_CONCURRENCY,
        INGEST_QUEUE,
        INGEST_WORKER_CONCURRENCY,
    )


# ---------------------------------------------------------------------------
# Steps: ingest
# ---------------------------------------------------------------------------


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=2)
def fetch_page(query: str, page: int, limit: int, sort: str = "date") -> dict:
    """Fetch one search-result page. Retried: the network is the flaky part."""
    return fetch_jobs_page(query, page=page, limit=limit, sort=sort)


@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=1)
def store_page(payload: dict, max_new: Optional[int] = None) -> dict:
    """Persist one page of vacancies, with their companies and locations.

    `max_new` stops partway through the page once that many *new* vacancies
    have been added, so a cap of 50 means 50 rather than "50 rounded up to
    whatever the page size happens to be". Vacancies left unstored are simply
    picked up by the next scrape, since ingest is keyed on (source,
    external_id).

    Retried: pages are stored concurrently, and PostgreSQL resolves a lock
    cycle between two of them by aborting one with a deadlock error. That is
    transient by nature - the retry re-runs the page and finds the rows the
    other transaction committed. Observed during a four-page company scrape.
    """
    created = updated = skipped = 0
    job_ids: list[int] = []
    with session_scope() as session:
        for job_payload in to_job_creates(payload):
            if max_new is not None and created >= max_new:
                skipped += 1
                continue
            job, was_created = job_repo.upsert(session, job_payload)
            job_ids.append(job.id)
            created += was_created
            updated += not was_created

    log.info(
        "stored page: %s created, %s updated%s",
        created,
        updated,
        f", {skipped} left for a later scrape" if skipped else "",
    )
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "job_ids": job_ids,
    }


# ---------------------------------------------------------------------------
# Steps: enrichment. Only the two model steps touch the GPU.
# ---------------------------------------------------------------------------


@DBOS.step()
def prepare_text(job_id: int, target_language: str, profile_name: str) -> dict:
    """Load a vacancy, clean its HTML, detect language, decide what is needed.

    Deliberately cheap and GPU-free: the decision to skip is made here so the
    expensive steps are never entered for work that is already current.
    """
    with session_scope() as session:
        job = job_repo.get(session, job_id)
        if job is None:
            return {"job_id": job_id, "found": False}

        cleaned = sanitize_description(job.description)
        translation = enrichment_repo.get_translation(session, job_id, target_language)
        assessment = enrichment_repo.get_assessment(session, job_id, profile_name)

        return {
            "job_id": job_id,
            "found": True,
            "source": job.source,
            "title": job.title,
            "company": job.company.name if job.company else None,
            "location": job.city,
            "salary": job.salary_range,
            # contract type and education level are normalised into association
            # tables (ADR-0006), so they are read from the relationships rather
            # than from columns that do not exist on `jobs`.
            "contract": ", ".join(term.name for term in job.contract_types) or None,
            "education": ", ".join(term.name for term in job.education_levels) or None,
            "text": cleaned.text,
            "language": cleaned.language,
            "source_hash": cleaned.content_hash,
            "clean_chars": cleaned.clean_chars,
            "needs_translation": not enrichment_repo.translation_is_current(
                translation,
                source_hash=cleaned.content_hash,
                prompt_version=TRANSLATION_PROMPT_VERSION,
            ),
            "needs_assessment": not enrichment_repo.assessment_is_current(
                assessment,
                source_hash=cleaned.content_hash,
                prompt_version=ASSESSMENT_PROMPT_VERSION,
            ),
            # Reuse a current translation as the assessment input.
            "existing_translation": translation.description if translation else None,
        }


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

    Called inside `enrich_job_workflow`, which holds the single `llm` slot, so
    the pause holds the whole queue: the next vacancy cannot start until this
    one has cooled down and finished. `DBOS.sleep` is durable - a restart
    mid-pause resumes the remaining time rather than starting over.

    Re-reads after each pause, and gives up after `max_cooldowns` so a stuck
    sensor cannot stall the queue for good.
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
def translate_step(prepared: dict, target_language: str) -> dict:
    """GPU step: translate one vacancy. Runs only on the `llm` queue."""
    result = translate_text(prepared["text"], source_language=prepared["language"])
    title = translate_title(prepared["title"], source_language=prepared["language"])

    return {
        "title": title,
        "description": result.text,
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
        "model": result.model,
        "chunks": result.chunks,
        "prompt_tokens": result.prompt_tokens,
        "output_tokens": result.output_tokens,
        "duration_ms": int(result.duration_s * 1000),
        "source_language": prepared["language"],
        "target_language": target_language,
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "source_hash": prepared["source_hash"],
    }


@DBOS.step()
def assess_step(prepared: dict, english_text: str, profile_name: str) -> dict:
    """GPU step: score one vacancy against a profile. Runs only on the `llm` queue.

    Scored on the English text when available - the model reasons better about
    the profile in the same language the profile is written in.
    """
    profile = load_profile(profile_name)
    try:
        result = assess_job(
            title=prepared["title"],
            description=english_text or prepared["text"],
            profile=profile,
            company=prepared["company"],
            location=prepared["location"],
            salary=prepared["salary"],
            contract=prepared["contract"],
            education=prepared["education"],
        )
    except AssessmentUnparseable as error:
        # About one reply in two hundred is unreadable even after a repair and
        # a salvage attempt. Losing the whole vacancy over it would also throw
        # away the translation, which is the expensive half. Leave it ungraded:
        # it stays pending and the next run tries again, and because the model
        # is sampled the retry usually succeeds.
        log.warning("job %s: could not read an assessment from the model (%s)", prepared["job_id"], error)
        return {}
    return {
        "fit_score": result.fit_score,
        "verdict": result.verdict,
        "seniority": result.seniority,
        "seniority_fit": result.seniority_fit,
        "stack_match": result.stack_match,
        "remote": result.remote,
        "dutch_required": result.dutch_required,
        "sponsorship_required": result.sponsorship_required,
        "eligibility": result.eligibility,
        "location_policy": result.location_policy,
        "requires_existing_permit": result.requires_existing_permit,
        "offers_sponsorship": result.offers_sponsorship,
        "blocked": result.blocked,
        "summary": result.summary,
        "reasoning": result.reasoning,
        "matched_requirements": result.matched_requirements,
        "missing_requirements": result.missing_requirements,
        "red_flags": result.red_flags,
        "model": result.model,
        "repaired": result.repaired,
        "prompt_tokens": result.prompt_tokens,
        "output_tokens": result.output_tokens,
        "duration_ms": int(result.duration_s * 1000),
        "prompt_version": ASSESSMENT_PROMPT_VERSION,
        "profile_name": profile_name,
        "source_hash": prepared["source_hash"],
    }


@DBOS.step()
def persist_translation(job_id: int, translation: dict) -> dict:
    """Store a translation as soon as it exists.

    Written before grading starts, deliberately. Grading can fail (a small model
    can emit unparseable JSON), and translating is the expensive half - roughly
    15s of GPU against 5s. Persisting here means a grading failure costs only the
    grading, and the next run skips straight to it because the translation is
    already current.
    """
    return persist_enrichment(job_id, translation, None)


@DBOS.step()
def persist_enrichment(
    job_id: int,
    translation: Optional[dict],
    assessment: Optional[dict],
) -> dict:
    """Write whatever the model produced. Separate step so a GPU result is
    never lost to a database hiccup - a retry re-runs the write, not the model."""
    with session_scope() as session:
        if translation:
            enrichment_repo.upsert_translation(
                session,
                job_id=job_id,
                target_language=translation["target_language"],
                source_language=translation["source_language"],
                title=translation["title"],
                description=translation["description"],
                source_hash=translation["source_hash"],
                skipped=translation["skipped"],
                skip_reason=translation["skip_reason"],
                model=translation["model"],
                prompt_version=translation["prompt_version"],
                chunks=translation["chunks"],
                prompt_tokens=translation["prompt_tokens"],
                output_tokens=translation["output_tokens"],
                duration_ms=translation["duration_ms"],
            )
        if assessment:
            enrichment_repo.upsert_assessment(
                session,
                job_id=job_id,
                profile_name=assessment["profile_name"],
                source_hash=assessment["source_hash"],
                prompt_version=assessment["prompt_version"],
                fit_score=assessment["fit_score"],
                verdict=assessment["verdict"],
                seniority=assessment["seniority"],
                seniority_fit=assessment.get("seniority_fit"),
                stack_match=assessment.get("stack_match"),
                remote=assessment["remote"],
                dutch_required=assessment["dutch_required"],
                sponsorship_required=assessment.get("sponsorship_required", False),
                eligibility=assessment.get("eligibility"),
                location_policy=assessment.get("location_policy"),
                requires_existing_permit=assessment.get("requires_existing_permit", False),
                offers_sponsorship=assessment.get("offers_sponsorship", False),
                blocked=assessment["blocked"],
                summary=assessment["summary"],
                reasoning=assessment["reasoning"],
                matched_requirements=assessment["matched_requirements"],
                missing_requirements=assessment["missing_requirements"],
                red_flags=assessment["red_flags"],
                details=assessment,
                model=assessment["model"],
                repaired=assessment["repaired"],
                prompt_tokens=assessment["prompt_tokens"],
                output_tokens=assessment["output_tokens"],
                duration_ms=assessment["duration_ms"],
            )
    return {"job_id": job_id, "translated": bool(translation), "assessed": bool(assessment)}


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@DBOS.workflow()
def enrich_job_workflow(
    job_id: int,
    target_language: str = "en",
    profile_name: str = "default",
    force: bool = False,
) -> dict:
    """Translate and grade one vacancy.

    Enqueue this on LLM_QUEUE - never call it directly - so that only one
    instance is ever on the GPU. Each step is checkpointed, so an interruption
    resumes rather than repeating the model calls already paid for.
    """
    prepared = prepare_text(job_id, target_language, profile_name)
    if not prepared["found"]:
        log.warning("enrich: job %s no longer exists", job_id)
        return {"job_id": job_id, "status": "missing"}
    # Only Nationale Vacaturebank vacancies belong to this Dutch pipeline. Jobs
    # from other sources are graded by their own workflow when they are
    # created; running them through here would overwrite that grade with a
    # Netherlands-specific one. `.get` because a recovered run may replay a
    # prepare_text result recorded before this key existed.
    if prepared.get("source", SOURCE_NVB) != SOURCE_NVB:
        log.info("enrich: job %s is not an NVB vacancy (%s), skipping", job_id, prepared["source"])
        return {"job_id": job_id, "status": "not_nvb"}
    if not prepared["text"]:
        log.info("enrich: job %s has no usable description", job_id)
        return {"job_id": job_id, "status": "no_description"}

    if force or prepared["needs_translation"] or prepared["needs_assessment"]:
        # Before the GPU is touched, and only when it is about to be: an
        # already-current vacancy costs no heat, so it does not wait.
        cool_down_gpu()

    translation: Optional[dict] = None
    if force or prepared["needs_translation"]:
        DBOS.set_event(STAGE_EVENT, "translating the description")
        translation = translate_step(prepared, target_language)
        # Banked immediately: see persist_translation's docstring.
        persist_translation(job_id, translation)
    english = (translation or {}).get("description") or prepared["existing_translation"]

    assessment: Optional[dict] = None
    assessment_failed = False
    if force or prepared["needs_assessment"]:
        DBOS.set_event(STAGE_EVENT, "grading against your profile")
        # An empty dict means the model's reply could not be read; the vacancy
        # stays ungraded rather than the workflow failing.
        assessment = assess_step(prepared, english or "", profile_name) or None
        assessment_failed = assessment is None

    if assessment_failed:
        # Not "up to date": it is ungraded, and saying so is what makes the
        # next run pick it up rather than reporting a clean pass.
        log.info("enrich: job %s left ungraded, the reply could not be read", job_id)
        return {
            "job_id": job_id,
            "status": "assessment_unreadable",
            "translated": translation is not None,
            "assessed": False,
            "gpu_ms": (translation or {}).get("duration_ms", 0),
        }

    if translation is None and assessment is None:
        log.info("enrich: job %s already current, GPU not used", job_id)
        return {"job_id": job_id, "status": "up_to_date"}

    if assessment is not None:
        persist_enrichment(job_id, None, assessment)

    gpu_ms = (translation or {}).get("duration_ms", 0) + (assessment or {}).get("duration_ms", 0)
    return {
        "job_id": job_id,
        "status": "enriched",
        "translated": translation is not None,
        "assessed": assessment is not None,
        "fit_score": (assessment or {}).get("fit_score"),
        "verdict": (assessment or {}).get("verdict"),
        "gpu_ms": gpu_ms,
    }


@DBOS.workflow()
def enrich_pending_workflow(
    limit: int = 50,
    target_language: str = "en",
    profile_name: str = "default",
    force: bool = False,
) -> dict:
    """Enqueue enrichment for every vacancy that needs it, then wait.

    The children land on the `llm` queue, so they run strictly one at a time
    however many are enqueued. Enqueueing is instant; the waiting happens as the
    queue drains, and the parent workflow is itself durable, so this survives a
    restart mid-run.
    """
    health = llm_available()
    if not health["ok"]:
        log.warning("enrichment skipped: %s", health["message"])
        return {
            "queued": 0,
            "enriched": 0,
            "up_to_date": 0,
            "failed": 0,
            "gpu_seconds": 0.0,
            "status": "llm_unavailable",
            "message": health["message"],
        }

    with session_scope() as session:
        needs_translation = enrichment_repo.pending_translation(
            session,
            target_language=target_language,
            prompt_version=TRANSLATION_PROMPT_VERSION,
            limit=limit,
        )
        needs_assessment = enrichment_repo.pending_assessment(
            session,
            profile_name=profile_name,
            prompt_version=ASSESSMENT_PROMPT_VERSION,
            limit=limit,
        )
        job_ids = sorted({job.id for job in needs_translation} | {job.id for job in needs_assessment})

    log.info("enrich: %s vacancy(ies) queued on the %s queue", len(job_ids), LLM_QUEUE)

    handles = [
        DBOS.enqueue_workflow(
            LLM_QUEUE, enrich_job_workflow, job_id, target_language, profile_name, force
        )
        for job_id in job_ids
    ]

    results = []
    cancelled = 0
    for index, handle in enumerate(handles):
        try:
            results.append(handle.get_result())
        except Exception as error:  # one bad vacancy must not sink the batch
            log.exception("enrich: a queued vacancy failed: %s", error)
            results.append({"status": "failed", "error": str(error)})

            # Same reasoning as the company scrape: check whether the server
            # is still there before letting the rest of the queue fail too.
            health = llm_available()
            if not health["ok"]:
                cancelled = _cancel_queued(handles, index + 1)
                log.warning(
                    "enrich: model server went away mid-run, cancelled %s "
                    "queued vacancy(ies) - %s",
                    cancelled,
                    health["message"],
                )
                break

    summary = {
        "queued": len(job_ids),
        "enriched": sum(1 for r in results if r.get("status") == "enriched"),
        "up_to_date": sum(1 for r in results if r.get("status") == "up_to_date"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "cancelled": cancelled,
        "gpu_seconds": round(sum(r.get("gpu_ms", 0) for r in results) / 1000, 1),
    }
    if cancelled:
        summary["status"] = "llm_unavailable"
    log.info("enrich done: %s", summary)
    return summary


@DBOS.workflow()
def scrape_and_enrich_workflow(
    query: str,
    limit: int = 50,
    max_pages: Optional[int] = None,
    target_language: str = "en",
    profile_name: str = "default",
    enrich: bool = True,
) -> dict:
    """The whole pipeline: walk the search pages, then translate and grade.

    Scraping is parallel (network-bound, on the ingest queue); enrichment is
    strictly serial (GPU-bound, on the llm queue). Both halves are durable, so
    this can be interrupted at any point and resumed.
    """
    log.info("pipeline start: query=%r limit=%s max_pages=%s", query, limit, max_pages)

    first = fetch_page(query, 1, limit)
    total_pages = int(first.get("pages") or 1)
    last_page = min(total_pages, max_pages) if max_pages else total_pages

    stored = store_page(first, MAX_NEW_VACANCIES_PER_SCRAPE)
    job_ids = list(stored["job_ids"])
    created, updated = stored["created"], stored["updated"]

    # Pages 2..n in parallel on the ingest queue.
    page_handles = [
        DBOS.enqueue_workflow(INGEST_QUEUE, ingest_page_workflow, query, page, limit)
        for page in range(2, last_page + 1)
    ]
    for handle in page_handles:
        result = handle.get_result()
        job_ids.extend(result["job_ids"])
        created += result["created"]
        updated += result["updated"]

    scrape_summary = {
        "query": query,
        "pages": last_page,
        "jobs_seen": len(job_ids),
        "created": created,
        "updated": updated,
    }
    log.info("pipeline scraped: %s", scrape_summary)

    if not enrich:
        return {"scrape": scrape_summary, "enrich": None}

    enrich_summary = enrich_pending_workflow(
        limit=max(len(job_ids), 1),
        target_language=target_language,
        profile_name=profile_name,
    )
    return {"scrape": scrape_summary, "enrich": enrich_summary}


@DBOS.workflow()
def ingest_page_workflow(query: str, page: int, limit: int) -> dict:
    """Fetch and store one page. Enqueued on the ingest queue."""
    return store_page(fetch_page(query, page, limit))


# ---------------------------------------------------------------------------
# Per-company scrape
# ---------------------------------------------------------------------------


@DBOS.step()
def company_search_query(company_id: int) -> dict:
    """Read the employer's own job-board search term.

    A step, so the query that was actually used is checkpointed: if the flow
    resumes after the seed link is edited, it continues with the term it
    started with rather than silently switching searches mid-run.
    """
    from app.db.models.company import Company

    with session_scope() as session:
        company = session.get(Company, company_id)
        if company is None:
            return {"found": False, "company_id": company_id}
        try:
            query = query_for(company)
        except NoSearchQuery as error:
            return {"found": True, "company_id": company_id, "query": None, "error": str(error)}
        return {
            "found": True,
            "company_id": company_id,
            "name": company.name,
            "query": query,
        }


def _report(phase: str, **fields: Any) -> None:
    """Publish where a company scrape has got to, for the batch progress view.

    A workflow event rather than a table: it is transient, per workflow, and
    DBOS already stores it durably next to the workflow it describes.
    """
    DBOS.set_event(PROGRESS_EVENT, {"phase": phase, **fields})


def _cancel_queued(handles, from_index: int) -> int:
    """Cancel the enrichment workflows that have not started yet.

    Called when the model server has gone away mid-run. Without this, every
    vacancy still on the queue takes its turn, spends three retries with
    backoff discovering the same thing, and fails - fifty vacancies of that is
    several minutes of nothing.
    """
    cancelled = 0
    for handle in handles[from_index:]:
        try:
            DBOS.cancel_workflow(handle.workflow_id, cancel_children=True)
            cancelled += 1
        except Exception:  # already finished, or gone
            continue
    return cancelled


@DBOS.step()
def llm_available() -> dict:
    """Is the model server up, with the configured model installed?

    Checked once before any enrichment is queued. Without this, an Ollama that
    is simply not running turns into one failed workflow per vacancy, each
    burning three retries with backoff first: a single scrape logged 43
    failures and a stack trace, for what is really one fact - the server is
    down. The vacancies are already stored by then, so the honest outcome is to
    skip grading and say why; the next run picks them up untouched.
    """
    from app.integrations.ollama import health

    ok, message = health()
    if not ok:
        log.warning("skipping enrichment: %s", message)
    return {"ok": ok, "message": message}


@DBOS.step()
def select_for_enrichment(
    job_ids: list[int],
    target_language: str,
    profile_name: str,
    limit: int,
) -> dict:
    """Narrow a scrape's vacancies to those that actually need the GPU.

    Two reasons this is a query rather than "enqueue them all and let each one
    work it out": a vacancy that is already current costs a workflow
    round-trip to discover that, and hundreds of those are slow even when none
    of them touch the model; and without a ceiling one click can commit the GPU
    for hours.

    Returns the ids to enrich, capped, plus what was left out.
    """
    from app.db.models.enrichment import JobAssessment, JobTranslation
    from app.db.models.job import Job
    from sqlalchemy import select

    if not job_ids:
        return {"ids": [], "needed": 0, "skipped_current": 0, "deferred": 0}

    with session_scope() as session:
        rows = session.execute(
            select(Job.id, JobTranslation.id, JobAssessment.id)
            .outerjoin(
                JobTranslation,
                (JobTranslation.job_id == Job.id)
                & (JobTranslation.target_language == target_language)
                & (JobTranslation.prompt_version == TRANSLATION_PROMPT_VERSION),
            )
            .outerjoin(
                JobAssessment,
                (JobAssessment.job_id == Job.id)
                & (JobAssessment.profile_name == profile_name)
                & (JobAssessment.prompt_version == ASSESSMENT_PROMPT_VERSION),
            )
            .where(
                Job.id.in_(job_ids),
                Job.description.is_not(None),
                Job.source == SOURCE_NVB,  # other sources are graded on creation (ADR-0015)
            )
        ).all()

    needing = [job_id for job_id, translation, assessment in rows if not (translation and assessment)]
    current = len(rows) - len(needing)

    return {
        "ids": needing[:limit],
        "needed": len(needing),
        "skipped_current": current,
        "deferred": max(0, len(needing) - limit),
    }


@DBOS.step()
def stamp_scrape(company_id: int, vacancies: int) -> None:
    """Record when this employer was last scraped, and what it yielded."""
    with session_scope() as session:
        record_scrape(session, company_id, vacancies=vacancies)


@DBOS.workflow()
def scrape_company_workflow(
    company_id: int,
    limit: int = 50,
    max_pages: Optional[int] = None,
    target_language: str = "en",
    profile_name: str = "default",
    enrich: bool = True,
) -> dict:
    """Scrape every page of one employer's search, then translate and grade.

    Same flow as a manual query ingest - only the query differs, and it comes
    from the employer's own Nationale Vacaturebank search link.

    GPU safety: the vacancies found here are enriched by enqueueing one child
    workflow each onto LLM_QUEUE, which allows a single job at a time across
    every process. Triggering this for ten employers at once queues the work;
    it does not run ten models.
    """
    resolved = company_search_query(company_id)
    if not resolved["found"]:
        return {"company_id": company_id, "status": "missing"}
    if not resolved.get("query"):
        return {"company_id": company_id, "status": "no_query", "error": resolved.get("error")}

    query = resolved["query"]
    log.info("company %s (%s): scraping query %r", company_id, resolved.get("name"), query)

    # --- scrape every page ---
    _report("fetching", page=1, pages=None, found=0)
    first = fetch_page(query, 1, limit)
    total_pages = int(first.get("pages") or 1)

    requested = min(total_pages, max_pages) if max_pages else total_pages
    last_page = min(requested, MAX_PAGES_PER_SCRAPE)
    truncated = last_page < total_pages
    if truncated:
        log.warning(
            "company %s: query %r has %s pages; stopping at %s. A generic "
            "employer name matches unrelated vacancies on a full-text search.",
            company_id,
            query,
            total_pages,
            last_page,
        )

    stored = store_page(first, MAX_NEW_VACANCIES_PER_SCRAPE)
    job_ids = list(stored["job_ids"])
    created, updated = stored["created"], stored["updated"]

    # Sequential, not parallel: the budget is checked between pages, and three
    # pages running at once could each add a full page of new vacancies before
    # any of them is counted. Page fetches are sub-second and capped at 20, so
    # the parallelism was worth little; stopping on the right page is worth
    # more.
    pages_walked = 1
    hit_vacancy_cap = created >= MAX_NEW_VACANCIES_PER_SCRAPE

    for page in range(2, last_page + 1):
        if hit_vacancy_cap:
            break
        remaining = MAX_NEW_VACANCIES_PER_SCRAPE - created
        _report("fetching", page=page, pages=last_page, found=len(job_ids))
        result = store_page(fetch_page(query, page, limit), remaining)
        job_ids.extend(result["job_ids"])
        created += result["created"]
        updated += result["updated"]
        pages_walked = page
        if created >= MAX_NEW_VACANCIES_PER_SCRAPE:
            hit_vacancy_cap = True
            log.warning(
                "company %s: query %r added %s new vacancies, stopping at the "
                "cap of %s after page %s of %s.",
                company_id,
                query,
                created,
                MAX_NEW_VACANCIES_PER_SCRAPE,
                page,
                total_pages,
            )

    # Stamped before enrichment: the scrape itself is done at this point, and a
    # later GPU failure should not make it look as though we never looked.
    stamp_scrape(company_id, len(job_ids))

    summary = {
        "company_id": company_id,
        "company": resolved.get("name"),
        "query": query,
        "status": "done",
        "pages": pages_walked,
        "pages_available": total_pages,
        # Surfaced so the UI can say the search was broader than we walked,
        # rather than implying the employer only has this many vacancies.
        "truncated": truncated or pages_walked < total_pages,
        "hit_vacancy_cap": hit_vacancy_cap,
        "vacancy_cap": MAX_NEW_VACANCIES_PER_SCRAPE,
        "vacancies": len(job_ids),
        "created": created,
        "updated": updated,
    }

    if not enrich or not job_ids:
        summary["enriched"] = 0
        return summary

    # --- enrich, strictly one model at a time ---
    _report("checking the model server", found=len(job_ids))
    health = llm_available()
    if not health["ok"]:
        summary["enrich_status"] = "llm_unavailable"
        summary["enrich_message"] = health["message"]
        summary["enriched"] = 0
        log.info(
            "company %s: scraped %s vacancies but skipped grading - %s",
            company_id,
            len(job_ids),
            health["message"],
        )
        return summary

    selection = select_for_enrichment(
        list(dict.fromkeys(job_ids)),
        target_language,
        profile_name,
        MAX_ENRICHED_PER_SCRAPE,
    )
    summary["enrich_needed"] = selection["needed"]
    summary["enrich_skipped_current"] = selection["skipped_current"]
    summary["enrich_deferred"] = selection["deferred"]
    summary["hit_enrich_cap"] = selection["deferred"] > 0

    if selection["deferred"]:
        log.warning(
            "company %s: %s vacancies need enrichment, queueing %s and leaving "
            "%s for a later run.",
            company_id,
            selection["needed"],
            len(selection["ids"]),
            selection["deferred"],
        )

    handles = [
        DBOS.enqueue_workflow(
            LLM_QUEUE, enrich_job_workflow, job_id, target_language, profile_name, False
        )
        for job_id in selection["ids"]
    ]

    enriched = failed = 0
    for index, handle in enumerate(handles):
        # Children finish in queue order, so the one awaited is the one on (or
        # next for) the GPU; the UI reads its `stage` event through this id.
        _report("grading", done=index, total=len(handles), current=handle.workflow_id)
        try:
            result = handle.get_result()
            enriched += result.get("status") == "enriched"
        except Exception as error:  # one vacancy must not sink the scrape
            log.exception("company %s: enriching a vacancy failed: %s", company_id, error)
            failed += 1

            # A failure might be this one vacancy, or it might be the model
            # server having gone away. Ask, rather than guess from the
            # exception type, which does not survive the workflow boundary
            # reliably. If the server is gone, everything still queued will
            # fail the same way, so stop now.
            health = llm_available()
            if not health["ok"]:
                abandoned = _cancel_queued(handles, index + 1)
                log.warning(
                    "company %s: model server went away mid-run, cancelled %s "
                    "queued vacancy(ies) - %s",
                    company_id,
                    abandoned,
                    health["message"],
                )
                summary["enrich_status"] = "llm_unavailable"
                summary["enrich_message"] = health["message"]
                summary["enrich_cancelled"] = abandoned
                break

    summary["enriched"] = enriched
    summary["failed"] = failed
    log.info("company scrape done: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Batch company scrape
# ---------------------------------------------------------------------------


def batch_child_id(batch_id: str, company_id: int) -> str:
    """The workflow id of one company's scrape inside a batch.

    Deterministic, so the status endpoint can find each company's workflow
    from the batch id alone, before or after it has started.
    """
    return f"{batch_id}-company-{company_id}"


@DBOS.workflow()
def scrape_companies_workflow(
    companies: list[dict],
    target_language: str = "en",
    profile_name: str = "default",
) -> dict:
    """Scrape several employers - the same flow as one - and wait for them all.

    `companies` is `[{"id": ..., "name": ...}]`, chosen by the caller. Each is
    one `scrape_company_workflow` on COMPANY_QUEUE, so at most
    COMPANY_SCRAPE_CONCURRENCY run at once, and their grading still goes
    through the single-slot `llm` queue like any other scrape.

    Cancelling this workflow with `cancel_children=True` stops every company
    scrape still waiting or running, and the vacancies they queued for the GPU.
    """
    batch_id = DBOS.workflow_id
    DBOS.set_event("companies", companies)
    log.info("batch %s: scraping %s companies", batch_id, len(companies))

    handles = []
    for company in companies:
        with SetWorkflowID(batch_child_id(batch_id, company["id"])):
            handles.append(
                DBOS.enqueue_workflow(
                    COMPANY_QUEUE,
                    scrape_company_workflow,
                    company["id"],
                    50,
                    None,
                    target_language,
                    profile_name,
                    True,
                )
            )

    results = []
    for company, handle in zip(companies, handles):
        try:
            results.append(handle.get_result())
        except Exception as error:  # one employer must not sink the batch
            log.warning("batch %s: company %s did not finish: %s", batch_id, company["id"], error)
            results.append({"company_id": company["id"], "status": "failed", "error": str(error)})

    summary = {
        "companies": len(companies),
        "done": sum(1 for r in results if r.get("status") == "done"),
        "failed": sum(1 for r in results if r.get("status") != "done"),
        "vacancies": sum(r.get("vacancies", 0) for r in results),
        "created": sum(r.get("created", 0) for r in results),
        "enriched": sum(r.get("enriched", 0) for r in results),
    }
    log.info("batch %s done: %s", batch_id, summary)
    return summary
