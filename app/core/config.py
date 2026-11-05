from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://jaa:jaa@localhost:5432/jaa"
    admin_key: str = "change-me"

    fetch_user_agent: str = (
        "JobApplicationAgent/0.1 (+https://github.com/mohamedalsayed12/job-application-agent)"
    )
    api_allowlist: str = (
        "api.greenhouse.io,boards-api.greenhouse.io,"
        "api.lever.co,eu.lever.co,"
        "www.reed.co.uk,api.reed.co.uk,"
        "api.adzuna.com,api.adzuna.co.uk"
    )

    # Conservative placeholders until V3 / B4; operators override via env.
    search_request_unit_price: Decimal = Decimal("0.005")
    ai_extraction_unit_price: Decimal = Decimal("0.002")
    ai_summary_unit_price: Decimal = Decimal("0.001")

    @field_validator(
        "search_request_unit_price",
        "ai_extraction_unit_price",
        "ai_summary_unit_price",
        mode="before",
    )
    @classmethod
    def _decimal(cls, value: object) -> object:
        if isinstance(value, str):
            return Decimal(value)
        return value

    def api_allowlist_hosts(self) -> frozenset[str]:
        return frozenset(
            host.strip().lower()
            for host in self.api_allowlist.split(",")
            if host.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
