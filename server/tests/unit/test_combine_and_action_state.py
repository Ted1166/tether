import pytest

from app.domain.action_state import ACTION_MACHINE, DECIDABLE, advance_action
from app.domain.errors import IllegalTransition
from app.policy.combine import combine_decisions
from app.schemas.actions import Decision, Verdict
from app.schemas.records import PendingActionStatus

S = PendingActionStatus


def decision(
    verdict: Verdict, rule: str, triggered: tuple[str, ...] = (), version: int = 3
) -> Decision:
    return Decision(
        verdict=verdict,
        rule=rule,
        reason=f"because {rule}",
        triggered_rules=triggered,
        policy_version=version,
    )


def test_the_strictest_decision_wins() -> None:
    combined = combine_decisions(
        [
            decision(Verdict.ALLOW, "within_policy"),
            decision(Verdict.NEEDS_APPROVAL, "approval_required", ("approval_required",)),
            decision(Verdict.BLOCK, "amount_over_cap", ("amount_over_cap",)),
        ]
    )
    assert combined.verdict is Verdict.BLOCK
    assert combined.rule == "amount_over_cap"
    assert combined.reason == "because amount_over_cap"


def test_approval_outranks_allow() -> None:
    combined = combine_decisions(
        [decision(Verdict.ALLOW, "within_policy"), decision(Verdict.NEEDS_APPROVAL, "quiet_hours")]
    )
    assert combined.verdict is Verdict.NEEDS_APPROVAL
    assert combined.rule == "quiet_hours"


def test_all_allow_stays_allow() -> None:
    combined = combine_decisions([decision(Verdict.ALLOW, "within_policy")] * 2)
    assert combined.verdict is Verdict.ALLOW


def test_the_first_of_equally_strict_decisions_is_reported() -> None:
    combined = combine_decisions(
        [decision(Verdict.BLOCK, "first"), decision(Verdict.BLOCK, "second")]
    )
    assert combined.rule == "first"


def test_triggered_rules_are_merged_without_duplicates_in_order() -> None:
    combined = combine_decisions(
        [
            decision(Verdict.NEEDS_APPROVAL, "a", ("a", "b")),
            decision(Verdict.NEEDS_APPROVAL, "b", ("b", "c")),
        ]
    )
    assert combined.triggered_rules == ("a", "b", "c")


def test_the_policy_version_is_carried() -> None:
    assert combine_decisions([decision(Verdict.ALLOW, "x", version=9)]).policy_version == 9


def test_combining_nothing_is_an_error() -> None:
    with pytest.raises(ValueError):
        combine_decisions([])


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PENDING, S.EXECUTED),
        (S.PENDING, S.FAILED),
        (S.PENDING, S.REJECTED),
        (S.PENDING, S.BLOCKED),
        (S.PENDING, S.EXPIRED),
        (S.FAILED, S.EXECUTED),
        (S.FAILED, S.FAILED),
        (S.FAILED, S.REJECTED),
        (S.FAILED, S.BLOCKED),
    ],
)
def test_legal_action_transitions(current: S, target: S) -> None:
    assert advance_action(current, target) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.EXECUTED, S.PENDING),
        (S.EXECUTED, S.FAILED),
        (S.REJECTED, S.EXECUTED),
        (S.BLOCKED, S.EXECUTED),
        (S.EXPIRED, S.EXECUTED),
        (S.FAILED, S.PENDING),
    ],
)
def test_illegal_action_transitions_raise(current: S, target: S) -> None:
    with pytest.raises(IllegalTransition):
        advance_action(current, target)


def test_only_pending_and_failed_actions_can_still_be_decided() -> None:
    assert {S.PENDING, S.FAILED} == DECIDABLE
    assert {s for s in S if ACTION_MACHINE.is_terminal(s)} == {
        S.EXECUTED,
        S.REJECTED,
        S.BLOCKED,
        S.EXPIRED,
    }
    assert ACTION_MACHINE.states == frozenset(S)
