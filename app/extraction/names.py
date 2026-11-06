"""Company-name cleaning — re-export from queue (shared with dedupe)."""

from app.queue.names import clean_company_name

__all__ = ["clean_company_name"]
