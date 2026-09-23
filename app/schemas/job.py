"""Transfer objects for vacancies.

`JobCreate` is what the repository accepts. Related data arrives as plain
values - a location as its fields, vocabularies as lists of names - and the
repository resolves them to rows, so callers never handle foreign keys.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class LocationBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    city: str = Field(min_length=1, max_length=255)
    city_slug: Optional[str] = Field(default=None, max_length=255)
    municipality: Optional[str] = Field(default=None, max_length=255)
    municipality_slug: Optional[str] = Field(default=None, max_length=255)
    province: Optional[str] = Field(default=None, max_length=128)
    province_slug: Optional[str] = Field(default=None, max_length=128)
    country_iso: Optional[str] = Field(default=None, max_length=2)
    country_name: Optional[str] = Field(default=None, max_length=128)
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None


class LocationRead(LocationBase):
    id: int


class DcoTitleBase(BaseModel):
    """One candidate occupation title from the classifier."""

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    rank: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=255)
    slug: Optional[str] = Field(default=None, max_length=255)
    generic_title: Optional[str] = Field(default=None, max_length=255)
    similarity: Optional[float] = Field(default=None, ge=0, le=1)
    source: Optional[str] = Field(default=None, max_length=128)


class DcoTitleRead(DcoTitleBase):
    id: int


class VocabularyTerms(BaseModel):
    """A vocabulary list plus which entry the source treats as primary."""

    model_config = ConfigDict(str_strip_whitespace=True)

    values: list[str] = Field(default_factory=list)
    primary: Optional[str] = None


class JobBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=512)
    description: Optional[str] = None
    status: Optional[str] = Field(default=None, max_length=64)
    origin: Optional[str] = Field(default=None, max_length=64)

    function_title: Optional[str] = Field(default=None, max_length=512)
    function_title_slug: Optional[str] = Field(default=None, max_length=512)
    dco_title: Optional[str] = Field(default=None, max_length=255)
    dco_title_slug: Optional[str] = Field(default=None, max_length=255)
    isco: Optional[str] = Field(default=None, max_length=16)
    jdco: Optional[str] = Field(default=None, max_length=255)
    job_titles: list[str] = Field(default_factory=list)

    career_level: Optional[str] = Field(default=None, max_length=64)

    salary_min: Optional[int] = Field(default=None, ge=0)
    salary_max: Optional[int] = Field(default=None, ge=0)
    salary_type: Optional[str] = Field(default=None, max_length=32)
    working_hours_min: Optional[int] = Field(default=None, ge=0)
    working_hours_max: Optional[int] = Field(default=None, ge=0)

    street: Optional[str] = Field(default=None, max_length=255)
    zip_code: Optional[str] = Field(default=None, max_length=16)

    apply_option: Optional[str] = Field(default=None, max_length=64)
    apply_url: Optional[str] = None
    detail_url: Optional[str] = None
    number_of_applies: Optional[int] = Field(default=None, ge=0)

    contact_first_name: Optional[str] = Field(default=None, max_length=128)
    contact_last_name: Optional[str] = Field(default=None, max_length=128)
    contact_email: Optional[str] = Field(default=None, max_length=255)
    contact_phone: Optional[str] = Field(default=None, max_length=64)

    start_date: Optional[date] = None
    end_date: Optional[date] = None
    expiration_date: Optional[date] = None

    recruiter_id: Optional[str] = Field(default=None, max_length=128)
    reference_id: Optional[str] = Field(default=None, max_length=255)


class JobCreate(JobBase):
    """A vacancy plus its related data, as plain values."""

    source: str = Field(default="nvb", max_length=64)
    external_id: str = Field(min_length=1, max_length=128)

    # Resolved to rows by the repository.
    company_name: Optional[str] = Field(default=None, max_length=512)
    company_slug: Optional[str] = Field(default=None, max_length=512)
    company_type: Optional[str] = Field(default=None, max_length=128)
    company_website: Optional[str] = Field(default=None, max_length=512)

    location: Optional[LocationBase] = None

    categories: VocabularyTerms = Field(default_factory=VocabularyTerms)
    industries: VocabularyTerms = Field(default_factory=VocabularyTerms)
    education_levels: VocabularyTerms = Field(default_factory=VocabularyTerms)
    contract_types: VocabularyTerms = Field(default_factory=VocabularyTerms)

    dco_titles: list[DcoTitleBase] = Field(default_factory=list)

    raw: Optional[dict] = None


class JobUpdate(BaseModel):
    """Partial update of the vacancy's own columns.

    Related data (company, location, vocabularies, classifier titles) is
    replaced wholesale by `upsert`, not patched here.
    """

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)

    title: Optional[str] = Field(default=None, min_length=1, max_length=512)
    description: Optional[str] = None
    status: Optional[str] = Field(default=None, max_length=64)
    career_level: Optional[str] = Field(default=None, max_length=64)
    salary_min: Optional[int] = Field(default=None, ge=0)
    salary_max: Optional[int] = Field(default=None, ge=0)
    salary_type: Optional[str] = Field(default=None, max_length=32)
    working_hours_min: Optional[int] = Field(default=None, ge=0)
    working_hours_max: Optional[int] = Field(default=None, ge=0)
    apply_url: Optional[str] = None
    detail_url: Optional[str] = None
    number_of_applies: Optional[int] = Field(default=None, ge=0)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    expiration_date: Optional[date] = None


class CompanySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: Optional[str] = None


class JobRead(JobBase):
    """What the application hands back for a vacancy."""

    id: int
    source: str
    external_id: str
    created_at: datetime
    updated_at: datetime

    company: Optional[CompanySummary] = None
    location: Optional[LocationRead] = None
    dco_titles: list[DcoTitleRead] = Field(default_factory=list)


class JobPage(BaseModel):
    items: list[JobRead]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total
