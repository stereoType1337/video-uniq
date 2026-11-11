"""Providers exist only when their credentials are configured (no credentials -> no button)."""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.config import Settings
from app.services.payments.kassai import KassaiCards, KassaiSBP
from app.services.payments.platega import Platega
from app.services.payments.ports import PaymentProvider

ORDER = ("platega", "kassai_sbp", "kassai_cards")  # button order; Stars is always last


def build_providers(cfg: Settings) -> dict[str, PaymentProvider]:
    providers: dict[str, PaymentProvider] = {}
    if cfg.platega_merchant_id and cfg.platega_api_key:
        providers[Platega.name] = Platega(
            cfg.platega_merchant_id,
            cfg.platega_api_key,
            cfg.platega_method_code,
            cfg.public_url.rstrip("/"),
            cfg.platega_min_amount,
        )
    if cfg.kassai_shop_id and cfg.kassai_api_key and cfg.kassai_secret_key2:
        domain = urlsplit(cfg.public_url).hostname or "localhost"
        args = (cfg.kassai_shop_id, cfg.kassai_api_key, cfg.kassai_secret_key2, domain)
        providers[KassaiSBP.name] = KassaiSBP(*args)
        if cfg.kassai_cards_enabled:
            providers[KassaiCards.name] = KassaiCards(*args)
    return {name: providers[name] for name in ORDER if name in providers}
