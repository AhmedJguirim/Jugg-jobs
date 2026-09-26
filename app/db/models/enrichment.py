"""LLM-derived data about a vacancy: translations and fit assessments.

These are kept out of `jobs` on purpose. They are *derived* data with their own
provenance - which model produced them, under which prompt version, from which
source text - and they are expensive to regenerate (~16s of GPU each). Separate
tables mean:

- re-ingesting a vacancy never destroys work the GPU already did;
- `source_hash` shows at a glance whether a stored result still matches the
  current description, so unchanged text is never re-translated;
- `prompt_version` lets an improved prompt re-run only the rows that predate it;
- a second target language or a second scoring profile is a new row, not a
  migration.
"""

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class JobTranslation(Base, TimestampMixin):
    """One vacancy rendered into one target language."""

    __tablename__ = "job_translations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    target_language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    source_language: Mapped[str | None] = mapped_column(String(8), nullable=True)

    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fingerprint of the *cleaned* source text this was produced from. When it
    # stops matching, the translation is stale and the job is re-queued.
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # True when the text was already English and no model was invoked.
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    skip_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- provenance / cost ---
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("job_id", "target_language", name="uq_job_translations_job_lang"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobTranslation job_id={self.job_id} lang={self.target_language}>"


class JobAssessment(Base, TimestampMixin):
    """How well one vacancy fits one candidate profile.

    `fit_score` rates the *work* only; eligibility barriers live in `blocked`,
    `dutch_required` and `red_flags`. Keeping them apart is what lets a shortlist
    distinguish "wrong profession" from "right job, cannot apply" - see ADR-0007.
    """

    __tablename__ = "job_assessments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    profile_name: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    fit_score: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    seniority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Which tier of the candidate's skills the vacancy's stack falls into.
    # Stored as a column, not just inside `details`, because "show me every
    # core-stack role" is the query this whole pipeline exists to answer.
    stack_match: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    seniority_fit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Three-tier eligibility, derived in code from what the advert states:
    #   blocked    - the advert itself rules the candidate out (red)
    #   relocation - reachable, needs a move or a visa conversation (orange)
    #   remote_ok  - doable from where the candidate already is (green)
    # Indexed because "show me everything I could actually take" is the query
    # this column exists to answer.
    eligibility: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    location_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # True only when the advert says outright that the applicant must already
    # hold the right to work, or already live in the country.
    requires_existing_permit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    offers_sponsorship: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    dutch_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # server_default so the column can be added to a table that already has
    # assessment rows without a separate backfill.
    sponsorship_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    matched_requirements: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    missing_requirements: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    red_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")

    # Full parsed model output, for auditing a score without re-running the GPU.
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # --- provenance / cost ---
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    repaired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("job_id", "profile_name", name="uq_job_assessments_job_profile"),
        # The shortlist query: best fit first, within one profile.
        Index("ix_job_assessments_profile_score", "profile_name", "fit_score"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobAssessment job_id={self.job_id} score={self.fit_score} {self.verdict}>"
