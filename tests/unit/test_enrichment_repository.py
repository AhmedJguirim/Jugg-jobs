"""Unit tests for the enrichment repository.

The staleness logic is the most valuable thing here: it is what keeps the GPU
idle when nothing has changed, so it is tested from both directions.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.repositories import enrichment as repo
from app.repositories import job as job_repo
from app.schemas.job import JobCreate

TRANSLATE_V1 = "translate-nl-en-v1"
ASSESS_V1 = "assess-v1"


@pytest.fixture
def job(session: Session):
    return job_repo.create(
        session,
        JobCreate(
            external_id="ext-1",
            title="Python Developer",
            description="<p>Wij zoeken een Python developer.</p>",
            company_name="Acme B.V.",
        ),
    )


def add_translation(session: Session, job_id: int, **overrides):
    values = dict(
        job_id=job_id,
        source_hash="hash-a",
        prompt_version=TRANSLATE_V1,
        source_language="nl",
        title="Python Developer",
        description="We are looking for a Python developer.",
        model="qwen3.5:4b",
        duration_ms=1500,
    )
    values.update(overrides)
    return repo.upsert_translation(session, **values)


def add_assessment(session: Session, job_id: int, **overrides):
    values = dict(
        job_id=job_id,
        source_hash="hash-a",
        prompt_version=ASSESS_V1,
        fit_score=80,
        verdict="strong_match",
        duration_ms=800,
    )
    values.update(overrides)
    return repo.upsert_assessment(session, **values)


class TestTranslations:
    def test_creates_a_translation(self, session: Session, job):
        translation = add_translation(session, job.id)

        assert translation.id is not None
        assert translation.target_language == "en"

    def test_get_returns_it(self, session: Session, job):
        add_translation(session, job.id)

        assert repo.get_translation(session, job.id) is not None

    def test_get_returns_none_when_absent(self, session: Session, job):
        assert repo.get_translation(session, job.id) is None

    def test_upsert_replaces_rather_than_duplicating(self, session: Session, job):
        add_translation(session, job.id)
        add_translation(session, job.id, description="A better translation.")

        translation = repo.get_translation(session, job.id)
        assert translation.description == "A better translation."

    def test_a_second_language_is_a_separate_row(self, session: Session, job):
        add_translation(session, job.id)
        add_translation(session, job.id, target_language="fr", description="Bonjour")

        assert repo.get_translation(session, job.id, "en").description.startswith("We are")
        assert repo.get_translation(session, job.id, "fr").description == "Bonjour"

    def test_records_cost_metadata(self, session: Session, job):
        translation = add_translation(session, job.id, prompt_tokens=100, output_tokens=200)

        assert translation.prompt_tokens == 100
        assert translation.output_tokens == 200

    def test_can_record_a_skip(self, session: Session, job):
        translation = add_translation(
            session, job.id, skipped=True, skip_reason="already English"
        )

        assert translation.skipped is True


class TestTranslationStaleness:
    def test_absent_translation_is_not_current(self):
        assert repo.translation_is_current(None, source_hash="h", prompt_version=TRANSLATE_V1) is False

    def test_matching_hash_and_version_is_current(self, session: Session, job):
        translation = add_translation(session, job.id)

        assert repo.translation_is_current(
            translation, source_hash="hash-a", prompt_version=TRANSLATE_V1
        ) is True

    def test_changed_source_text_is_stale(self, session: Session, job):
        """The description was edited upstream, so the translation must be redone."""
        translation = add_translation(session, job.id)

        assert repo.translation_is_current(
            translation, source_hash="hash-b", prompt_version=TRANSLATE_V1
        ) is False

    def test_newer_prompt_version_is_stale(self, session: Session, job):
        """An improved prompt should re-run only rows made by the older one."""
        translation = add_translation(session, job.id)

        assert repo.translation_is_current(
            translation, source_hash="hash-a", prompt_version="translate-nl-en-v2"
        ) is False


class TestPendingQueries:
    def test_untranslated_job_is_pending(self, session: Session, job):
        assert job.id in {j.id for j in repo.pending_translation(session)}

    def test_translated_job_is_not_pending(self, session: Session, job):
        add_translation(session, job.id)

        pending = repo.pending_translation(session, prompt_version=TRANSLATE_V1)
        assert job.id not in {j.id for j in pending}

    def test_job_translated_by_an_older_prompt_is_pending_again(self, session: Session, job):
        add_translation(session, job.id, prompt_version="translate-nl-en-v0")

        pending = repo.pending_translation(session, prompt_version=TRANSLATE_V1)
        assert job.id in {j.id for j in pending}

    def test_jobs_without_a_description_are_skipped(self, session: Session):
        bare = job_repo.create(session, JobCreate(external_id="bare", title="No body"))

        pending = repo.pending_translation(session)
        assert bare.id not in {j.id for j in pending}

    def test_unassessed_job_is_pending(self, session: Session, job):
        assert job.id in {j.id for j in repo.pending_assessment(session)}

    def test_assessed_job_is_not_pending(self, session: Session, job):
        add_assessment(session, job.id)

        pending = repo.pending_assessment(session, prompt_version=ASSESS_V1)
        assert job.id not in {j.id for j in pending}

    def test_assessment_under_a_different_profile_is_still_pending(self, session: Session, job):
        add_assessment(session, job.id, profile_name="other")

        pending = repo.pending_assessment(session, profile_name="default")
        assert job.id in {j.id for j in pending}

    def test_limit_is_respected(self, session: Session, job):
        for index in range(3):
            job_repo.create(
                session,
                JobCreate(external_id=f"more-{index}", title="X", description="<p>tekst</p>"),
            )

        assert len(repo.pending_translation(session, limit=2)) == 2


class TestAssessments:
    def test_creates_an_assessment(self, session: Session, job):
        assessment = add_assessment(session, job.id)

        assert assessment.fit_score == 80
        assert assessment.verdict == "strong_match"

    def test_upsert_replaces(self, session: Session, job):
        add_assessment(session, job.id)
        add_assessment(session, job.id, fit_score=20, verdict="irrelevant")

        assert repo.get_assessment(session, job.id).fit_score == 20

    def test_a_second_profile_is_a_separate_row(self, session: Session, job):
        add_assessment(session, job.id, profile_name="default", fit_score=80)
        add_assessment(session, job.id, profile_name="data", fit_score=40)

        assert repo.get_assessment(session, job.id, "default").fit_score == 80
        assert repo.get_assessment(session, job.id, "data").fit_score == 40

    def test_stores_arrays_and_details(self, session: Session, job):
        assessment = add_assessment(
            session,
            job.id,
            red_flags=["requires Dutch"],
            matched_requirements=["Python"],
            details={"raw": True},
        )

        assert assessment.red_flags == ["requires Dutch"]
        assert assessment.matched_requirements == ["Python"]
        assert assessment.details == {"raw": True}

    def test_blocked_and_score_are_independent(self, session: Session, job):
        assessment = add_assessment(session, job.id, fit_score=90, blocked=True)

        assert assessment.fit_score == 90
        assert assessment.blocked is True


class TestShortlist:
    @pytest.fixture(autouse=True)
    def seed(self, session: Session):
        for index, (score, blocked) in enumerate(
            [(90, False), (70, False), (85, True), (30, False)]
        ):
            created = job_repo.create(
                session,
                JobCreate(
                    external_id=f"s-{index}",
                    title=f"Job {index}",
                    description="<p>tekst</p>",
                ),
            )
            add_assessment(session, created.id, fit_score=score, blocked=blocked)
            add_translation(session, created.id, title=f"Job {index} EN")

    def test_returns_matches_above_the_threshold(self, session: Session):
        rows = repo.shortlist(session, min_score=55)

        assert len(rows) == 2  # 90 and 70; the 85 is blocked, the 30 too low

    def test_orders_by_score_descending(self, session: Session):
        rows = repo.shortlist(session, min_score=0)

        scores = [assessment.fit_score for _, assessment, _ in rows]
        assert scores == sorted(scores, reverse=True)

    def test_blocked_jobs_are_hidden_by_default(self, session: Session):
        rows = repo.shortlist(session, min_score=0)

        assert all(assessment.blocked is False for _, assessment, _ in rows)

    def test_blocked_jobs_can_be_included(self, session: Session):
        """A great role you cannot take is worth seeing, just not by default."""
        rows = repo.shortlist(session, min_score=0, include_blocked=True)

        assert any(assessment.blocked for _, assessment, _ in rows)

    def test_includes_the_translation(self, session: Session):
        rows = repo.shortlist(session, min_score=55)

        assert all(translation is not None for _, _, translation in rows)

    def test_limit_applies(self, session: Session):
        assert len(repo.shortlist(session, min_score=0, limit=1)) == 1


class TestProgress:
    def test_counts_pending_work(self, session: Session, job):
        progress = repo.enrichment_progress(session)

        assert progress["jobs_with_description"] == 1
        assert progress["translated"] == 0
        assert progress["translation_pending"] == 1

    def test_counts_completed_work_and_gpu_time(self, session: Session, job):
        add_translation(session, job.id, duration_ms=2000)
        add_assessment(session, job.id, duration_ms=1000)

        progress = repo.enrichment_progress(session)

        assert progress["translated"] == 1
        assert progress["assessed"] == 1
        assert progress["gpu_seconds_spent"] == pytest.approx(3.0)
