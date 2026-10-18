"""Grade-then-create for parsed listings, and the guards that keep the Dutch
pipeline away from them (ADR-0015)."""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import func, select

from app.db.models.enrichment import JobAssessment, JobTranslation
from app.db.models.job import Job
from app.llm import global_grader
from app.llm.profile import CandidateProfile
from app.llm.service import AssessmentUnparseable, LlmResult
from app.parsers import ParsedListing, parse_listing
from app.repositories import enrichment as enrichment_repo
from app.repositories import job as job_repo
from app.schemas.job import JobCreate
from app.services import listing_grading as grading

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ashbyhq"
LENDABLE = "https://jobs.ashbyhq.com/lendable/ac527b20-e9fc-41d1-841c-bc881e2abef6"


@pytest.fixture
def job_payload() -> dict:
    html = (FIXTURES / "lendable_php_engineer.html").read_text(encoding="utf-8")
    parsed = parse_listing("ashbyhq", LENDABLE, html)
    assert isinstance(parsed, ParsedListing)
    return parsed.job.model_dump(mode="json")


@pytest.fixture(autouse=True)
def profile(monkeypatch) -> CandidateProfile:
    profile = CandidateProfile.from_dict({
        "name": "t",
        "headline": "Backend engineer",
        "skills": {"core": ["PHP", "Symfony", "Laravel"]},
        "languages": {"native": ["Arabic"], "fluent": ["English"]},
        "location": {"based_in": "Sousse, Tunisia", "work_authorization": "No EU work permit. Needs visa sponsorship."},
    })
    monkeypatch.setattr(grading, "load_profile", lambda name: profile)
    return profile


def stub_model(monkeypatch, reply: str | None):
    """Replace the model call; `None` means an unreadable reply."""

    def fake_chat(messages, **kwargs):
        return LlmResult(content=reply if reply is not None else "garbage", model="qwen3.5:4b",
                         prompt_tokens=1, output_tokens=1, duration_s=0.1, raw={})

    monkeypatch.setattr(global_grader, "chat", fake_chat)


REPLY = (
    '{"fit_score": 90, "verdict": "strong_match", "remote": "remote", "remote_scope": "region", '
    '"remote_places": ["UK", "select EU countries"], '
    '"location_evidence": "Please note we are only able to hire in the UK and select EU countries on a remote basis", '
    '"summary": "A PHP role.", "red_flags": []}'
)


class TestPrepare:
    def test_cleans_the_text_and_collects_the_facts(self, session, job_payload):
        prepared = grading.prepare(job_payload, "t", session)

        assert prepared["title"] == "PHP Engineer" and prepared["company"] == "Lendable"
        assert prepared["facts"]["Location"] == "EU Remote"
        assert "PHP 8.0+ is a must" in prepared["text"] and "<p>" not in prepared["text"]
        assert prepared["job_id"] is None and prepared["current"] is False

    def test_the_profile_defaults_to_the_configured_one(self, session, job_payload, monkeypatch):
        monkeypatch.setattr(grading, "get_settings", lambda: type("S", (), {"llm_default_profile": "ahmed"})())

        assert grading.prepare(job_payload, None, session)["profile_name"] == "ahmed"

    def test_long_text_is_capped(self, session, job_payload):
        job_payload["description"] = "<p>" + ("PHP developer needed for the team. " * 2000) + "</p>"

        prepared = grading.prepare(job_payload, "t", session)

        assert prepared["truncated"] and len(prepared["text"]) <= grading.GRADE_MAX_CHARS


class TestGrade:
    def test_a_restriction_the_model_quotes_is_checked_and_applied(self, session, job_payload, monkeypatch):
        stub_model(monkeypatch, REPLY)

        assessment = grading.grade(grading.prepare(job_payload, "t", session))

        # "UK and select EU countries" does not include Tunisia.
        assert assessment["blocked"] and assessment["eligibility"] == "blocked"
        assert "remote only within UK, select EU countries" in assessment["red_flags"]
        assert assessment["decisions"]["location_restriction"]["includes_candidate"] is False
        assert assessment["prompt_version"] == "assess-global-v1"

    def test_an_unreadable_reply_gives_an_empty_grade(self, session, job_payload, monkeypatch):
        stub_model(monkeypatch, None)

        assert grading.grade(grading.prepare(job_payload, "t", session)) == {}


