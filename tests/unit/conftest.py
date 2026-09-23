"""Fixtures for database unit tests.

Tests run against a real PostgreSQL database (nljobs_test), not SQLite: the
repository uses PostgreSQL-specific features (ON CONFLICT, ILIKE) that SQLite
would not exercise faithfully.

Isolation strategy: each test runs inside a transaction that is rolled back on
teardown. Nothing is ever committed, so tests cannot leak into each other and
the suite leaves the database exactly as it found it.
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402


@pytest.fixture(scope="session")
def engine() -> Engine:
    """Engine bound to the dedicated test database, with the schema created."""
    url = get_settings().sqlalchemy_test_url
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover - environment guard
        pytest.skip(f"test database unavailable ({error.__class__.__name__}); skipping db tests")

    # The schema is created from the models rather than by running Alembic:
    # these tests verify repository behaviour, and migration correctness is
    # asserted separately in tests/unit/test_migrations.py.
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Session:
    """A session wrapped in a transaction that is always rolled back."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False, future=True)

    # Repository code calls session.rollback() when it traps an IntegrityError.
    # Binding to a SAVEPOINT means that rollback undoes only the failed
    # statement, leaving the outer test transaction intact.
    nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        if nested.is_active:
            nested.rollback()
        transaction.rollback()
        connection.close()


@pytest.fixture
def company_payload() -> dict:
    """A complete, valid company payload; override fields per test."""
    return {
        "name": "Test Company B.V.",
        "type": "employer",
        "website": "https://example.test",
        "slug": "test-company",
        "province": "Noord-Holland",
        "google_jobs_link": "https://www.google.com/search?q=Test+Company+jobs",
        "linkedin_jobs_link": "https://www.linkedin.com/jobs/search/?keywords=Test+Company",
        "indeed_jobs_link": "https://nl.indeed.com/jobs?q=Test+Company",
        "glassdoor_jobs_link": "https://www.glassdoor.com/Search/results.htm?keyword=Test+Company",
        "nationalevacaturebank_jobs_link": "https://www.nationalevacaturebank.nl/vacature/zoeken?query=Test",
    }
