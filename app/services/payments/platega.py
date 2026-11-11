"""Platega: SBP/QR. Ported from NotSpy (production).

Auth both ways is the header pair X-MerchantId / X-Secret (no body signature), so the
callback amount is verified against our record (verifies_amount). The status endpoint needs
the same headers; lost callbacks are recovered by the reconcile cron.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

import aiohttp
import structlog

from app.services.payments.ports import (
    CreatedPayment,
    PaymentProvider,
    ProviderError,
    WebhookVerdict,
)

logger = structlog.get_logger()

PLATEGA_API = "https://app.platega.io"
_CONFIRMED = "CONFIRMED"
_FAILED = {"CANCELED", "CANCELLED", "CHARGEBACKED"}


class Platega(PaymentProvider):
    name = "platega"
    label = "🏦 СБП / QR"
    verifies_amount = True
    can_poll = True

    def __init__(  # noqa: PLR0913 - mirrors provider settings
        self, merchant_id: str, api_key: str, method_code: int, return_url: str, min_amount: int
    ) -> None:
        self._merchant_id = merchant_id
        self._api_key = api_key
        self._method_code = method_code
        self._return_url = return_url
        self.min_amount = min_amount
        self._session: aiohttp.ClientSession | None = None

    def verify_credentials(self, merchant_id: str | None, secret: str | None) -> bool:
        return hmac.compare_digest(merchant_id or "", self._merchant_id) and hmac.compare_digest(
            secret or "", self._api_key
        )

    async def create_payment(self, payment_id: int, tg_id: int, amount: int) -> CreatedPayment:
        if amount < self.min_amount:
            raise ProviderError(self.name, f"amount {amount} < min {self.min_amount}")
        body = {
            "paymentMethod": self._method_code,
            "paymentDetails": {"amount": amount, "currency": "RUB"},
            "description": f"Credits {payment_id}",
            "return": f"{self._return_url}/pay/success",
            "failedUrl": f"{self._return_url}/pay/fail",
            "payload": str(payment_id),
        }
        status, payload = await self._request("POST", "/transaction/process", body)
        if status not in (200, 201):
            raise ProviderError(self.name, f"API {status}: {str(payload)[:300]}")
        transaction_id = transaction_id_of(payload)
        url = str(payload.get("redirect") or payload.get("return") or "")
        if not transaction_id or not url:
            raise ProviderError(self.name, f"no payment URL: {str(payload)[:300]}")
        logger.info("platega_created", payment_id=payment_id, transaction=transaction_id)
        return CreatedPayment(transaction_id, url)

    async def verify_webhook(self, payload: dict[str, Any]) -> WebhookVerdict:
        """Headers are checked in the HTTP layer (verify_credentials); this parses the body."""
        status = str(payload.get("status") or "").upper()
        internal: int | None = None
        try:
            internal = int(str(payload.get("payload")))
        except (TypeError, ValueError):
            internal = None
        return WebhookVerdict(
            provider_payment_id=transaction_id_of(payload),
            amount=amount_of(payload),
            is_success=status == _CONFIRMED,
            is_final=status == _CONFIRMED or status in _FAILED,
            internal_payment_id=internal,
            raw=payload,
        )

    async def fetch_status(self, provider_payment_id: str) -> WebhookVerdict | None:
        if not provider_payment_id:
            return None
        status, payload = await self._request("GET", f"/transaction/{provider_payment_id}")
        if status != 200:
            if status != 404:
                logger.warning(
                    "platega_status_failed", http=status, transaction=provider_payment_id
                )
            return None
        payload.setdefault("id", provider_payment_id)
        return await self.verify_webhook(payload)

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Retries network errors and 5xx only; a 4xx is the provider's final answer."""
        headers = {"X-MerchantId": self._merchant_id, "X-Secret": self._api_key}
        session = self._get_session()
        last: Exception | None = None
        for attempt in range(3):
            try:
                async with session.request(
                    method, PLATEGA_API + path, json=body, headers=headers
                ) as r:
                    if r.status >= 500 and attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                    try:
                        data = await r.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        data = {"raw": await r.text()}
                    return r.status, data if isinstance(data, dict) else {"raw": data}
            except (aiohttp.ClientError, TimeoutError) as exc:
                last = exc
                await asyncio.sleep(2**attempt)
        raise ProviderError(self.name, f"network error: {last}")

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def transaction_id_of(payload: dict[str, Any]) -> str:
    """The callback uses "id", the create response "transactionId"."""
    return str(payload.get("id") or payload.get("transactionId") or "")


def amount_of(payload: dict[str, Any]) -> int:
    """Top-level (callback), nested object, or a string like "51.21 RUB" (create response)."""
    raw = payload.get("amount")
    if raw is None:
        details = payload.get("paymentDetails")
        if isinstance(details, dict):
            raw = details.get("amount")
        elif isinstance(details, str) and details.split():
            raw = details.split()[0]
    if isinstance(raw, str) and raw.split():
        raw = raw.split()[0]  # "299.00 RUB" may appear at any level
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return 0
