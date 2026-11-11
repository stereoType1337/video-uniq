"""Kassa AI (ex-FreeKassa): SBP and cards. Ported from NotSpy, which runs it in production.

Request: HMAC-SHA256(api_key, values sorted by key joined with "|").
Webhook: form-encoded; MD5("shop_id:AMOUNT:secret_key2:MERCHANT_ORDER_ID") == SIGN.
CUR_ID selects the method: 44 -> SBP, 36 -> cards. Probe without SIGN -> answer "YES".
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import aiohttp
import structlog

from app.services.payments.ports import (
    CreatedPayment,
    InvalidWebhookSignatureError,
    PaymentProvider,
    ProviderError,
    WebhookVerdict,
)

logger = structlog.get_logger()

KASSAI_API = "https://api.fk.life/v1/orders/create"
_NSPK = "https://qr.nspk.ru/"
_MAX_REDIRECTS = 5


class _KassaiBase(PaymentProvider):
    method_id: int = 0

    def __init__(self, shop_id: str, api_key: str, secret_key2: str, email_domain: str) -> None:
        self._shop_id = shop_id
        self._api_key = api_key
        self._secret_key2 = secret_key2
        # Kassa requires an e-mail; use our own domain, never a third-party one.
        self._email_domain = email_domain
        self._session: aiohttp.ClientSession | None = None

    def sign_request(self, data: dict[str, Any]) -> str:
        message = "|".join(str(v) for _, v in sorted(data.items()))
        return hmac.new(self._api_key.encode(), message.encode(), hashlib.sha256).hexdigest()

    def webhook_sign(self, amount: str, order_id: str) -> str:
        raw = f"{self._shop_id}:{amount}:{self._secret_key2}:{order_id}"
        return hashlib.md5(raw.encode()).hexdigest()  # noqa: S324 - provider's scheme

    async def create_payment(self, payment_id: int, tg_id: int, amount: int) -> CreatedPayment:
        if amount < self.min_amount:
            raise ProviderError(self.name, f"amount {amount} < min {self.min_amount}")
        nonce = int(time.time() * 1000)
        order_id = f"{nonce}_{tg_id}"
        data: dict[str, Any] = {
            "shopId": int(self._shop_id),
            "nonce": nonce,
            "i": self.method_id,
            "amount": amount,
            "currency": "RUB",
            "paymentId": order_id,
            "desc": f"Credits {payment_id}",
            "email": f"user{tg_id}@{self._email_domain}",
            "ip": "127.0.0.1",
        }
        data["signature"] = self.sign_request(data)
        logger.info("kassai_request", provider=self.name, payment_id=payment_id, amount=amount)
        session = self._get_session()
        async with session.post(KASSAI_API, json=data, allow_redirects=False) as resp:
            text = await resp.text()
            if resp.status not in (200, 301, 302):
                raise ProviderError(self.name, f"API {resp.status}: {text[:300]}")
            try:
                body = await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                body = {}
            location = resp.headers.get("location") or (
                body.get("location", "") if isinstance(body, dict) else ""
            )
        if not location:
            reason = body.get("message") or body.get("error") if isinstance(body, dict) else None
            raise ProviderError(self.name, f"no payment URL: {reason or text[:200]}")
        return CreatedPayment(order_id, await self._bank_deeplink(session, location))

    async def _bank_deeplink(self, session: aiohttp.ClientSession, url: str) -> str:
        """paymentt.kassa.ai/qr/?id=… is a QR page; /qr/go/?id=… redirects straight to the
        qr.nspk.ru link that opens the bank app on a phone."""
        current = url.replace("/qr/?", "/qr/go/?", 1)
        try:
            for _ in range(_MAX_REDIRECTS):
                async with session.get(current, allow_redirects=False) as resp:
                    location = resp.headers.get("Location")
                    if resp.status not in (301, 302, 303, 307, 308) or not location:
                        return current
                    current = location
                    if current.startswith(_NSPK):
                        return current
        except aiohttp.ClientError:
            logger.warning("kassai_deeplink_failed", exc_info=True)
            return url
        return current

    async def verify_webhook(self, payload: dict[str, Any]) -> WebhookVerdict:
        amount = str(payload.get("AMOUNT", ""))
        order_id = str(payload.get("MERCHANT_ORDER_ID", "") or payload.get("intid", ""))
        expected = self.webhook_sign(amount, order_id)
        if not hmac.compare_digest(expected.lower(), str(payload.get("SIGN", "")).lower()):
            raise InvalidWebhookSignatureError(self.name)
        try:
            rub = int(float(amount))
        except ValueError:
            rub = 0
        return WebhookVerdict(order_id, rub, is_success=True, raw=payload)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class KassaiSBP(_KassaiBase):
    name = "kassai_sbp"
    label = "🏦 СБП"
    method_id = 44
    min_amount = 10


class KassaiCards(_KassaiBase):
    name = "kassai_cards"
    label = "💳 Карта РФ"
    method_id = 36
    min_amount = 50


KASSAI_METHOD_TO_PROVIDER = {"44": KassaiSBP.name, "36": KassaiCards.name}
