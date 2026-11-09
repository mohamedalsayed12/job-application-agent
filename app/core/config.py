from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_AI_MODELS = {
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-4o-mini",
}
MIN_SESSION_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Application settings loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://jaa:jaa@localhost:5432/jaa"
    admin_key: str = "change-me"
    session_secret: str = ""
    # Local development over plain http needs this false, or the browser drops the cookie.
    session_cookie_secure: bool = True

    fetch_user_agent: str = (
        "JobApplicationAgent/0.1 (+https://github.com/mohamedalsayed12/job-application-agent)"
    )
    api_allowlist: str = (
        "api.greenhouse.io,boards-api.greenhouse.io,"
        "api.lever.co,api.eu.lever.co,jobs.lever.co,jobs.eu.lever.co,"
        "www.reed.co.uk,api.reed.co.uk,"
        "api.adzuna.com,api.adzuna.co.uk,"
        "serpapi.com,"
        "api.mojeek.com,"
        "api.openai.com,"
        "api.anthropic.com"
    )

    # D29: web search provider id is a setting (default SerpAPI free tier).
    web_search_provider: str = "serpapi"
    serpapi_api_key: str = ""
    mojeek_api_key: str = ""
    reed_api_key: str = ""
    adzuna_enabled: bool = False

    # D30: leftover AI extraction (Anthropic Claude Haiku 5.5 + JSON Schema outputs).
    # AI_PROVIDER=openai keeps the gpt-4o-mini client. Blank model = provider default.
    ai_provider: str = "anthropic"
    ai_extraction_model: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # B4 seed: Haiku 5.5 ≈ $0.00035 per leftover batch (1.5k in × $0.10/M + 0.4k out × $0.50/M);
    # the £0.0005 unit price below stays a conservative ceiling for either provider.
    # Step 10 audit C5: 500 output tokens truncated batches (stop_reason max_tokens).
    # The per-call cap covers input + output, so it rises with the output limit.
    ai_extraction_per_item_token_cap: int = 4000
    ai_extraction_max_output_tokens: int = 1500

    # Paid Mojeek Business fallback £3/1k ⇒ £0.003; SerpAPI free tier is £0 via FREE_MONEY.
    search_request_unit_price: Decimal = Decimal("0.003")
    ai_extraction_unit_price: Decimal = Decimal("0.0005")
    ai_summary_unit_price: Decimal = Decimal("0.001")

    # D41: job summaries. Blank model = provider default. Caps cover input + output tokens.
    ai_summary_model: str = ""
    ai_summary_max_output_tokens: int = 300
    ai_summary_per_item_token_cap: int = 8000
    ai_summary_daily_token_cap: int = 300000

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

    def web_problems(self) -> list[str]:
        """Why the web app must not start. The worker and tools do not need these."""
        problems = []
        if len(self.session_secret) < MIN_SESSION_SECRET_LENGTH:
            problems.append(f"SESSION_SECRET must be at least {MIN_SESSION_SECRET_LENGTH} characters")
        if self.admin_key in ("", "change-me"):
            problems.append("ADMIN_KEY must be set to a private value")
        return problems

    def resolved_ai_extraction_model(self) -> str:
        explicit = self.ai_extraction_model.strip()
        if explicit:
            return explicit
        return _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def resolved_ai_summary_model(self) -> str:
        return self.ai_summary_model.strip() or _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def api_allowlist_hosts(self) -> frozenset[str]:
        return frozenset(
            host.strip().lower()
            for host in self.api_allowlist.split(",")
            if host.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
