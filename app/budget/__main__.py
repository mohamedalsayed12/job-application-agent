"""``python -m app.budget``: show each provider's monthly cap and what has been spent this month.

``python -m app.budget set apify 15`` sets a provider's monthly cap from now on. Older caps are
kept as history; the gate uses the newest one.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import BudgetPolicy, SpendLedger

# D72: what one person searching 8 job titles in one place each day can spend at the price ceilings.
# Twice the titles or places needs about twice the apify and serpapi caps.
RECOMMENDED = {"apify": Decimal("15"), "serpapi": Decimal("12"), "anthropic": Decimal("10")}


def set_cap(session: Session, provider: str, amount: Decimal, *, now: datetime, currency: str = "USD") -> BudgetPolicy:
    row = BudgetPolicy(
        provider=provider, period="month", money_cap=amount, request_cap=None, currency=currency, effective_from=now
    )
    session.add(row)
    session.flush()
    return row


def caps(session: Session, *, now: datetime) -> list[tuple[str, Decimal | None, Decimal]]:
    """Each provider with a cap or spend this month: (provider, newest cap, spent this month)."""
    month = datetime(now.year, now.month, 1, tzinfo=UTC)
    spent = dict(
        session.execute(
            select(SpendLedger.provider, func.sum(func.coalesce(SpendLedger.actual_cost, SpendLedger.estimated_cost)))
            .where(SpendLedger.occurred_at >= month)
            .group_by(SpendLedger.provider)
        ).all()
    )
    newest: dict[str, Decimal | None] = {}
    policies = session.scalars(
        select(BudgetPolicy)
        .where(BudgetPolicy.period == "month", BudgetPolicy.effective_from <= now)
        .order_by(BudgetPolicy.effective_from)
    )
    for row in policies:
        if row.effective_to is None or row.effective_to > now:
            newest[row.provider] = row.money_cap
    return [
        (provider, newest.get(provider), Decimal(spent.get(provider) or 0))
        for provider in sorted(set(newest) | set(spent))
    ]


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.budget", description=__doc__)
    commands = parser.add_subparsers(dest="command")
    setter = commands.add_parser("set", help="set a provider's monthly cap")
    setter.add_argument("provider", help="apify, serpapi, anthropic, or * for all providers together")
    setter.add_argument("amount", help="money allowed each month, for example 15")
    commands.add_parser("recommended", help="set the caps the wider daily search needs (D72)")
    args = parser.parse_args()

    now = datetime.now(UTC)
    with SessionLocal() as session:
        if args.command == "set":
            try:
                amount = Decimal(args.amount)
            except InvalidOperation:
                parser.error("amount must be a number")
            if not amount.is_finite() or amount < 0:
                parser.error("amount must be zero or more")
            set_cap(session, args.provider.strip().lower(), amount, now=now)
            session.commit()
        elif args.command == "recommended":
            for provider, amount in RECOMMENDED.items():
                set_cap(session, provider, amount, now=now)
            session.commit()
        for provider, cap, spent in caps(session, now=now):
            print(f"{provider}: cap {cap if cap is not None else 'not set'}, spent this month {spent:.4f}")


if __name__ == "__main__":
    main()
