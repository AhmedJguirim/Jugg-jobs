"""Model registry.

Importing this package imports every model, which is what populates
`Base.metadata`. Alembic's env.py imports this module so autogenerate sees the
full schema; add each new model here.
"""

from app.db.base import Base
from app.db.models.company import Company
from app.db.models.enrichment import JobAssessment, JobTranslation
from app.db.models.job import Job
from app.db.models.location import Location
from app.db.models.manual_job_listing_url import ManualJobListingUrl
from app.db.models.raw_job_listing import RawJobListing
from app.db.models.taxonomy import (
    Category,
    ContractType,
    DcoTitle,
    EducationLevel,
    Industry,
    job_categories,
    job_contract_types,
    job_education_levels,
    job_industries,
)

__all__ = [
    "Base",
    "Category",
    "Company",
    "ContractType",
    "DcoTitle",
    "EducationLevel",
    "Industry",
    "Job",
    "JobAssessment",
    "JobTranslation",
    "Location",
    "ManualJobListingUrl",
    "RawJobListing",
    "job_categories",
    "job_contract_types",
    "job_education_levels",
    "job_industries",
]
