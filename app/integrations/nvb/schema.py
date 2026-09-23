"""Pydantic models for the Nationale Vacaturebank jobs API.

Derived from the live response of:
    GET https://api.nationalevacaturebank.nl/api/jobs/v3/sites/nationalevacaturebank.nl/jobs

The payload is HAL-shaped: page metadata sits at the root and the vacancies live
under `_embedded.jobs`.

Every model allows unknown fields (`extra="allow"`), so a field added by the API
shows up on the object instead of raising. Fields observed as null or missing in
at least one real result are Optional with a default, so parsing never fails on a
sparse record.

    from app.integrations.nvb.schema import JobSearchResponse

    page = JobSearchResponse.model_validate(response.json())
    for job in page.jobs:
        print(job.title, job.company_name, job.city)
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---- nested objects ----


class Link(_Base):
    href: str


class JobLinks(_Base):
    self: Optional[Link] = None
    detail: Optional[Link] = None  # public web page for the vacancy
    related_jobs: Optional[Link] = Field(default=None, alias="relatedJobs")


class Company(_Base):
    name: Optional[str] = None
    slug: Optional[str] = None
    type: Optional[str] = None  # e.g. "human_resource_manager"
    website: Optional[str] = None  # observed null


class Contact(_Base):
    first_name: Optional[str] = Field(default=None, alias="firstName")
    last_name: Optional[str] = Field(default=None, alias="lastName")
    email_address: Optional[str] = Field(default=None, alias="emailAddress")
    phone_number: Optional[str] = Field(default=None, alias="phoneNumber")
    street: Optional[str] = None
    zipcode: Optional[str] = None  # lowercase 'c', unlike WorkLocation.zipCode
    city: Optional[str] = None
    website: Optional[str] = None


class Apply(_Base):
    option: Optional[str] = None  # e.g. "external"
    url: Optional[str] = None  # null when the option is not external


class Country(_Base):
    iso: Optional[str] = None  # "NL"
    name: Optional[str] = None  # "Nederland"


class Geolocation(_Base):
    # The API returns these as strings, not numbers.
    latitude: Optional[str] = None
    longitude: Optional[str] = None


class WorkLocation(_Base):
    street: Optional[str] = None
    zip_code: Optional[str] = Field(default=None, alias="zipCode")
    city: Optional[str] = None
    city_slug: Optional[str] = Field(default=None, alias="citySlug")
    municipality: Optional[str] = None
    municipality_slug: Optional[str] = Field(default=None, alias="municipalitySlug")
    province: Optional[str] = None
    province_slug: Optional[str] = Field(default=None, alias="provinceSlug")
    display_name: Optional[str] = Field(default=None, alias="displayName")
    country: Optional[Country] = None
    geolocation: Optional[Geolocation] = None
    source: Optional[str] = None


class WorkingHours(_Base):
    min: Optional[int] = None
    max: Optional[int] = None


class Salary(_Base):
    min: Optional[int] = None
    max: Optional[int] = None
    type: Optional[str] = None  # e.g. "monthly"


class Metadata(_Base):
    isco: Optional[str] = None  # ISCO occupation code
    jdco: Optional[str] = None


class DcoTitle(_Base):
    """Classifier-normalised job title with a confidence score."""

    title: Optional[str] = None
    slug: Optional[str] = None
    generic_title: Optional[str] = Field(default=None, alias="genericTitle")
    similarity: Optional[float] = None
    source: Optional[str] = None


class Logos(_Base):
    """Logo URLs keyed by pixel size; null on the job when there is no logo.

    Sizes seen: 90x32, 100x35, 110x39, 152x54, 180x64, 199x71, 260x92, 345x122,
    1000x1000. The keys are not valid Python identifiers, so read them with
    `logos.get("1000x1000")`.
    """

    def get(self, size: str) -> Optional[str]:
        return (self.model_extra or {}).get(size)

    @property
    def largest(self) -> Optional[str]:
        return self.get("1000x1000")


# ---- the vacancy ----


class Job(_Base):
    id: str
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None  # "active"
    origin: Optional[str] = None  # "jobdigger", "ndp"

    company: Company = Field(default_factory=Company)
    contact: Optional[Contact] = None
    apply: Optional[Apply] = None
    work_location: Optional[WorkLocation] = Field(default=None, alias="workLocation")
    working_hours: Optional[WorkingHours] = Field(default=None, alias="workingHours")
    salary: Optional[Salary] = None  # null on most records
    logos: Optional[Logos] = None  # null when the employer has no logo
    metadata: Optional[Metadata] = None

    # ISO-8601 with a Z suffix; pydantic parses these into datetimes.
    start_date: Optional[datetime] = Field(default=None, alias="startDate")
    end_date: Optional[datetime] = Field(default=None, alias="endDate")
    expiration_date: Optional[datetime] = Field(default=None, alias="expirationDate")

    career_level: Optional[str] = Field(default=None, alias="careerLevel")  # "Starter", "Ervaren"
    contract_type: Optional[str] = Field(default=None, alias="contractType")  # "Vast"
    contract_types: list[str] = Field(default_factory=list, alias="contractTypes")
    education_level: Optional[str] = Field(default=None, alias="educationLevel")  # "MBO", "HBO"
    education_levels: list[str] = Field(default_factory=list, alias="educationLevels")
    categories: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    job_titles: list[str] = Field(default_factory=list, alias="jobTitles")

    function_title: Optional[str] = Field(default=None, alias="functionTitle")
    function_title_slug: Optional[str] = Field(default=None, alias="functionTitleSlug")
    dco_title: Optional[str] = Field(default=None, alias="dcoTitle")
    dco_title_slug: Optional[str] = Field(default=None, alias="dcoTitleSlug")
    dco_titles: list[DcoTitle] = Field(default_factory=list, alias="dcoTitles")

    recruiter_id: Optional[str] = Field(default=None, alias="recruiterId")
    reference_id: Optional[str] = Field(default=None, alias="referenceId")
    number_of_applies: Optional[int] = Field(default=None, alias="numberOfApplies")
    is_pbp: Optional[bool] = Field(default=None, alias="isPbp")
    display_options: list[str] = Field(default_factory=list, alias="displayOptions")
    custom_fields: list[Any] = Field(default_factory=list, alias="customFields")

    # Always null in the sampled responses; typed loosely until a non-null appears.
    requirements: Optional[Any] = None
    video: Optional[Any] = None
    working_place: Optional[Any] = Field(default=None, alias="workingPlace")
    corporate_identity_template: Optional[Any] = Field(
        default=None, alias="corporateIdentityTemplate"
    )

    links: Optional[JobLinks] = Field(default=None, alias="_links")

    # -- convenience --

    @property
    def city(self) -> Optional[str]:
        return self.work_location.city if self.work_location else None

    @property
    def company_name(self) -> Optional[str]:
        return self.company.name

    @property
    def url(self) -> Optional[str]:
        """Public vacancy page, when the API supplied one."""
        if self.links and self.links.detail:
            return self.links.detail.href
        return None

    @property
    def salary_range(self) -> Optional[str]:
        if not self.salary or self.salary.min is None:
            return None
        return f"{self.salary.min}-{self.salary.max} ({self.salary.type})"


# ---- the page ----


class Embedded(_Base):
    jobs: list[Job] = Field(default_factory=list)
    aggregations: Optional[Any] = None  # facet counts; shape varies by query


class JobSearchResponse(_Base):
    page: int
    limit: int
    pages: int
    total: int
    embedded: Embedded = Field(default_factory=Embedded, alias="_embedded")
    links: dict[str, Link] = Field(default_factory=dict, alias="_links")

    @property
    def jobs(self) -> list[Job]:
        return self.embedded.jobs

    @property
    def has_next_page(self) -> bool:
        return self.page < self.pages
