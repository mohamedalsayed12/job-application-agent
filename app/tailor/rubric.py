"""UK grad tech keyword rubric derived from six graduate listings."""

from __future__ import annotations

from typing import Final

# bucket_id -> display label
RUBRIC_BUCKETS: Final[dict[str, str]] = {
    "coding": "Coding / programming",
    "python": "Python",
    "apps_apis_tools": "Applications, APIs, platforms and developer tools",
    "cloud_devops": "Cloud, containers and DevOps",
    "testing_quality": "Testing, debugging and quality",
    "vcs_ci": "Version control, code review and CI/CD",
    "lifecycle": "Software lifecycle and maintenance",
    "security_reliability": "Security and reliability",
    "teamwork": "Teamwork and collaboration",
    "communication": "Communication",
    "client_focus": "Client, customer, user or business focus",
    "problem_solving": "Problem-solving",
}

RUBRIC_IDS: Final[tuple[str, ...]] = tuple(RUBRIC_BUCKETS)

PRIORITIES: Final[tuple[str, ...]] = ("required", "preferred", "nice")
