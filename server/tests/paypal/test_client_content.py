import httpx
import pytest

from tests.paypal.helpers import Harness

pytestmark = pytest.mark.anyio

PATH = "/v1/notifications/verify-webhook-signature"


async def test_raw_content_is_sent_byte_for_byte_as_json(harness: Harness) -> None:
    raw = b'{"a": 1,   "b":[1,2,  3]}'
    harness.fake.queue("POST", PATH, httpx.Response(200, json={}))
    async with harness.client() as client:
        await client.request("POST", PATH, content=raw)
    request = harness.fake.calls("POST", PATH)[0]
    assert request.content == raw
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["PayPal-Request-Id"]


async def test_json_and_content_cannot_be_combined(harness: Harness) -> None:
    async with harness.client() as client:
        with pytest.raises(ValueError, match="either json or content"):
            await client.request("POST", PATH, json={"a": 1}, content=b"{}")
