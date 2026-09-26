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
with a normal concurrency.

**Why a workflow per vacancy.** Each vacancy is one queued child workflow whose
steps are checkpointed. A crash, a Ctrl-C, or an Ollama restart mid-run resumes
at the step it reached instead of re-spending GPU time on work already done, and
a single failing vacancy does not take the run down with it.
"""

from __future__ import annotations

from typing import Any, Optional

from dbos import DBOS

from app.db.session import session_scope
from app.integrations.nvb import fetch_jobs_page, to_job_creates
from app.llm import assess_job, load_profile, translate_text, translate_title
from app.llm.prompts import ASSESSMENT_PROMPT_VERSION, TRANSLATION_PROMPT_VERSION
from app.logging_setup import log
from app.repositories import enrichment as enrichment_repo
from app.repositories import job as job_repo
from app.text import sanitize_description

# Queue names. The GPU queue's concurrency is the safety property of this module.
LLM_QUEUE = "llm"
INGEST_QUEUE = "job-ingest"

# One at a time. Do not raise these without a second GPU.
LLM_CONCURRENCY = 1
INGEST_WORKER_CONCURRENCY = 3

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


@DBOS.step()
def store_page(payload: dict) -> dict:
    """Persist one page of vacancies, with their companies and locations."""
    created = updated = 0
    job_ids: list[int] = []
    with session_scope() as session:
        for job_payload in to_job_creates(payload):
            job, was_created = job_repo.upsert(session, job_payload)
            job_ids.append(job.id)
            created += was_created
            updated += not was_created

    log.info("stored page: %s created, %s updated", created, updated)
    return {"created": created, "updated": updated, "job_ids": job_ids}


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
    return {
        "fit_score": result.fit_score,
        "verdict": result.verdict,
        "seniority": result.seniority,
        "remote": result.remote,
        "dutch_required": result.dutch_required,
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
                remote=assessment["remote"],
                dutch_required=assessment["dutch_required"],
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
    if not prepared["text"]:
        log.info("enrich: job %s has no usable description", job_id)
        return {"job_id": job_id, "status": "no_description"}

    translation: Optional[dict] = None
    if force or prepared["needs_translation"]:
        translation = translate_step(prepared, target_language)
    english = (translation or {}).get("description") or prepared["existing_translation"]

    assessment: Optional[dict] = None
    if force or prepared["needs_assessment"]:
        assessment = assess_step(prepared, english or "", profile_name)

    if translation is None and assessment is None:
        log.info("enrich: job %s already current, GPU not used", job_id)
        return {"job_id": job_id, "status": "up_to_date"}

    persist_enrichment(job_id, translation, assessment)

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
    for handle in handles:
        try:
            results.append(handle.get_result())
        except Exception as error:  # one bad vacancy must not sink the batch
            log.exception("enrich: a queued vacancy failed: %s", error)
            results.append({"status": "failed", "error": str(error)})

    summary = {
        "queued": len(job_ids),
        "enriched": sum(1 for r in results if r.get("status") == "enriched"),
        "up_to_date": sum(1 for r in results if r.get("status") == "up_to_date"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "gpu_seconds": round(sum(r.get("gpu_ms", 0) for r in results) / 1000, 1),
    }
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

    stored = store_page(first)
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
