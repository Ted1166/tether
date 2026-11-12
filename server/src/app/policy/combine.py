from collections.abc import Sequence

from app.schemas.actions import Decision, Verdict

SEVERITY = {Verdict.ALLOW: 0, Verdict.NEEDS_APPROVAL: 1, Verdict.BLOCK: 2}


def combine_decisions(decisions: Sequence[Decision]) -> Decision:
    if not decisions:
        raise ValueError("there is nothing to combine")

    strictest = max(decisions, key=lambda decision: SEVERITY[decision.verdict])
    triggered: list[str] = []
    for decision in decisions:
        for rule in decision.triggered_rules:
            if rule not in triggered:
                triggered.append(rule)

    return Decision(
        verdict=strictest.verdict,
        rule=strictest.rule,
        reason=strictest.reason,
        triggered_rules=tuple(triggered),
        policy_version=decisions[0].policy_version,
    )
