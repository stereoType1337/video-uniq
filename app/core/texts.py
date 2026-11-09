"""User-facing bot texts. Kept in one place so handlers, services and workers agree."""

from __future__ import annotations

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
