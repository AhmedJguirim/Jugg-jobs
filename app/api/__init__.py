"""HTTP routers for the UI-facing API."""

from app.api.board import router as board_router
from app.api.companies import router as companies_router
from app.api.jobs_test import router as jobs_test_router
from app.api.workspace import router as workspace_router

__all__ = ["board_router", "companies_router", "jobs_test_router", "workspace_router"]
