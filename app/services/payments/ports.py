"""Payment provider port (tg-bot-blueprint payments.md). Adapters hold no business logic:
settlement lives in BillingRepository.apply_verdict only."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import AppError


class ProviderError(AppError):
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


class InvalidWebhookSignatureError(AppError):
    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider}: invalid webhook signature")
        self.provider = provider


@dataclass(frozen=True, slots=True)
class CreatedPayment:
    provider_payment_id: str
    payment_url: str


@dataclass(frozen=True, slots=True)
class WebhookVerdict:
    provider_payment_id: str
    amount: int  # RUB
    is_success: bool
    # False = the provider has no verdict yet (PENDING): the payment must stay untouched,
    # otherwise a slow but real payment would be marked cancelled.
    is_final: bool = True
    internal_payment_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(abc.ABC):
    name: str
    label: str  # button text
    min_amount: int = 1
    verifies_amount: bool = False  # True when the callback body is unsigned
    can_poll: bool = False  # has a status API -> lost callbacks are recovered by reconcile

    @abc.abstractmethod
    async def create_payment(self, payment_id: int, tg_id: int, amount: int) -> CreatedPayment: ...

    @abc.abstractmethod
    async def verify_webhook(self, payload: dict[str, Any]) -> WebhookVerdict: ...

    async def fetch_status(self, provider_payment_id: str) -> WebhookVerdict | None:
        """Poll the provider; None = no verdict (or not supported)."""
        return None

    async def aclose(self) -> None:
        return None
