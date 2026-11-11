from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from app.core import texts
from app.core.config import settings
from app.core.exceptions import PaymentAmountMismatchError, PaymentNotFoundError
from app.db.repositories.billing import ProviderVerdict
from app.db.repositories.users import UserRepository
from app.handlers.screens import (
    STARS_METHOD,
    BuyPack,
    PayWith,
    render_pay_link,
    render_pay_methods,
)
from app.services.billing import (
    STARS_CURRENCY,
    AccountService,
    PaymentFlow,
    find_pack,
    referral_link,
)
from app.services.payments.ports import ProviderError

router = Router(name="billing")


@router.callback_query(BuyPack.filter())
async def handle_pack(
    query: CallbackQuery,
    callback_data: BuyPack,
    bot: Bot,
    accounts: AccountService,
    payments: PaymentFlow,
) -> None:
    pack = find_pack(callback_data.code)
    if pack is None or not isinstance(query.message, Message):
        await query.answer("Пакет больше недоступен, откройте /buy заново.", show_alert=True)
        return
    if payments.providers:  # RUB methods configured: let the user pick
        text, keyboard = render_pay_methods(pack, payments.providers)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer()
        return
    await _send_stars_invoice(query, bot, accounts, payments, pack.code)


@router.callback_query(PayWith.filter())
async def handle_pay_with(
    query: CallbackQuery,
    callback_data: PayWith,
    bot: Bot,
    accounts: AccountService,
    payments: PaymentFlow,
) -> None:
    pack = find_pack(callback_data.code)
    if pack is None or not isinstance(query.message, Message):
        await query.answer("Пакет больше недоступен, откройте /buy заново.", show_alert=True)
        return
    if callback_data.method == STARS_METHOD:
        await _send_stars_invoice(query, bot, accounts, payments, pack.code)
        return
    if callback_data.method not in payments.providers:
        await query.answer("Этот способ сейчас недоступен.", show_alert=True)
        return
    account = await accounts.ensure(query.from_user.id, query.from_user.username)
    try:
        url = await payments.start_external(account.user, pack, callback_data.method)
    except ProviderError:
        await query.answer(texts.PAYMENT_PROVIDER_DOWN, show_alert=True)
        return
    text, keyboard = render_pay_link(pack, url)
    await query.message.answer(text, reply_markup=keyboard)
    await query.answer()


async def _send_stars_invoice(
    query: CallbackQuery, bot: Bot, accounts: AccountService, payments: PaymentFlow, code: str
) -> None:
    pack = find_pack(code)
    if pack is None or query.message is None:
        await query.answer()
        return
    account = await accounts.ensure(query.from_user.id, query.from_user.username)
    payment = await payments.start(account.user, pack)
    await bot.send_invoice(
        chat_id=query.message.chat.id,
        title=texts.invoice_title(pack.credits),
        description=texts.invoice_description(pack.credits),
        payload=str(payment.id),
        currency=STARS_CURRENCY,
        prices=[LabeledPrice(label=texts.invoice_title(pack.credits), amount=pack.stars)],
    )
    await query.answer()


@router.pre_checkout_query()
async def handle_pre_checkout(
    query: PreCheckoutQuery, users: UserRepository, payments: PaymentFlow
) -> None:
    user = await users.get_by_tg_id(query.from_user.id)
    error = await payments.check_pre_checkout(
        user, query.invoice_payload, query.total_amount, query.currency
    )
    await query.answer(ok=error is None, error_message=error)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message, payments: PaymentFlow) -> None:
    paid = message.successful_payment
    if paid is None or not paid.invoice_payload.isdigit():
        return
    verdict = ProviderVerdict(
        payment_id=int(paid.invoice_payload),
        provider_payment_id=paid.telegram_payment_charge_id,
        amount=paid.total_amount,
        payload=paid.model_dump(mode="json", exclude_none=True),
    )
    try:
        outcome = await payments.complete(verdict)
    except (PaymentNotFoundError, PaymentAmountMismatchError):
        # Telegram already took the Stars: keep the charge id in logs for a manual refund.
        await message.answer("Оплата получена, но не сошлась со счётом. Напишите в /support.")
        raise
    if outcome.just_completed and outcome.balance is not None:
        await message.answer(texts.purchase_done(outcome.payment.credits, outcome.balance))


@router.message(Command("ref"))
@router.message(F.text == texts.BTN_INVITE)
async def handle_ref(message: Message, bot: Bot, accounts: AccountService) -> None:
    if message.from_user is None:
        return
    account = await accounts.ensure(message.from_user.id, message.from_user.username)
    me = await bot.me()
    link = referral_link(me.username or "", account.user.ref_code)
    await message.answer(texts.referral(link, settings.referral_bonus))
