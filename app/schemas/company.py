"""Transfer objects for the companies API surface.

These are intentionally separate from the ORM model: they define what callers
may send and what the app returns, so a change to the storage layer does not
silently change the public contract (and vice versa).

`CompanyUpdate` uses `None` to mean "leave unchanged" and relies on
`model_dump(exclude_unset=True)` so that an explicit null can still be written.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """Normalise empty/whitespace strings to NULL and trim the rest.

    The seed data contains trailing spaces ("Amsterdam ") which would otherwise
    create distinct province values that differ only by whitespace.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class CompanyBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=512)
    type: Optional[str] = Field(default=None, max_length=128)
    website: Optional[str] = Field(default=None, max_length=512)
    slug: Optional[str] = Field(default=None, max_length=512)
    province: Optional[str] = Field(default=None, max_length=128)
    google_jobs_link: Optional[str] = None
    linkedin_jobs_link: Optional[str] = None
    indeed_jobs_link: Optional[str] = None
    glassdoor_jobs_link: Optional[str] = None
    nationalevacaturebank_jobs_link: Optional[str] = None

    _normalise = field_validator(
        "type",
        "website",
        "slug",
        "province",
        "google_jobs_link",
        "linkedin_jobs_link",
        "indeed_jobs_link",
        "glassdoor_jobs_link",
        "nationalevacaturebank_jobs_link",
        mode="before",
    )(_blank_to_none)


class CompanyCreate(CompanyBase):
    """Payload for creating a company. `id` is assigned by the database."""


class CompanyUpdate(BaseModel):
    """Partial update: only the fields actually supplied are written."""

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=1, max_length=512)
    type: Optional[str] = Field(default=None, max_length=128)
    website: Optional[str] = Field(default=None, max_length=512)
    slug: Optional[str] = Field(default=None, max_length=512)
    province: Optional[str] = Field(default=None, max_length=128)
    google_jobs_link: Optional[str] = None
    linkedin_jobs_link: Optional[str] = None
    indeed_jobs_link: Optional[str] = None
    glassdoor_jobs_link: Optional[str] = None
    nationalevacaturebank_jobs_link: Optional[str] = None


class CompanyRead(CompanyBase):
    """What the application hands back to callers."""

    id: int
    created_at: datetime
    updated_at: datetime


class CompanyPage(BaseModel):
    """A slice of a company listing, with the total for pagination."""

    items: list[CompanyRead]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total
