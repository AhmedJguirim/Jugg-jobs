"""Application settings.

Configuration comes from environment variables (optionally via a .env file) so
that nothing environment-specific is hard-coded and credentials stay out of the
source tree. See .env.example for the expected keys.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- database ---
    postgres_user: str = Field(default="postgres")
    postgres_password: str = Field(default="postgres")
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="jugg_jobs")
    postgres_test_db: str = Field(default="jugg_jobs_test")

    # Set DATABASE_URL to override the assembled URL entirely (e.g. in CI).
    database_url: str | None = Field(default=None)

    # --- app ---
    log_level: str = Field(default="INFO")
    sql_echo: bool = Field(default=False)

    # --- local LLM (Ollama) ---
    # One GPU, one model at a time: enforced by the `llm` DBOS queue's
    # concurrency of 1, not by these settings. See ADR-0007.
    ollama_base_url: str = Field(default="http://localhost:11434")
    llm_model: str = Field(default="qwen3.5:4b")
    # Generous: a cold model load plus a long posting can take minutes.
    llm_timeout_seconds: float = Field(default=600.0)
    # 8192 comfortably fits a long cleaned posting, the profile and the reply;
    # the model supports far more but larger contexts cost VRAM.
    llm_num_ctx: int = Field(default=8192)
    # Keep the model resident between queued jobs so each one does not reload it.
    llm_keep_alive: str = Field(default="10m")
    # Which scoring profile to use when none is named.
    llm_default_profile: str = Field(default="default")

    # --- GPU cooldown ---
    # Checked before each vacancy that needs the GPU: above this temperature
    # (Celsius, as nvidia-smi reports it) the queue pauses before the next one.
    gpu_max_temp_c: int = Field(default=80)
    gpu_cooldown_seconds: int = Field(default=180)
    # Give up waiting after this many pauses and carry on, so a stuck sensor
    # cannot stall the queue forever.
    gpu_max_cooldowns: int = Field(default=5)

    def _url_for(self, database: str) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{database}"
        )

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or self._url_for(self.postgres_db)

    @property
    def sqlalchemy_test_url(self) -> str:
        return self._url_for(self.postgres_test_db)


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance. Call `get_settings.cache_clear()` in tests."""
    return Settings()
