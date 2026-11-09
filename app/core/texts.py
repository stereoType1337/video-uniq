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


def done(count: int) -> str:
    return f"🎉 Готово: {count} уникальных копий. Каждая — отдельный файл ниже."


def partial(ok: int, failed: int) -> str:
    return f"Готово {ok} копий, ещё {failed} не прошли проверку уникальности и не выданы."


FAILED = "😔 Не получилось обработать это видео. Попробуйте другой файл или напишите в /support."


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
