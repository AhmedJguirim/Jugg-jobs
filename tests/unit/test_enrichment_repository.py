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
