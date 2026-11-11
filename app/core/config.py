import uuid
from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_AI_MODELS = {
    "anthropic": "claude-haiku-5-5",
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
    api_allowlist: str = "api.apify.com,api.anthropic.com"

    # D54: LinkedIn jobs through the Black Falcon Data scraper on Apify.
    apify_api_token: str = ""
    linkedin_actor: str = "blackfalcondata/linkedin-job-scraper"
    linkedin_max_results: int = 50
    linkedin_every_hours: int = 24
    # Daily searches cover the last 3 days, so a missed day or two is caught up.
    linkedin_posted_within_days: int = 3
    linkedin_stale_days: int = 14
    linkedin_wait_minutes: int = 15
    # The scraper charges $0.00027 a job and $0.0005 a run, and Apify adds compute; these ceilings cover all three.
    linkedin_unit_price: Decimal = Decimal("0.001")
    linkedin_run_price: Decimal = Decimal("0.02")
    usd_to_gbp: Decimal = Decimal("0.80")

    # Blank model = provider default.
    ai_provider: str = "anthropic"
    anthropic_api_key: str = ""

    ai_summary_unit_price: Decimal = Decimal("0.001")
    # D43: about 6k tokens in and 0.5k out on Haiku 5.5 is under £0.002; this is a ceiling.
    ai_cv_unit_price: Decimal = Decimal("0.005")
    # D51: about 3.5k tokens in and 0.2k out on Haiku 5.5, ~$0.00045 at the prices above; this is a ceiling.
    ai_judge_unit_price: Decimal = Decimal("0.0005")
    # D52: about 6k tokens in (advert and CV) and 1.5k out, ~$0.0014 at the prices above; this is a ceiling.
    ai_report_unit_price: Decimal = Decimal("0.0015")

    # D41: job summaries. Blank model = provider default. Caps cover input + output tokens.
    ai_summary_model: str = ""
    ai_summary_max_output_tokens: int = 300
    ai_summary_per_item_token_cap: int = 8000
    ai_summary_daily_token_cap: int = 300000

    # D43: reading a CV to fill the profile form. Blank model = provider default.
    ai_cv_model: str = ""
    ai_cv_max_output_tokens: int = 1500
    ai_cv_max_chars: int = 24000

    # D50: the AI judging whether each job fits each person. Blank model = provider default.
    ai_judge_model: str = ""
    ai_judge_max_output_tokens: int = 400
    # About 1,000 verdicts a day at ~4k tokens each; the money cap still bounds the month.
    ai_judge_daily_token_cap: int = 4000000
    # Calls sent to the AI at once; spending and saving stay one at a time.
    ai_judge_concurrency: int = 5
    # Work per worker cycle, so matching updates the page every few minutes instead of after the whole backlog.
    ai_judge_per_cycle: int = 100
    ai_report_per_cycle: int = 20
    # Operator request (2026-11-10): comma-separated applicant ids the AI judges and reports for. Blank = everyone.
    ai_applicant_ids: str = ""

    # D52: the full fit report for jobs scoring 3.0 or more. Blank model = provider default.
    ai_report_model: str = ""
    ai_report_max_output_tokens: int = 3000
    ai_report_daily_token_cap: int = 3000000
    ai_report_cv_chars: int = 12000

    @field_validator(
        "linkedin_unit_price",
        "linkedin_run_price",
        "usd_to_gbp",
        "ai_summary_unit_price",
        "ai_cv_unit_price",
        "ai_judge_unit_price",
        "ai_report_unit_price",
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

    def resolved_ai_summary_model(self) -> str:
        return self.ai_summary_model.strip() or _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def resolved_ai_judge_model(self) -> str:
        return self.ai_judge_model.strip() or _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def ai_applicants(self) -> set[uuid.UUID] | None:
        """The people the AI works for, or None for everyone."""
        ids = {uuid.UUID(part.strip()) for part in self.ai_applicant_ids.split(",") if part.strip()}
        return ids or None

    def resolved_ai_report_model(self) -> str:
        return self.ai_report_model.strip() or _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def resolved_ai_cv_model(self) -> str:
        return self.ai_cv_model.strip() or _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def api_allowlist_hosts(self) -> frozenset[str]:
        return frozenset(
            host.strip().lower()
            for host in self.api_allowlist.split(",")
            if host.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
