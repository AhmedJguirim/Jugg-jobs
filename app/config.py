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
    postgres_db: str = Field(default="nljobs")
    postgres_test_db: str = Field(default="nljobs_test")

    # Set DATABASE_URL to override the assembled URL entirely (e.g. in CI).
    database_url: str | None = Field(default=None)

    # --- app ---
    log_level: str = Field(default="INFO")
    sql_echo: bool = Field(default=False)

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
