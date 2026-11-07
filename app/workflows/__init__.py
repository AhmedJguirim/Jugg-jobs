"""DBOS workflows. Importing a module registers its workflows with DBOS."""

from app.workflows.llm_queue import APPLICATION_VERSION, LLM_QUEUE, register_queues

__all__ = ["APPLICATION_VERSION", "LLM_QUEUE", "register_queues"]