class TestPersist:
    def graded(self, session, job_payload, monkeypatch):
        stub_model(monkeypatch, REPLY)
        prepared = grading.prepare(job_payload, "t", session)
        return prepared, grading.grade(prepared)

    def test_creates_the_job_its_text_and_its_grade_together(self, session, job_payload, monkeypatch):
        prepared, assessment = self.graded(session, job_payload, monkeypatch)

        result = grading.persist(session, job_payload, prepared, assessment)

        job = session.get(Job, result["job_id"])
        assert job.source == "ashbyhq" and job.title == "PHP Engineer" and job.company.name == "Lendable"
        grade = enrichment_repo.get_assessment(session, job.id, "t")
        assert grade.fit_score == assessment["fit_score"] and grade.blocked
        translation = enrichment_repo.get_translation(session, job.id, "en")
        assert translation.skipped and "PHP 8.0+ is a must" in translation.description
        assert result["status"] == "stored" and result["created"] is True

    def test_a_second_run_updates_instead_of_duplicating(self, session, job_payload, monkeypatch):
        prepared, assessment = self.graded(session, job_payload, monkeypatch)
        first = grading.persist(session, job_payload, prepared, assessment)

        second = grading.persist(session, job_payload, prepared, assessment)

        assert second["job_id"] == first["job_id"] and second["created"] is False
        assert session.scalar(select(func.count()).select_from(JobAssessment).where(JobAssessment.job_id == first["job_id"])) == 1

    def test_current_grade_is_detected_so_the_gpu_can_be_skipped(self, session, job_payload, monkeypatch):
        prepared, assessment = self.graded(session, job_payload, monkeypatch)
        grading.persist(session, job_payload, prepared, assessment)

        assert grading.prepare(job_payload, "t", session)["current"] is True
        assert grading.prepare(job_payload, "other-profile", session)["current"] is False

    def test_user_marks_survive_a_regrade(self, session, job_payload, monkeypatch):
        prepared, assessment = self.graded(session, job_payload, monkeypatch)
        job_id = grading.persist(session, job_payload, prepared, assessment)["job_id"]
        job_repo.set_marks(session, job_id, applied=True)

        grading.persist(session, job_payload, prepared, assessment)

        assert session.get(Job, job_id).applied_at is not None


class TestTheDutchPipelineLeavesOtherSourcesAlone:
    def make_jobs(self, session) -> tuple[Job, Job]:
        nvb, _ = job_repo.upsert(session, JobCreate(source="nvb", external_id="guard-nvb", title="Dev", description="<p>PHP</p>"))
        other, _ = job_repo.upsert(session, JobCreate(source="ashbyhq", external_id="guard-ashby", title="Dev", description="<p>PHP</p>"))
        return nvb, other

    def test_pending_queries_only_return_nvb_vacancies(self, session):
        nvb, other = self.make_jobs(session)

        translating = {j.id for j in enrichment_repo.pending_translation(session, prompt_version="x", limit=100_000)}
        assessing = {j.id for j in enrichment_repo.pending_assessment(session, profile_name="t", prompt_version="x", limit=100_000)}

        assert nvb.id in translating and nvb.id in assessing
        assert other.id not in translating and other.id not in assessing

    def test_a_graded_job_is_not_picked_up_after_its_grade_is_stored(self, session, job_payload, monkeypatch):
        stub_model(monkeypatch, REPLY)
        prepared = grading.prepare(job_payload, "t", session)
        job_id = grading.persist(session, job_payload, prepared, grading.grade(prepared))["job_id"]

        pending = {j.id for j in enrichment_repo.pending_assessment(session, profile_name="t", prompt_version="assess-v4", limit=100_000)}

        assert job_id not in pending

    def test_enrich_job_workflow_skips_other_sources(self, monkeypatch):
        from app.workflows import enrichment as flow

        calls = []
        monkeypatch.setattr(flow, "prepare_text", lambda *a, **k: {
            "found": True, "source": "ashbyhq", "text": "x", "needs_translation": True, "needs_assessment": True})
        monkeypatch.setattr(flow, "cool_down_gpu", lambda: calls.append("cool"))
        monkeypatch.setattr(flow, "translate_step", lambda *a, **k: calls.append("translate"))

        result = flow.enrich_job_workflow.__wrapped__(1, "en", "t", False)

        assert result["status"] == "not_nvb" and calls == []

    def test_nvb_vacancies_still_take_the_old_path(self, monkeypatch):
        from app.workflows import enrichment as flow

        monkeypatch.setattr(flow, "prepare_text", lambda *a, **k: {
            "found": True, "source": "nvb", "text": "", "needs_translation": False, "needs_assessment": False})

        assert flow.enrich_job_workflow.__wrapped__(1, "en", "t", False)["status"] == "no_description"

    def test_a_replayed_prepare_result_without_a_source_key_is_treated_as_nvb(self, monkeypatch):
        from app.workflows import enrichment as flow

        monkeypatch.setattr(flow, "prepare_text", lambda *a, **k: {
            "found": True, "text": "", "needs_translation": False, "needs_assessment": False})

        assert flow.enrich_job_workflow.__wrapped__(1, "en", "t", False)["status"] == "no_description"


