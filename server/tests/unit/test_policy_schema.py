from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.policy.defaults import starter_policy
from app.schemas.actions import ActionContext, ActionKind
from app.schemas.policy import AmountPolicy, ContactPolicy, Policy, QuietHours
from tests.factories import make_action


def test_default_policy_is_valid_and_cautious() -> None:
    policy = Policy()
    assert policy.plans.max_discount_percent == 0
    assert ActionKind.CANCEL_INVOICE in policy.disabled_actions
    assert policy.auto_actions == frozenset({ActionKind.CREATE_INVOICE})


def test_overlapping_auto_and_disabled_actions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Policy(
            auto_actions=frozenset({ActionKind.SEND_REMINDER}),
            disabled_actions=frozenset({ActionKind.SEND_REMINDER}),
        )


def test_hard_cap_cannot_sit_below_auto_approve_threshold() -> None:
    with pytest.raises(ValidationError):
        AmountPolicy(auto_approve_below=Decimal("500"), hard_cap=Decimal("100"))


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        QuietHours(timezone="Mars/Olympus")


def test_policy_emails_are_normalized() -> None:
    contacts = ContactPolicy(allowed=frozenset({"  Dan@Example.COM "}))
    assert contacts.allowed == frozenset({"dan@example.com"})


def test_invalid_policy_email_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ContactPolicy(allowed=frozenset({"not-an-email"}))


def test_action_email_is_normalized() -> None:
    assert make_action(client_email=" MAYA@Example.com").client_email == "maya@example.com"


def test_negative_amount_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_action(amount="-5")


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ActionContext(now=datetime(2026, 11, 4, 12, 0))


def test_policy_round_trips_through_json() -> None:
    original = starter_policy(["dan@example.com"], timezone="Africa/Nairobi")
    restored = Policy.model_validate_json(original.model_dump_json())
    assert restored == original


def test_starter_policy_carries_contacts_and_timezone() -> None:
    policy = starter_policy(["Dan@example.com"], timezone="Africa/Nairobi")
    assert policy.contacts.allowed == frozenset({"dan@example.com"})
    assert policy.quiet_hours.timezone == "Africa/Nairobi"
