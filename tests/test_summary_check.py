"""Fact-check used by the AI judge and fit report: numbers, dates and names must appear in the source."""

from __future__ import annotations

import pytest

from app.summaries.check import unsupported

POSTING = (
    "Junior Backend Developer at Acme in Manchester. You will build payment services in Python and "
    "PostgreSQL with a team of 12 engineers. Salary £36,500. Applications close on 3 March 2027."
)
GOOD = "A junior backend role building payment services in Python and PostgreSQL. You join a team of 12 engineers."


@pytest.mark.parametrize(
    ("summary", "problems"),
    [
        (GOOD, []),
        ("The salary is £36500 and you work in Manchester.", []),
        ("The salary is £40,000.", ["40,000"]),
        ("Applications close in April.", ["April"]),
        ("Applications close on 3 March 2027.", []),
        ("You will work with Google and Kubernetes.", ["Google", "Kubernetes"]),
        ("You may need three years of Python.", ["three"]),
        ("Acme's team builds payment services.", []),
        ("Build payment services for Acme.", []),
        ("Leading payment services at Acme.", ["Leading"]),
        ("Key skills are Python and PostgreSQL. Required: Python.", []),
        ("A Manchester-based role building Python-based payment services.", []),
        ("A Leeds-based role.", ["Leeds-based"]),
        ("You build a payment service in Python.", []),
        ("You build payment APIs.", ["APIs"]),
        ("You join a team of twelve engineers.", []),
        ("You join a team of twelve developers.", ["twelve"]),
        ("Applications close on 3 March, three days away.", ["three"]),
    ],
)
def test_a_number_date_or_name_not_in_the_source_is_caught(summary: str, problems: list[str]) -> None:
    assert unsupported(summary, POSTING) == problems


@pytest.mark.parametrize(
    ("posting", "summary", "problems"),
    [
        ("Based in the United Kingdom.", "A UK role.", []),
        ("A UK role.", "Based in the United Kingdom.", []),
        ("You will own continuous integration and deployment.", "You own CI pipelines.", []),
        ("Trade foreign exchange products.", "You build FX products.", []),
        ("Run services on K8s.", "You run Kubernetes services.", []),
        ("Come and join us in London.", "Join the United States team.", ["United", "States"]),
        ("A UK role.", "A role in Taiwan.", ["Taiwan"]),
        ("Strong English-language skills and Git-based workflows.", "You need English and Git.", []),
        ("Datastores, especially Mongo.", "You work with MongoDB.", []),
    ],
)
def test_names_the_source_writes_another_way_are_accepted(posting: str, summary: str, problems: list[str]) -> None:
    assert unsupported(summary, posting) == problems
