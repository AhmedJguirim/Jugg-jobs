"""Transfer objects for raw job listings (the workshop section)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RawJobListingCreate(BaseModel):
    # Only the link is trimmed. The body is stored exactly as pasted, which is
    # the point of keeping it raw.
    url: str = Field(min_length=1)
    html_body: str = Field(min_length=1)

    model_config = ConfigDict(str_strip_whitespace=False)


class RawJobListingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    html_body: str
    created_at: datetime
