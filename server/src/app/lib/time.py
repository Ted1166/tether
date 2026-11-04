from datetime import UTC, date, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def days_overdue(due_date: date | None, today: date) -> int:
    if due_date is None:
        return 0
    return max((today - due_date).days, 0)
