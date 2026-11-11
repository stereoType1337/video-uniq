"""Provider callbacks and the pages a buyer returns to (tg-bot-blueprint payments.md).

HTTP contract: probe without a signature -> 200; bad signature/credentials -> 401/403;
our own failure -> 500 (the provider retries, which is what we want); an unknown payment or
an amount mismatch -> 200 (otherwise the provider retries forever) plus an ERROR alert.
"""

from __future__ import annotations

from html import escape
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from app.core import texts
from app.core.config import settings
from app.core.exceptions import PaymentAmountMismatchError
from app.db.repositories.billing import PaymentOutcome
from app.services.billing import PaymentFlow
from app.services.payments.kassai import KASSAI_METHOD_TO_PROVIDER, KassaiSBP
from app.services.payments.platega import Platega
from app.services.payments.ports import InvalidWebhookSignatureError

logger = structlog.get_logger()

FLOW_KEY: web.AppKey[PaymentFlow] = web.AppKey("payment_flow", PaymentFlow)
BOT_KEY: web.AppKey[Bot] = web.AppKey("payment_bot", Bot)


async def notify_buyer(bot: Bot, outcome: PaymentOutcome | None) -> None:
    if outcome is None or not outcome.just_completed or outcome.tg_id is None:
        return
    if outcome.balance is None:
        return
    try:
        await bot.send_message(
            outcome.tg_id, texts.purchase_done(outcome.payment.credits, outcome.balance)
        )
    except TelegramAPIError:
        logger.warning("purchase_notify_failed", payment_id=outcome.payment.id)


async def kassai_webhook(request: web.Request) -> web.Response:
    form = await request.post()
    payload: dict[str, Any] = {k: str(v) for k, v in form.items()}
    if "SIGN" not in payload:  # "check URL" probe from the Kassa cabinet
        return web.Response(text="YES")
    provider = KASSAI_METHOD_TO_PROVIDER.get(str(payload.get("CUR_ID", "")), KassaiSBP.name)
    flow = request.app[FLOW_KEY]
    if provider not in flow.providers:
        return web.Response(status=400, text="provider not configured")
    try:
        outcome = await flow.process_webhook(provider, payload)
    except InvalidWebhookSignatureError:
        logger.warning("kassai_bad_signature", remote=request.remote)
        return web.Response(status=403, text="bad signature")
    except PaymentAmountMismatchError:
        logger.exception("payment_amount_mismatch", provider=provider)
        return web.Response(text="YES")
    await notify_buyer(request.app[BOT_KEY], outcome)
    return web.Response(text="YES")


async def platega_webhook(request: web.Request) -> web.Response:
    flow = request.app[FLOW_KEY]
    provider = flow.providers.get(Platega.name)
    if not isinstance(provider, Platega):
        return web.json_response({"error": "provider not configured"}, status=400)
    if not provider.verify_credentials(
        request.headers.get("X-MerchantId"), request.headers.get("X-Secret")
    ):
        logger.warning("platega_bad_credentials", remote=request.remote)
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = await request.json()
    except ValueError:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "bad json"}, status=400)
    try:
        outcome = await flow.process_webhook(Platega.name, payload)
    except PaymentAmountMismatchError:
        logger.exception("payment_amount_mismatch", provider=Platega.name)
        return web.json_response({"status": "ok"})
    await notify_buyer(request.app[BOT_KEY], outcome)
    return web.json_response({"status": "ok"})


_RESULT_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.5 system-ui, sans-serif; display: flex; min-height: 100vh;
         align-items: center; justify-content: center; margin: 0; padding: 24px; }}
  .card {{ max-width: 420px; text-align: center; }}
  .icon {{ font-size: 56px; }}
  a.btn {{ display: inline-block; margin-top: 20px; padding: 14px 22px; border-radius: 12px;
          background: #2AABEE; color: #fff; text-decoration: none; font-weight: 600; }}
</style></head>
<body><div class="card"><div class="icon">{icon}</div><h1>{title}</h1><p>{note}</p>
<a class="btn" href="{bot_url}">Вернуться в бота</a></div></body></html>"""


def _result_page(ok: bool) -> str:
    bot_url = f"https://t.me/{escape(settings.bot_username)}" if settings.bot_username else "/"
    return _RESULT_PAGE.format(
        title="Оплата прошла" if ok else "Оплата не прошла",
        icon="✅" if ok else "❌",
        note=(
            "Кредиты зачислятся в течение минуты — бот пришлёт сообщение."
            if ok
            else "Деньги не списаны. Попробуйте ещё раз или выберите другой способ оплаты."
        ),
        bot_url=bot_url,
    )


async def pay_success(_: web.Request) -> web.Response:
    return web.Response(text=_result_page(ok=True), content_type="text/html")


async def pay_fail(_: web.Request) -> web.Response:
    return web.Response(text=_result_page(ok=False), content_type="text/html")


def setup(app: web.Application) -> None:
    """Routes only; FLOW_KEY/BOT_KEY are attached on startup (the flow needs the DB pool)."""
    app.router.add_post("/payments/kassai/webhook", kassai_webhook)
    app.router.add_post("/payments/platega/webhook", platega_webhook)
    app.router.add_get("/pay/success", pay_success)
    app.router.add_get("/pay/fail", pay_fail)
