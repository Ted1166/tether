from app.policy.rules import ALL_RULES, Finding
from app.schemas.actions import ActionContext, Decision, ProposedAction, Verdict
from app.schemas.policy import Policy


def evaluate(action: ProposedAction, policy: Policy, context: ActionContext) -> Decision:
    findings = [
        finding for rule in ALL_RULES if (finding := rule(action, policy, context)) is not None
    ]
    triggered = tuple(finding.rule for finding in findings)

    blocks = [finding for finding in findings if finding.verdict is Verdict.BLOCK]
    if blocks:
        return _decision(blocks[0], triggered, policy)
    if findings:
        return _decision(findings[0], triggered, policy)

    return Decision(
        verdict=Verdict.ALLOW,
        rule="within_policy",
        reason="every limit in your policy is satisfied",
        triggered_rules=(),
        policy_version=policy.version,
    )


def _decision(finding: Finding, triggered: tuple[str, ...], policy: Policy) -> Decision:
    return Decision(
        verdict=finding.verdict,
        rule=finding.rule,
        reason=finding.reason,
        triggered_rules=triggered,
        policy_version=policy.version,
    )
