"""HTTP routers for the UI-facing API."""

from app.api.board import router as board_router
from app.api.workspace import router as workspace_router

__all__ = ["board_router", "workspace_router"]
