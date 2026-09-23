"""Places where work happens.

Locations are deduplicated into their own table because they repeat heavily -
in a 200-vacancy sample the same handful of cities (Amsterdam, Eindhoven,
Utrecht) account for most rows. Storing each once makes "all jobs in city X" an
indexed join rather than a string scan, and gives one place to correct a
misspelled municipality.

Only the *place* lives here. Address detail that belongs to the individual
vacancy (street, postcode) stays on `jobs`, since two vacancies in Amsterdam do
not share a street.
"""

from sqlalchemy import Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Location(Base, TimestampMixin):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    city: Mapped[str] = mapped_column(String(255), nullable=False)
    city_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    municipality: Mapped[str | None] = mapped_column(String(255), nullable=True)
    municipality_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The real Dutch province, unlike companies.province which holds city names
    # in the seed data (see ADR-0002).
    province: Mapped[str | None] = mapped_column(String(128), nullable=True)
    province_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)

    country_iso: Mapped[str | None] = mapped_column(String(2), nullable=True)
    country_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # The API sends coordinates as strings; stored as exact numerics rather than
    # floats so a value round-trips unchanged.
    latitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)

    __table_args__ = (
        # Identity of a place: the same city name can exist in two municipalities
        # (e.g. a village name reused), so both take part in the key.
        UniqueConstraint("city", "municipality", "province", name="uq_locations_city_municipality_province"),
        Index("ix_locations_city", "city"),
        Index("ix_locations_province", "province"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Location id={self.id} city={self.city!r} province={self.province!r}>"
