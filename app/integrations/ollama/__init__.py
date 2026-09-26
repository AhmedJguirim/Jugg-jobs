"""Local Ollama integration. Must be driven through the `llm` queue (ADR-0007)."""

from app.integrations.ollama.client import (
    LlmResult,
    OllamaError,
    OllamaUnavailable,
    chat,
    health,
    list_models,
)

__all__ = [
    "LlmResult",
    "OllamaError",
    "OllamaUnavailable",
    "chat",
    "health",
    "list_models",
]
