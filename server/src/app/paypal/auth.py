import asyncio
import time
from collections.abc import Callable

import httpx

from app.config import PayPalSettings
from app.paypal.errors import PayPalAuthError, PayPalUnavailable, from_response

EXPIRY_MARGIN_SECONDS = 60.0


class TokenProvider:
    def __init__(
        self,
        settings: PayPalSettings,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    async def get(self) -> str:
        async with self._lock:
            if self._token is not None and self._clock() < self._expires_at:
                return self._token
            return await self._fetch()

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    async def _fetch(self) -> str:
        url = f"{self._settings.base_url.rstrip('/')}/v1/oauth2/token"
        try:
            response = await self._http.post(
                url,
                auth=(self._settings.client_id, self._settings.client_secret.get_secret_value()),
                data={"grant_type": "client_credentials", "scope": self._settings.scope},
                headers={"Accept": "application/json"},
            )
        except httpx.TransportError as error:
            raise PayPalUnavailable("could not reach PayPal to authenticate") from error

        if response.status_code != 200:
            raise self._failure(response)

        try:
            body = response.json()
            token = str(body["access_token"])
            lifetime = float(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError) as error:
            raise PayPalAuthError("PayPal returned an unreadable token response") from error

        self._token = token
        self._expires_at = self._clock() + max(lifetime - EXPIRY_MARGIN_SECONDS, 0.0)
        return token

    def _failure(self, response: httpx.Response) -> Exception:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        error = from_response(response.status_code, payload)
        if response.status_code >= 500 or response.status_code == 429:
            return PayPalUnavailable(
                error.message, status=error.status, name=error.name, debug_id=error.debug_id
            )
        return PayPalAuthError(
            error.message, status=error.status, name=error.name, debug_id=error.debug_id
        )
