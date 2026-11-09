# video-uniq

Сервис уникализации видео для соцсетей: Telegram-бот, веб-кабинет и REST API поверх одной
очереди FFmpeg-воркеров. Из одного исходника делает N копий, каждая из которых отличается для
алгоритмов антидублей (pHash кадров, аудио-отпечаток, хеш файла), но визуально равна оригиналу.

Стек: Python 3.12, aiogram 3 (webhook), arq + Redis, PostgreSQL 16 (asyncpg), FFmpeg, S3-совместимое
хранилище, Docker Compose, централизованный Caddy. Стандарт кода: `SKILL.md` (обязателен).

## Архитектура

```
Telegram bot ─┐
Web cabinet  ─┼─> API/handlers ─> Redis (arq) ─> workers (FFmpeg) ─> S3 ─> delivery
REST API     ─┘        │                              │
                   PostgreSQL <───── fingerprints ─────┘
```

- `app/handlers` — тонкие хендлеры бота (меню, загрузка, баланс, настройки, рефералка).
- `app/services/uniqualizer.py` — генерация плана обработки из сида, сборка команды FFmpeg.
- `app/services/profiles.py` — профили метаданных (редакторы, при явном включении — камеры).
- `app/services/fingerprints.py` — pHash / Chromaprint / SHA-256, проверка на коллизии.
- `app/services/billing.py` — кредиты, транзакции, сгорание через 30 дней, Stars-инвойсы.
- `app/workers/uniqualize.py` — arq-задача: скачать, отрендерить, проверить, выдать.
- `app/db/repositories` — весь доступ к БД, сырой SQL через asyncpg.

Подробности: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Разбор конкурента и целевые
метрики выхода: [docs/COMPETITOR_UNIQUEVID.md](docs/COMPETITOR_UNIQUEVID.md).

## Режимы и тарифы (черновик)

| Режим | Разрешение | Изменения | Кредитов за копию |
|---|---|---|---|
| Base | до 720p | геометрия, время, цвет, звук, кодек, метаданные | 1 |
| HD | до 1080p | то же, сильнее диапазоны | 2 |
| HD+ | до 1080p | плюс видимые изменения: виньетка, зерно, лёгкий сдвиг рамки | 3 |

Копий за запуск: 1 / 5 / 10. Кредиты сгорают через 30 дней после последней покупки.

## Запуск

```bash
cp .env.example .env            # заполнить BOT_TOKEN, WEBHOOK_HOST, DATABASE_URL, REDIS_URL
docker compose up -d --build    # bot, worker x2, db, redis
```

Caddy конфиг для сервера лежит в `caddy/video-uniq.conf`, порты наружу не пробрасываются (SKILL §13).

## Разработка

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -e ".[dev]"
ruff check . && mypy app && pytest
```

Для тестов рендера нужны `ffmpeg`, `ffprobe` и `fpcalc` (`brew install ffmpeg chromaprint`),
без них такие тесты пропускаются. SQL-тесты запускаются, если задан `TEST_DATABASE_URL`
(пустая одноразовая БД, схема пересоздаётся).

## Статус

Готово: пайплайн рендера, отпечатки и гейт качества, arq-задача (этап 1). Дорожная карта в `docs/ARCHITECTURE.md`, раздел «План работ».