class TestGreenhouseListings:
    URL = "https://job-boards.greenhouse.io/2k/jobs/8035080003"

    @pytest.fixture
    def gh_payload(self):
        html = (FIXTURES.parent / "greenhouse" / "2k_lead_backend_engineer.html").read_text(encoding="utf-8")
        parsed = parse_listing("greenhouse", self.URL, html)
        assert isinstance(parsed, ParsedListing)
        return parsed.job.model_dump(mode="json")

    def test_it_is_graded_with_the_pages_facts_and_then_created(self, session, gh_payload, monkeypatch):
        stub_model(monkeypatch, '{"fit_score": 80, "verdict": "possible", "remote": "remote", "summary": "A role."}')
        prepared = grading.prepare(gh_payload, "t", session)

        assert prepared["company"] == "2K" and prepared["facts"] == {
            "Location": "San Mateo, California, United States",
            "Salary": "169100-250280 USD (yearly)",  # with its currency
        }
        assessment = grading.grade(prepared)
        result = grading.persist(session, gh_payload, prepared, assessment)

        job = session.get(Job, result["job_id"])
        assert (job.source, job.external_id, job.company.name) == ("greenhouse", "8035080003", "2K")
        assert (job.salary_min, job.salary_max, job.salary_type) == (169100, 250280, "yearly")
        assert enrichment_repo.get_assessment(session, job.id, "t").prompt_version == "assess-global-v1"

    def test_the_dutch_pipeline_leaves_it_alone(self, session, gh_payload, monkeypatch):
        stub_model(monkeypatch, '{"fit_score": 80, "verdict": "possible"}')
        prepared = grading.prepare(gh_payload, "t", session)
        job_id = grading.persist(session, gh_payload, prepared, grading.grade(prepared))["job_id"]

        pending = {j.id for j in enrichment_repo.pending_assessment(
            session, profile_name="t", prompt_version="assess-v4", limit=100_000)}

        assert job_id not in pending


class TestLeverListings:
    URL = "https://jobs.lever.co/jobgether/29ff8655-9468-4c31-8804-93b79abeb2a8"

    @pytest.fixture
    def lv_payload(self):
        html = (FIXTURES.parent / "lever" / "jobgether_full_stack_microsoft_developer.html").read_text(encoding="utf-8")
        parsed = parse_listing("lever", self.URL, html)
        assert isinstance(parsed, ParsedListing)
        return parsed.job.model_dump(mode="json")

    def test_it_is_graded_with_the_pages_facts_and_then_created(self, session, lv_payload, monkeypatch):
        stub_model(monkeypatch, '{"fit_score": 70, "verdict": "possible", "remote": "remote", "summary": "A role."}')
        prepared = grading.prepare(lv_payload, "t", session)

        assert prepared["company"] == "Jobgether"
        assert prepared["facts"]["Location"] == "US" and prepared["facts"]["Location Type"] == "Remote"
        assert prepared["facts"]["Salary"] == "100000-130000 USD (yearly)"  # with its currency
        result = grading.persist(session, lv_payload, prepared, grading.grade(prepared))

        job = session.get(Job, result["job_id"])
        assert (job.source, job.company.name, job.salary_min) == ("lever", "Jobgether", 100000)
        assert enrichment_repo.get_assessment(session, job.id, "t").prompt_version == "assess-global-v1"

    def test_the_dutch_pipeline_leaves_it_alone(self, session, lv_payload, monkeypatch):
        stub_model(monkeypatch, '{"fit_score": 70, "verdict": "possible"}')
        prepared = grading.prepare(lv_payload, "t", session)
        job_id = grading.persist(session, lv_payload, prepared, grading.grade(prepared))["job_id"]

        pending = {j.id for j in enrichment_repo.pending_assessment(
            session, profile_name="t", prompt_version="assess-v4", limit=100_000)}

        assert job_id not in pending
