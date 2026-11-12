from collections.abc import AsyncIterator

import httpx
import pytest

from app.agent.llm import LLMAuthError, LLMOutputError, LLMUnavailable
from app.agent.loop import Agent, AgentOutputError
from app.container import Container
from app.db.database import Database
from app.main import create_app
from app.paypal.errors import PayPalUnavailable
from app.schemas.policy import AmountPolicy
from tests.agent.fakes import (
    TODAY,
    FakeModel,
    Rig,
    base_policy,
    invoice_call,
    set_policy,
    tool_call,
)
from tests.db.conftest import StepClock

pytestmark = pytest.mark.anyio


class Harness:
    def __init__(self, db: Database, clock: StepClock, *script: object) -> None:
        self.rig = Rig(db, clock)
        self.model = FakeModel(*script)  # type: ignore[arg-type]
        agent = Agent(self.model, self.rig.service, today=lambda: TODAY)
        self.container = Container(
            database=db, webhooks=None, actions=self.rig.service, agent=agent
        )

    def client(self, container: Container | None = None) -> httpx.AsyncClient:
        app = create_app(container or self.container)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tether")


@pytest.fixture
async def harness(db: Database, clock: StepClock) -> AsyncIterator[Harness]:
    await set_policy(db, base_policy())
    yield Harness(db, clock, invoice_call())


async def test_chat_returns_a_proposal_that_can_be_approved(harness: Harness) -> None:
    async with harness.client() as client:
        chat = await client.post("/chat", json={"message": "Bill Dan 400 for the logo"})
        pending = await client.get("/approvals", params={"status": "pending"})
        action_id = chat.json()["action"]["id"]
        approved = await client.post(f"/approvals/{action_id}/approve")
        ledger = await client.get("/invoices")

    body = chat.json()
    assert chat.status_code == 200
    assert body["kind"] == "proposal"
    assert body["action"]["status"] == "pending"
    assert body["action"]["verdict"] == "needs_approval"
    assert body["action"]["amount"] == "400.00"
    assert [item["id"] for item in pending.json()] == [action_id]
    assert approved.status_code == 200
    assert approved.json()["status"] == "executed"
    (invoice,) = ledger.json()
    assert invoice["status"] == "sent"
    assert invoice["total"] == "400.00"


async def test_chat_without_a_configured_agent_is_unavailable(db: Database) -> None:
    app = create_app(Container(database=db, webhooks=None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://tether") as client:
        response = await client.post("/chat", json={"message": "hello"})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


@pytest.mark.parametrize("message", ["", "x" * 4001])
async def test_chat_validates_the_message_length(harness: Harness, message: str) -> None:
    async with harness.client() as client:
        response = await client.post("/chat", json={"message": message})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LLMUnavailable("overloaded"), 503),
        (LLMAuthError("bad key sk-ant-secret"), 502),
        (LLMOutputError("no tool"), 502),
        (AgentOutputError("invalid twice"), 502),
    ],
)
async def test_model_problems_map_to_gateway_errors_without_leaking_details(
    db: Database, clock: StepClock, error: Exception, status: int
) -> None:
    broken = Harness(db, clock, error, error)
    async with broken.client() as client:
        response = await client.post("/chat", json={"message": "Bill Dan 400"})
    assert response.status_code == status
    assert "sk-ant-secret" not in response.text


async def test_a_clarification_comes_back_as_a_question(db: Database, clock: StepClock) -> None:
    call = tool_call(
        "ask_clarification", question="What is the email?", missing_fields=["client_email"]
    )
    quiet = Harness(db, clock, call)
    async with quiet.client() as client:
        response = await client.post("/chat", json={"message": "Bill Dan 400"})
    assert response.json()["kind"] == "clarification"
    assert response.json()["action"] is None


async def test_approving_an_unknown_action_is_a_404(harness: Harness) -> None:
    async with harness.client() as client:
        response = await client.post("/approvals/999/approve")
    assert response.status_code == 404


