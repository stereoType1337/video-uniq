"""User-facing bot texts. Kept in one place so handlers, services and workers agree."""

from __future__ import annotations

from app.services.video_rules import Rejection, RejectReason

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
        return f"✅ Видео принято. Делаю {copies} копий, начну через несколько секунд."
    return (
        f"✅ Видео принято, сейчас очередь.\n"
        f"Перед вами задач: {position}. Ожидайте — пришлю {copies} копий, как только будут готовы."
    )


def processing(copies: int) -> str:
    return f"⚙️ Обрабатываю видео: {copies} копий…"


def done(count: int, charged: int, balance: int | None) -> str:
    text = (
        f"🎉 Готово: {count} уникальных копий, каждая — отдельный файл ниже.\nСписано {charged} кр."
    )
    return text + (f", на балансе {balance} кр." if balance is not None else ".")


def partial(ok: int, failed: int, refunded: int) -> str:
    return (
        f"Выдано {ok} копий, ещё {failed} не прошли проверку уникальности. "
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
        "Пришлите видео файлом — сделаю несколько уникальных копий, которые соцсети считают "
        "новыми, а зрители не отличат от оригинала.\n\n"
        "/balance — баланс и цены, /buy — пополнить, /ref — пригласить друга."
    )
    if trial_credits:
        text += f"\n\n🎁 Вам начислено {trial_credits} кредитов, чтобы попробовать бесплатно."
    return text


def balance(credits: int, prices: dict[str, int], expires_days: int) -> str:
    lines = [f"💰 Баланс: {credits} кр.", "", "Цена одной копии:"]
    lines += [f"• {mode} — {price} кр." for mode, price in prices.items()]
    lines += ["", f"Купленные кредиты действуют {expires_days} дней с последней покупки."]
    return "\n".join(lines)


BUY_TITLE = "Выберите пакет кредитов (оплата Telegram Stars):"


def pack_button(credits: int, stars: int) -> str:
    return f"{credits} кр. — {stars} ⭐"


def invoice_title(credits: int) -> str:
    return f"{credits} кредитов"


def invoice_description(credits: int) -> str:
    return f"{credits} кредитов на уникализацию видео. Действуют 30 дней с последней покупки."


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
