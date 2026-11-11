"""User-facing bot texts. Kept in one place so handlers, services and workers agree."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.video_rules import Rejection, RejectReason


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 копия, 2 копии, 5 копий, 11 копий, 21 копия."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


_UNIQUE_COPY_FORMS = ("уникальная копия", "уникальные копии", "уникальных копий")


def copies_word(n: int) -> str:
    return f"{n} {plural(n, 'копия', 'копии', 'копий')}"


def credits_word(n: int) -> str:
    return f"{n} {plural(n, 'кредит', 'кредита', 'кредитов')}"


DOWNLOADING = "⏳ Загружаю видео…"
NOT_A_VIDEO = "Это не видео. Пришлите ролик файлом (📎 → Файл), до {limit} МБ."
TOO_LARGE = "Файл {size:.0f} МБ, а лимит {limit} МБ. Сожмите или обрежьте ролик и пришлите снова."
TOO_MANY_JOBS = (
    "У вас уже {limit} задачи в работе. Дождитесь результата и пришлите следующее видео."
)
DOWNLOAD_FAILED = "Не удалось загрузить видео. Попробуйте отправить его ещё раз."
COMPRESSED_HINT = (
    "\n\nСовет: отправляйте видео файлом (📎 → Файл), а не как видео — "
    "так Telegram не пережимает его и копии получаются качественнее."
)


def queued(position: int, copies: int) -> str:
    if position == 0:
        return f"✅ Видео принято. Делаю {copies_word(copies)}, начну через несколько секунд."
    return (
        f"✅ Видео принято, сейчас очередь.\n"
        f"Перед вами задач: {position}. Ожидайте — пришлю {copies_word(copies)}, "
        "как только будут готовы."
    )


def processing(copies: int) -> str:
    return f"⚙️ Обрабатываю видео: {copies_word(copies)}…"


def done(count: int, charged: int, balance: int | None) -> str:
    files = "файл ниже" if count == 1 else "каждая — отдельный файл ниже"
    text = (
        f"🎉 Готово: {count} {plural(count, *_UNIQUE_COPY_FORMS)}, {files}.\nСписано {charged} кр."
    )
    return text + (f", на балансе {balance} кр." if balance is not None else ".")


def partial(ok: int, failed: int, refunded: int) -> str:
    return (
        f"Выдано {copies_word(ok)}, ещё {failed} не прошли проверку уникальности. "
        f"За них {refunded} кр. вернулись на баланс."
    )


FAILED = "😔 Не получилось обработать это видео. Попробуйте другой файл или напишите в /support."


def failed(refunded: int) -> str:
    return FAILED + (f"\nКредиты ({refunded} кр.) вернулись на баланс." if refunded else "")


def not_enough_credits(cost: int, balance: int) -> str:
    return (
        f"Не хватает кредитов: нужно {cost}, на балансе {balance}.\n"
        "Пополните баланс — /buy, или уменьшите число копий в /settings."
    )


def welcome(trial_credits: int) -> str:
    text = (
        "Пришлите видео файлом (📎 → Файл) — сделаю несколько уникальных копий, которые "
        "соцсети считают новыми, а зрители не отличат от оригинала.\n\n"
        "Режим и число копий — в ⚙️ Настройках, баланс и пополнение — в 💰 Балансе."
    )
    if trial_credits:
        text += f"\n\n🎁 Вам начислено {credits_word(trial_credits)}, чтобы попробовать бесплатно."
    return text


BUY_TITLE = "Выберите пакет кредитов (оплата Telegram Stars):"


def pack_button(credits: int, stars: int) -> str:
    return f"{credits} кр. — {stars} ⭐"


def invoice_title(credits: int) -> str:
    return credits_word(credits)


def invoice_description(credits: int) -> str:
    return (
        f"{credits_word(credits)} на уникализацию видео, действуют 30 дней с последней покупки. "
        "Оплачивая, вы принимаете условия /terms."
    )


def purchase_done(credits: int, balance: int) -> str:
    return f"✅ Оплата получена: +{credits} кр. На балансе {balance} кр."


def referral(link: str, bonus: int) -> str:
    return (
        f"🎯 Приглашайте друзей: после их первой обработки вы оба получите по {bonus} кр.\n\n"
        f"Ваша ссылка:\n{link}"
    )


def referral_rewarded(bonus: int) -> str:
    return f"🎁 Приглашённый вами друг сделал первую обработку — вам начислено {bonus} кр."


def referral_welcome_bonus(bonus: int) -> str:
    return f"🎁 Бонус за приглашение: +{bonus} кр."


UNREADABLE = "Не получается прочитать это видео. Пересохраните его в mp4 и пришлите снова."
_REQUIREMENTS = (
    "\n\n📐 Требования: mp4 или mov, от {min_s:g} с до {max_min:g} мин, "
    "до 4K при 30 fps или до 1080p при 60 fps."
)


def rejected(rejection: Rejection) -> str:
    src, rules = rejection.source, rejection.rules
    reason = rejection.reason
    if reason is RejectReason.BAD_FORMAT:
        text = "Этот формат не подходит. Нужен mp4 или mov — пересохраните ролик и пришлите снова."
    elif reason is RejectReason.TOO_SHORT:
        text = f"Видео слишком короткое: {src.duration:.1f} с."
    elif reason is RejectReason.TOO_LONG:
        text = f"Видео слишком длинное: {_duration(src.duration)}. Обрежьте его и пришлите снова."
    elif reason is RejectReason.TOO_LARGE_FRAME:
        text = f"Разрешение {src.width}×{src.height} больше 4K. Уменьшите его и пришлите снова."
    elif reason is RejectReason.TOO_HIGH_FPS:
        text = f"Частота {src.fps:.0f} кадров/с — это больше 60."
    else:
        text = f"{src.width}×{src.height} при {src.fps:.0f} кадрах/с — для 60 fps нужно до 1080p."
    return text + _REQUIREMENTS.format(
        min_s=rules.min_duration_s, max_min=rules.max_duration_s / 60
    )


def _duration(seconds: float) -> str:
    minutes, sec = divmod(round(seconds), 60)
    return f"{minutes} мин {sec:02d} с" if minutes else f"{sec} с"


# --- menu ------------------------------------------------------------------------------

BTN_UNIQUALIZE = "🎬 Уникализировать"
BTN_BALANCE = "💰 Баланс"
BTN_SETTINGS = "⚙️ Настройки"
BTN_INVITE = "🎯 Пригласить"
BTN_HELP = "❓ Помощь"
BTN_BUY = "💳 Пополнить"
MENU_PLACEHOLDER = "Пришлите видео файлом"

MODE_TITLES = {"base": "Base", "hd": "HD", "hd_plus": "HD+"}
MODE_DESCRIPTIONS = {
    "base": "до 720p, невидимые изменения",
    "hd": "до 1080p, изменения сильнее",
    "hd_plus": "до 1080p, плюс лёгкие видимые: виньетка, зерно",
}


def menu_hint(mode: str, copies: int, cost: int) -> str:
    return (
        f"Пришлите видео файлом (📎 → Файл).\n"
        f"Сейчас: {MODE_TITLES[mode]}, {copies_word(copies)} — {cost} кр. за запуск. "
        "Поменять — в ⚙️ Настройках."
    )


def settings_screen(mode: str, copies: int, price: int, balance: int) -> str:
    return (
        "⚙️ Настройки\n\n"
        f"Режим: {MODE_TITLES[mode]} — {MODE_DESCRIPTIONS[mode]}\n"
        f"Копий за запуск: {copies}\n\n"
        f"Один запуск: {copies} × {price} кр. = {copies * price} кр. На балансе {balance} кр."
    )


def mode_button(mode: str, price: int, selected: bool) -> str:
    return f"{'✅ ' if selected else ''}{MODE_TITLES[mode]} · {price} кр."


def copies_button(copies: int, selected: bool) -> str:
    return f"{'✅ ' if selected else ''}{copies}"


HELP = (
    "❓ Как это работает\n\n"
    "1. Пришлите видео файлом (📎 → Файл) — так Telegram не пережимает его.\n"
    "2. Бот сделает копии: у каждой своя геометрия, скорость, цвет, звук и кодирование, "
    "а каждая копия проверяется на отличие от оригинала и от прежних копий.\n"
    "3. Копии придут файлами — выкладывайте как новые ролики.\n\n"
    "📐 Требования: mp4 или mov, до {max_mb} МБ, от 0.5 с до 3 мин, "
    "до 4K при 30 fps или до 1080p при 60 fps. Вертикальные и горизонтальные — любые.\n\n"
    "💰 Кредиты: копия стоит 1–3 кр. в зависимости от режима. Списываются только за выданные "
    "копии. Купленные кредиты действуют {ttl} дней с последней покупки.\n\n"
    "🔒 Видео и копии удаляются через {files_ttl} ч.\n\n"
    "Важно: площадка всё равно может узнать ролик — мы повышаем шансы, но не гарантируем.\n\n"
    "📄 /terms — оферта · 🔒 /privacy — конфиденциальность · 💳 /paysupport — оплата"
)


def support(contact: str) -> str:
    if contact:
        return f"Поддержка: {contact}. Опишите проблему и приложите номер задачи, если есть."
    return "Поддержка скоро появится. Пока что опишите проблему ответом на это сообщение."


def balance_screen(credits: int, mode: str, copies: int, cost: int, ttl_days: int) -> str:
    return (
        f"💰 Баланс: {credits} кр.\n"
        f"Текущий запуск ({MODE_TITLES[mode]}, {copies_word(copies)}) стоит {cost} кр.\n\n"
        "Цена копии: Base 1 кр., HD 2 кр., HD+ 3 кр.\n"
        f"Купленные кредиты действуют {ttl_days} дней с последней покупки."
    )


# --- video card ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardInfo:
    mode: str
    copies: int
    audio: str
    price: int
    balance: int
    duration: float | None
    width: int | None
    height: int | None


AUDIO_TITLES = {"shift": "меняю тон", "keep": "оригинал", "mute": "без звука"}
AUDIO_HINTS = {
    "shift": "",
    "keep": "\nЗвук останется как в оригинале (подогнан под скорость видео).",
    "mute": "\nКопии будут без звука.",
}
CARD_EXPIRED = "Эта карточка устарела. Пришлите видео ещё раз."
CARD_CANCELLED = "Отменено, кредиты не списывались. Пришлите другое видео, когда будете готовы."


def card(info: CardInfo) -> str:
    size = f" · {info.width}×{info.height}" if info.width and info.height else ""
    duration = f" {_duration(info.duration)}" if info.duration else ""
    return (
        f"🎬 Видео{duration}{size}\n\n"
        f"Режим: {MODE_TITLES[info.mode]} — {MODE_DESCRIPTIONS[info.mode]}\n"
        f"Копий: {info.copies} · Звук: {AUDIO_TITLES[info.audio]}{AUDIO_HINTS[info.audio]}\n\n"
        f"{info.copies} × {info.price} кр. = {info.copies * info.price} кр. · "
        f"на балансе {info.balance} кр."
    )


def card_start_button(copies: int, cost: int) -> str:
    accusative = plural(copies, "копию", "копии", "копий")  # "сделать 1 копию / 3 копии"
    return f"▶ Сделать {copies} {accusative} — {cost} кр."


def card_topup_button(missing: int) -> str:
    return f"💳 Не хватает {missing} кр. — пополнить"


def card_audio_button(audio: str) -> str:
    return f"🔊 Звук: {AUDIO_TITLES[audio]} ↻"


def progress(ready: int, total: int) -> str:
    return f"⚙️ Готово {ready} из {total}…"


# --- metadata profiles -----------------------------------------------------------------


def camera_button(enabled: bool) -> str:
    return f"📱 Профили камер: {'вкл' if enabled else 'выкл'}"


def camera_hint(enabled: bool) -> str:
    if enabled:
        return (
            "\n\n📱 Профили камер включены: часть копий будет выглядеть как снятая на телефон "
            "(производитель, модель, версия ОС в метаданных). Используйте только для своего видео."
        )
    return "\n\n🎬 Копии выглядят как экспорт из видеоредакторов (CapCut, InShot и др.)."


def backup_caption(name: str, size: int, entries: int) -> str:
    return (
        f"🗄 Бэкап БД {name} · {size / 1024:.0f} КБ · объектов: {entries}\n"
        "Восстановление: README → «Бэкапы»."
    )


# --- legal ----------------------------------------------------------------------------


def terms_link(url: str) -> str:
    return f"📄 Пользовательское соглашение (оферта): {url}"


def privacy_link(url: str) -> str:
    return f"🔒 Политика конфиденциальности: {url}"


def legal_block(terms_url: str, privacy_url: str) -> str:
    return (
        "📜 Продолжая, вы принимаете наши:\n"
        f"1️⃣ Пользовательское соглашение: {terms_url}\n"
        f"2️⃣ Политика конфиденциальности: {privacy_url}"
    )


def paysupport(contact: str, email: str, terms_url: str) -> str:
    where = " или ".join(x for x in (contact, email) if x) or "ответом на это сообщение"
    return (
        "💳 Вопросы по оплате и возвратам\n\n"
        "• Если копии не выданы, кредиты возвращаются на баланс автоматически.\n"
        "• Возврат Stars за пакет — в течение 14 дней с покупки, если кредиты из него "
        "не израсходованы. Возврат проходит через Telegram на ваш аккаунт.\n"
        "• Охват и распознавание повторов площадками от нас не зависят, такие случаи "
        "не являются основанием для возврата.\n\n"
        f"Напишите: {where}. Укажите дату покупки.\n"
        f"Условия: {terms_url}"
    )


# --- RUB payments ---------------------------------------------------------------------

PAYMENT_PROVIDER_DOWN = "Платёжный сервис сейчас не отвечает. Попробуйте позже или оплатите Stars."


def pay_methods_title(credits: int) -> str:
    return f"💳 {credits_word(credits)}. Выберите способ оплаты:"


def pay_method_button(label: str, amount: int, unit: str) -> str:
    return f"{label} — {amount} {unit}"


def pay_link(credits: int, rub: int) -> str:
    return (
        f"Счёт на {rub} ₽ за {credits_word(credits)} создан. Нажмите кнопку, чтобы оплатить — "
        "откроется банк или страница оплаты. Кредиты придут сообщением сразу после оплаты.\n"
        "Ссылка действует 24 часа."
    )


def pay_link_button(rub: int) -> str:
    return f"Оплатить {rub} ₽"