async def test_approving_twice_is_a_409(harness: Harness) -> None:
    async with harness.client() as client:
        chat = await client.post("/chat", json={"message": "x"})
        action_id = chat.json()["action"]["id"]
        await client.post(f"/approvals/{action_id}/approve")
        again = await client.post(f"/approvals/{action_id}/approve")
    assert again.status_code == 409
    assert "executed" in again.json()["detail"]


async def test_a_policy_change_turns_an_approval_into_a_409_with_the_rule(
    harness: Harness, db: Database
) -> None:
    async with harness.client() as client:
        chat = await client.post("/chat", json={"message": "x"})
        action_id = chat.json()["action"]["id"]
        await set_policy(db, base_policy(amounts=AmountPolicy(auto_approve_below=10, hard_cap=100)))
        response = await client.post(f"/approvals/{action_id}/approve")
        ledger = await client.get("/invoices")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["rule"] == "amount_over_cap"
    assert "hard cap" in detail["reason"]
    assert ledger.json() == []


async def test_a_paypal_failure_during_approval_is_a_502_and_can_be_retried(
    harness: Harness,
) -> None:
    harness.rig.executor.fail_next("create", PayPalUnavailable("down", status=503))
    async with harness.client() as client:
        chat = await client.post("/chat", json={"message": "x"})
        action_id = chat.json()["action"]["id"]
        failed = await client.post(f"/approvals/{action_id}/approve")
        retried = await client.post(f"/approvals/{action_id}/approve")

    assert failed.status_code == 502
    assert retried.status_code == 200
    assert retried.json()["status"] == "executed"


async def test_rejecting_with_and_without_a_reason(db: Database, clock: StepClock) -> None:
    await set_policy(db, base_policy())
    two = Harness(db, clock, invoice_call(), invoice_call())
    async with two.client() as client:
        first = (await client.post("/chat", json={"message": "a"})).json()["action"]["id"]
        second = (await client.post("/chat", json={"message": "b"})).json()["action"]["id"]
        with_reason = await client.post(
            f"/approvals/{first}/reject", json={"reason": "wrong client"}
        )
        without = await client.post(f"/approvals/{second}/reject")
        repeated = await client.post(f"/approvals/{first}/reject")
        missing = await client.post("/approvals/999/reject")

    assert with_reason.status_code == 200
    assert with_reason.json()["status"] == "rejected"
    assert with_reason.json()["error"] == "wrong client"
    assert without.json()["status"] == "rejected"
    assert repeated.status_code == 409
    assert missing.status_code == 404


async def test_the_approvals_filter_is_validated(harness: Harness) -> None:
    async with harness.client() as client:
        response = await client.get("/approvals", params={"status": "nonsense"})
    assert response.status_code == 422


async def test_the_policy_can_be_read_and_replaced_with_an_audit_entry(harness: Harness) -> None:
    async with harness.client() as client:
        current = (await client.get("/policy")).json()
        changed = {**current, "amounts": {"auto_approve_below": "50", "hard_cap": "2000"}}
        updated = await client.put("/policy", json=changed)
        reread = (await client.get("/policy")).json()
        audit = (await client.get("/audit", params={"limit": 1})).json()

    assert current["version"] == 1
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert reread["amounts"]["hard_cap"] == "2000"
    assert audit[0]["kind"] == "policy_updated"
    assert audit[0]["actor"] == "owner"
    assert audit[0]["detail"] == {"from_version": 1, "to_version": 2}


async def test_an_invalid_policy_is_rejected(harness: Harness) -> None:
    async with harness.client() as client:
        current = (await client.get("/policy")).json()
        broken = {**current, "amounts": {"auto_approve_below": "500", "hard_cap": "100"}}
        response = await client.put("/policy", json=broken)
        unchanged = (await client.get("/policy")).json()
    assert response.status_code == 422
    assert unchanged["version"] == 1


async def test_a_default_policy_is_created_on_first_read(db: Database, clock: StepClock) -> None:
    fresh = Harness(db, clock)
    async with fresh.client() as client:
        policy = (await client.get("/policy")).json()
    assert policy["version"] == 1
    assert policy["contacts"]["mode"] == "allowlist"
    assert policy["plans"]["max_discount_percent"] == 0
