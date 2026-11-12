from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import AuditRepository, ClientRepository, Clock, PolicyRepository
from app.lib.time import utc_now
from app.policy.defaults import starter_policy
from app.schemas.policy import Policy
from app.schemas.records import AuditActor, NewAuditEntry


async def load_policy(session: AsyncSession, clock: Clock = utc_now) -> Policy:
    repository = PolicyRepository(session, clock)
    policy = await repository.current()
    if policy is not None:
        return policy

    clients = await ClientRepository(session, clock).all()
    emails = [client.email for client in clients]
    policy = await repository.save(starter_policy(emails))
    await AuditRepository(session, clock).append(
        NewAuditEntry(
            actor=AuditActor.SYSTEM,
            kind="policy_created",
            policy_version=policy.version,
            detail={"allowed_contacts": emails},
        )
    )
    return policy


async def replace_policy(
    session: AsyncSession, policy: Policy, actor: AuditActor, clock: Clock = utc_now
) -> Policy:
    previous = await load_policy(session, clock)
    saved = await PolicyRepository(session, clock).save(policy)
    await AuditRepository(session, clock).append(
        NewAuditEntry(
            actor=actor,
            kind="policy_updated",
            policy_version=saved.version,
            detail={"from_version": previous.version, "to_version": saved.version},
        )
    )
    return saved
