"""Concurrency tests for the get-or-create helpers.

These use two real database sessions rather than one, because the bug they
guard against only exists between transactions: a single session's identity map
hides it entirely.

The failure being reproduced was observed in production - a four-page company
scrape, whose pages are stored in parallel, died with
`UniqueViolation on uq_industries_name` and a PostgreSQL deadlock.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.company import Company
from app.db.models.location import Location
from app.db.models.taxonomy import Industry
from app.repositories import job as repo
from app.schemas.job import LocationBase


@pytest.fixture
def other_session(engine):
    """A second session on its own connection and transaction.

    Committed here rather than rolled back, because the race only reproduces
    when the competing row is actually visible to the first session. Cleaned up
    explicitly at the end.
    """
    session = Session(bind=engine.connect(), expire_on_commit=False, future=True)
    yield session
    session.rollback()
    session.close()


class TestVocabularyRace:
    def test_term_created_concurrently_is_reused_not_duplicated(
        self, session: Session, other_session: Session, engine
    ):
        """Two pages carrying the same industry must not both insert it."""
        name = "ConcurrentIndustryTest"
        try:
            # Another transaction creates the term and commits it.
            other_session.add(Industry(name=name))
            other_session.commit()

            # Our session has not seen it and tries to create it too.
            term = repo.get_or_create_term(session, Industry, name)

            assert term is not None
            assert term.name == name
            with engine.connect() as connection:
                count = connection.execute(
                    select(func.count()).select_from(Industry).where(Industry.name == name)
                ).scalar()
            assert count == 1
        finally:
            other_session.rollback()
            with engine.begin() as connection:
                connection.execute(Industry.__table__.delete().where(Industry.name == name))

    def test_the_rest_of_the_transaction_survives_a_lost_race(
        self, session: Session, other_session: Session, engine
    ):
        """Losing the race must roll back only the savepoint, not the page."""
        name = "SurvivorIndustryTest"
        try:
            marker = Industry(name="MarkerIndustryTest")
            session.add(marker)
            session.flush()

            other_session.add(Industry(name=name))
            other_session.commit()

            repo.get_or_create_term(session, Industry, name)

            # The work done before the lost race is still in this transaction.
            assert session.scalar(
                select(Industry).where(Industry.name == "MarkerIndustryTest")
            ) is not None
        finally:
            other_session.rollback()
            with engine.begin() as connection:
                connection.execute(Industry.__table__.delete().where(Industry.name == name))


class TestCompanyRace:
    def test_company_created_concurrently_is_reused(
        self, session: Session, other_session: Session, engine
    ):
        name = "ConcurrentCompanyTest B.V."
        try:
            other_session.add(Company(name=name))
            other_session.commit()

            company = repo.get_or_create_company(session, name=name)

            assert company is not None
            assert company.name == name
        finally:
            other_session.rollback()
            with engine.begin() as connection:
                connection.execute(Company.__table__.delete().where(Company.name == name))


class TestLocationRace:
    def test_location_created_concurrently_is_reused(
        self, session: Session, other_session: Session, engine
    ):
        city = "ConcurrentCityTest"
        try:
            other_session.add(Location(city=city, province="Test"))
            other_session.commit()

            location = repo.get_or_create_location(
                session, LocationBase(city=city, province="Test")
            )

            assert location is not None
            assert location.city == city
        finally:
            other_session.rollback()
            with engine.begin() as connection:
                connection.execute(Location.__table__.delete().where(Location.city == city))


class TestSingleSessionBehaviourUnchanged:
    """The ordinary, uncontended path must still behave exactly as before."""

    def test_term_is_created_once(self, session: Session):
        first = repo.get_or_create_term(session, Industry, "OnlyOnceTest")
        second = repo.get_or_create_term(session, Industry, "OnlyOnceTest")

        assert first.id == second.id

    def test_company_is_created_once(self, session: Session):
        first = repo.get_or_create_company(session, name="Once B.V.")
        second = repo.get_or_create_company(session, name="Once B.V.")

        assert first.id == second.id

    def test_location_is_created_once(self, session: Session):
        payload = LocationBase(city="Once City", province="Utrecht")

        first = repo.get_or_create_location(session, payload)
        second = repo.get_or_create_location(session, payload)

        assert first.id == second.id
