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
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://jaa:jaa@localhost:5436/jaa"
    admin_key: str = "change-me"
    session_secret: str = ""
    # Local development over plain http needs this false, or the browser drops the cookie.
    session_cookie_secure: bool = True
    # Links in account emails start with this, never with the request's Host header (which a sender can forge).
    public_base_url: str = "http://localhost:8000"

    # Account emails (verify, reset). "smtp" sends; "resend" uses Resend's HTTPS API (SMTP_PASSWORD =
    # API key); "memory" keeps them in a list for tests and never sends.
    mail_backend: str = "smtp"
    mail_from: str = "Job Agent <no-reply@localhost>"
    # Defaults reach Mailpit from docker-compose.yml, which catches mail locally and sends nothing on.
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    # "starttls", "ssl" or "none". Use "none" only for a local catcher such as Mailpit.
    smtp_security: str = "none"
    smtp_timeout_seconds: int = 10

    fetch_user_agent: str = (
        "JobApplicationAgent/0.1 (+https://github.com/mohamedalsayed12/job-application-agent)"
    )
    api_allowlist: str = (
        "api.apify.com,api.anthropic.com,"
        "oauth2.googleapis.com,gmail.googleapis.com,www.googleapis.com,serpapi.com"
    )

    # D54: LinkedIn jobs through the Black Falcon Data scraper on Apify.
    apify_api_token: str = ""
    linkedin_actor: str = "blackfalcondata/linkedin-job-scraper"
    linkedin_max_results: int = 100
    linkedin_every_hours: int = 24
    # Daily searches cover the last 3 days, so a missed day or two is caught up.
    linkedin_posted_within_days: int = 3
    # D72: LinkedIn experience levels searched (2 entry level, 3 associate). The AI judge still decides the level.
    linkedin_levels: str = "2,3"
    # D72: the first time a role and location is searched, on any source, it looks this far back.
    search_first_posted_within_days: int = 14
    # D72: a search that failed is tried again after this long, not after a whole day.
    search_retry_hours: int = 1
    linkedin_stale_days: int = 14
    linkedin_wait_minutes: int = 15
    # The typed titles and then their related titles, searched in each location.
    linkedin_titles_per_person: int = 10
    # The scraper charges $0.00027 a job and $0.0005 a run, and Apify adds compute; these ceilings cover all three.
    linkedin_unit_price: Decimal = Decimal("0.001")
    linkedin_run_price: Decimal = Decimal("0.02")
    usd_to_gbp: Decimal = Decimal("0.80")

    # Indeed through valig/indeed-jobs-scraper (~$0.07–0.10 / 1k results); ceilings cover event + compute.
    indeed_actor: str = "valig/indeed-jobs-scraper"
    indeed_max_results: int = 50
    indeed_every_hours: int = 24
    indeed_posted_within_days: int = 3
    indeed_stale_days: int = 14
    indeed_wait_minutes: int = 15
    indeed_country: str = "uk"  # actor enum uses uk, not ISO gb
    indeed_unit_price: Decimal = Decimal("0.0002")
    indeed_run_price: Decimal = Decimal("0.01")

    # D73: Google Jobs through SerpAPI (never fetch google.com; serpapi.com is allowlisted).
    serpapi_api_key: str = ""
    google_jobs_max_results: int = 10  # one SerpAPI page
    google_jobs_every_hours: int = 24
    google_jobs_stale_days: int = 14
    google_jobs_gl: str = "uk"
    # SerpAPI bills per search (~$0.015); settle the ceiling because the response has no cost field.
    google_jobs_unit_price: Decimal = Decimal("0")
    google_jobs_run_price: Decimal = Decimal("0.012")
    # D69: jobs on employers' own sites, found through Google search on SerpAPI. Same key as Google Jobs.
    careers_every_hours: int = 24
    # Google pages read per role and location; the next page is read only while too few jobs were found.
    careers_max_pages: int = 3
    careers_enough_jobs: int = 5
    # Company pages opened per role and location, and job links followed from one careers page.
    careers_max_visits: int = 20
    careers_links_per_site: int = 3
    careers_max_age_days: int = 30
    careers_stale_days: int = 14
    # SerpAPI's plans are about $0.01-0.025 a search; this ceiling is per Google page.
    careers_search_price: Decimal = Decimal("0.02")

    # Apply fill: matching form questions the patterns miss to the person's own answers or CV text.
    ai_apply_map_unit_price: Decimal = Decimal("0.002")
    ai_apply_map_model: str = ""
    ai_apply_map_max_output_tokens: int = 800

    # Outreach: people discovery and profile research through Apify (still never fetch linkedin.com here).
    apify_outreach_hm_actor: str = "automation_studio/linkedin-job-hiring-manager-finder"
    apify_outreach_employees_actor: str = "harvestapi/linkedin-company-employees"
    apify_outreach_profile_actor: str = "harvestapi/linkedin-profile"
    outreach_max_contacts: int = 8
    outreach_people_unit_price: Decimal = Decimal("0.04")  # Apify refuses runs capped under $0.05.
    outreach_profile_unit_price: Decimal = Decimal("0.01")
    outreach_ai_unit_price: Decimal = Decimal("0.002")
    outreach_daily_send_cap: int = 20
    outreach_gmail_dry_run: bool = True

    # Gmail OAuth (send only after the person connects and approves a draft).
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_redirect_uri: str = "http://localhost:8000/account/gmail/callback"
    gmail_token_key: str = ""

    # Interview intel: public Reddit, YouTube and LinkedIn posts through Apify (no Instagram/TikTok in v1).
    interview_reddit_actor: str = "harshmaur/reddit-search-scraper"
    interview_youtube_actor: str = "streamers/youtube-scraper"
    interview_linkedin_actor: str = "apimaestro/linkedin-posts-search-scraper-no-cookies"
    interview_max_per_platform: int = 25
    # Keep this short: interview collection runs inside the only worker cycle.
    interview_wait_minutes: int = 5
    interview_ttl_days: int = 7
    interview_per_cycle: int = 1
    interview_collect_unit_price: Decimal = Decimal("0.002")
    interview_collect_run_price: Decimal = Decimal("0.03")
    interview_analyse_unit_price: Decimal = Decimal("0.002")
    interview_plan_unit_price: Decimal = Decimal("0.003")
    interview_tailor_unit_price: Decimal = Decimal("0.003")
    interview_analyse_model: str = ""
    interview_plan_model: str = ""
    interview_analyse_max_output_tokens: int = 2000
    interview_plan_max_output_tokens: int = 2500
    interview_analyse_daily_token_cap: int = 1000000
    interview_plan_daily_token_cap: int = 500000
    interview_snippet_chars: int = 2000

    # Blank model = provider default.
    ai_provider: str = "anthropic"
    anthropic_api_key: str = ""

    ai_summary_unit_price: Decimal = Decimal("0.001")
    # D43: about 6k tokens in and 0.5k out on Haiku 5.5 is under £0.002; this is a ceiling.
    ai_cv_unit_price: Decimal = Decimal("0.005")
    # D72: one job title in, up to 9 related titles out; a few hundred tokens on Haiku.
    ai_related_titles_unit_price: Decimal = Decimal("0.001")
    ai_related_titles_max_output_tokens: int = 300
    # D51: about 3.5k tokens in and 0.2k out on Haiku 5.5, ~$0.00045 at the prices above; this is a ceiling.
    ai_judge_unit_price: Decimal = Decimal("0.0005")
    # D52: about 6k tokens in (advert and CV) and 1.5k out, ~$0.0014 at the prices above; this is a ceiling.
    ai_report_unit_price: Decimal = Decimal("0.0015")
    # D55: keyword extract / tailor / ATS revise each reserve this ceiling once.
    ai_tailor_unit_price: Decimal = Decimal("0.003")
    # About 300 tokens in and 100 out on Haiku 5.5 for one title; this is a ceiling.
    # D64: about 0.5k tokens in and 0.4k out on Haiku 5.5, well under £0.001; this is a ceiling.
    ai_ideas_unit_price: Decimal = Decimal("0.002")

    # D41: job summaries. Blank model = provider default. Caps cover input + output tokens.
    ai_summary_model: str = ""
    ai_summary_max_output_tokens: int = 300
    ai_summary_per_item_token_cap: int = 8000
    ai_summary_daily_token_cap: int = 300000

    # D43: reading a CV to fill the profile form. Blank model = provider default.
    ai_cv_model: str = ""
    ai_cv_max_output_tokens: int = 1500
    ai_cv_max_chars: int = 24000
    # D64: suggesting roles and places to search uses the CV model.
    ai_ideas_max_output_tokens: int = 800

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

    # D55: tailored CV for Apply / Consider jobs. Blank model = provider default.
    ai_tailor_model: str = ""
    ai_tailor_max_output_tokens: int = 8000
    ai_tailor_cv_chars: int = 12000
    ai_tailor_per_cycle: int = 5

    @field_validator("database_url", mode="before")
    @classmethod
    def _psycopg_url(cls, value: object) -> object:
        # Hosts such as Railway hand out postgresql://; SQLAlchemy needs the psycopg driver name.
        if isinstance(value, str) and value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value

    @field_validator(
        "linkedin_unit_price",
        "linkedin_run_price",
        "indeed_unit_price",
        "indeed_run_price",
        "google_jobs_unit_price",
        "google_jobs_run_price",
        "careers_search_price",
        "usd_to_gbp",
        "ai_apply_map_unit_price",
        "interview_collect_unit_price",
        "interview_collect_run_price",
        "interview_analyse_unit_price",
        "interview_plan_unit_price",
        "interview_tailor_unit_price",
        "ai_summary_unit_price",
        "ai_cv_unit_price",
        "ai_related_titles_unit_price",
        "ai_judge_unit_price",
        "ai_report_unit_price",
        "ai_tailor_unit_price",
        "outreach_people_unit_price",
        "outreach_profile_unit_price",
        "outreach_ai_unit_price",
        "ai_ideas_unit_price",
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
        if self.mail_backend not in ("smtp", "resend", "memory"):
            problems.append("MAIL_BACKEND must be smtp, resend or memory")
        if self.smtp_security not in ("starttls", "ssl", "none"):
            problems.append("SMTP_SECURITY must be starttls, ssl or none")
        if not self.public_base_url.startswith(("https://", "http://")):
            problems.append("PUBLIC_BASE_URL must start with https:// or http://")
        if self.session_cookie_secure and self.public_base_url.startswith("http://") and "localhost" not in self.public_base_url:
            problems.append("PUBLIC_BASE_URL must use https:// when SESSION_COOKIE_SECURE is true")
        return problems

    def resolved_model(self, override: str) -> str:
        return override.strip() or _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def ai_applicants(self) -> set[uuid.UUID] | None:
        """The people the AI works for, or None for everyone."""
        ids = {uuid.UUID(part.strip()) for part in self.ai_applicant_ids.split(",") if part.strip()}
        return ids or None

    def resolved_ai_report_model(self) -> str:
        return self.resolved_model(self.ai_report_model)

    def resolved_ai_cv_model(self) -> str:
        return self.resolved_model(self.ai_cv_model)

    def resolved_ai_tailor_model(self) -> str:
        return self.resolved_model(self.ai_tailor_model)

    def resolved_ai_outreach_model(self) -> str:
        return self.resolved_model(self.ai_summary_model)

    def resolved_ai_apply_map_model(self) -> str:
        return self.ai_apply_map_model.strip() or _DEFAULT_AI_MODELS.get(self.ai_provider.strip().lower(), "")

    def gmail_seal_secret(self) -> str:
        return (self.gmail_token_key or self.session_secret).strip()

    def resolved_interview_analyse_model(self) -> str:
        return self.resolved_model(self.interview_analyse_model)

    def resolved_interview_plan_model(self) -> str:
        return self.resolved_model(self.interview_plan_model)

    def api_allowlist_hosts(self) -> frozenset[str]:
        return frozenset(
            host.strip().lower()
            for host in self.api_allowlist.split(",")
            if host.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
