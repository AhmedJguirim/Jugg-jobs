"""Model registry.

Importing this package imports every model, which is what populates
`Base.metadata`. Alembic's env.py imports this module so autogenerate sees the
full schema; add each new model here.
"""

from app.db.base import Base
from app.db.models.company import Company

__all__ = ["Base", "Company"]
