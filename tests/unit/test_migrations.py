"""Guards that migrations and models stay in step.

A drifted migration only shows up in production, so assert here that the
migrated database matches what the ORM declares.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.config import get_settings
from app.db.models import Base


@pytest.fixture(scope="module")
def migrated_engine():
    """Engine for the migrated application database."""
    engine = create_engine(get_settings().sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                pytest.skip("application database has no migrations applied")
    except Exception as error:  # pragma: no cover - environment guard
        pytest.skip(f"application database unavailable ({error.__class__.__name__})")
    yield engine
    engine.dispose()


def test_no_pending_schema_changes(migrated_engine):
    """`alembic revision --autogenerate` should produce an empty diff."""
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        diff = compare_metadata(context, Base.metadata)

    assert diff == [], f"models and migrations have drifted: {diff}"


def test_companies_table_exists_with_expected_columns(migrated_engine):
    with migrated_engine.connect() as connection:
        columns = {c["name"]: c for c in inspect(connection).get_columns("companies")}

    expected = {
        "id",
        "name",
        "type",
        "website",
        "slug",
        "province",
        "google_jobs_link",
        "linkedin_jobs_link",
        "indeed_jobs_link",
        "glassdoor_jobs_link",
        "nationalevacaturebank_jobs_link",
        "last_scraped_at",
        "last_scrape_vacancies",
        "created_at",
        "updated_at",
    }
    assert expected == set(columns)


def test_nullability_matches_specification(migrated_engine):
    with migrated_engine.connect() as connection:
        columns = {c["name"]: c for c in inspect(connection).get_columns("companies")}

    assert columns["name"]["nullable"] is False
    assert columns["created_at"]["nullable"] is False
    assert columns["updated_at"]["nullable"] is False

    for optional in (
        "type",
        "website",
        "slug",
        "province",
        "google_jobs_link",
        "linkedin_jobs_link",
        "indeed_jobs_link",
        "glassdoor_jobs_link",
        "nationalevacaturebank_jobs_link",
    ):
        assert columns[optional]["nullable"] is True, f"{optional} should be nullable"


def test_unique_constraints_present(migrated_engine):
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        unique = {
            tuple(c["column_names"]) for c in inspector.get_unique_constraints("companies")
        }
        indexes = {tuple(i["column_names"]) for i in inspector.get_indexes("companies")}

    assert ("name",) in unique
    assert ("slug",) in unique
    assert ("province",) in indexes
