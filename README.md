# video-uniq

Сервис уникализации видео для соцсетей: Telegram-бот, веб-кабинет и REST API поверх одной
очереди FFmpeg-воркеров. Из одного исходника делает N копий, каждая из которых отличается для
алгоритмов антидублей (pHash кадров, аудио-отпечаток, хеш файла), но визуально равна оригиналу.

Стек: Python 3.12, aiogram 3 (webhook) + локальный telegram-bot-api, arq + Redis, PostgreSQL 16
(asyncpg), FFmpeg, Docker Compose, централизованный Caddy. Стандарт кода: `SKILL.md` (обязателен).

## Архитектура

```
Telegram bot ─┐
Web cabinet  ─┼─> API/handlers ─> Redis (arq) ─> workers (FFmpeg) ─> ./work ─> delivery
REST API     ─┘        │                              │
                   PostgreSQL <───── fingerprints ─────┘
```

- `app/handlers` — тонкие хендлеры бота (меню, загрузка, баланс, настройки, рефералка).
- `app/services/uniqualizer.py` — генерация плана обработки из сида, сборка команды FFmpeg.
- `app/services/profiles.py` — профили метаданных (редакторы, при явном включении — камеры).
- `app/services/fingerprints.py` — pHash / Chromaprint / SHA-256, проверка на коллизии.
- `app/services/intake.py` — приём видео: лимиты, перенос исходника, задача, очередь.
- `app/services/notifier.py` — статус задачи и выдача копий в чат.
- `app/services/billing.py` + `app/db/repositories/{billing,ledger}.py` — кредиты, резерв и
  расчёт по выданным копиям, Stars, бесплатные кредиты, рефералка, сгорание.
- `app/workers/uniqualize.py` — arq-задача: скачать, отрендерить, проверить, выдать.
- `app/db/repositories` — весь доступ к БД, сырой SQL через asyncpg.

Подробности: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Разбор конкурента и целевые
метрики выхода: [docs/COMPETITOR_UNIQUEVID.md](docs/COMPETITOR_UNIQUEVID.md),
[docs/COMPETITOR_UNIQLY.md](docs/COMPETITOR_UNIQLY.md).

## Режимы и тарифы (черновик)

| Режим | Разрешение | Изменения | Кредитов за копию |
|---|---|---|---|
| Base | до 720p | геометрия, время, цвет, звук, кодек, метаданные | 1 |
| HD | до 1080p | то же, сильнее диапазоны | 2 |
| HD+ | до 1080p | плюс видимые изменения: виньетка, зерно, лёгкий сдвиг рамки | 3 |

Копий за запуск: 1 / 5 / 10. Кредиты сгорают через 30 дней после последней покупки.

## Запуск

```bash
cp .env.example .env            # BOT_TOKEN, SERVICE_SECRET, TELEGRAM_API_ID, TELEGRAM_API_HASH
mkdir -p work/tg
# один раз: вывести бота из облачного Bot API, иначе локальный сервер не получит апдейты
curl -s "https://api.telegram.org/bot$BOT_TOKEN/logOut"
docker compose up -d --build   # bot, worker x2 (replicas в compose), tg_api, db, redis
```

Общий Caddy (один на сервер, SKILL §13) лежит в `deploy/caddy/` и ставится в `/opt/caddy`:

```bash
docker network create webapps_network            # один раз на сервер
mkdir -p /opt/caddy/conf.d/video-uniq && cp deploy/caddy/* /opt/caddy/
cp caddy/video-uniq.conf /opt/caddy/conf.d/video-uniq/
cd /opt/caddy && docker compose up -d             # дальше: docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

`api_id`/`api_hash` — my.telegram.org → API development tools (это ключи приложения, а не
юзербота: локальный Bot API авторизуется токеном бота). Caddy конфиг лежит в
`caddy/video-uniq.conf`, порты наружу не пробрасываются (SKILL §13).

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

Готово: пайплайн рендера, отпечатки и гейт качества, arq-задача (этап 1); приём видео до
150 МБ с проверками, очередь с позицией, выдача копий, очистка файлов, биллинг со Stars и
рефералкой (этап 2). Дальше: меню и настройки. Дорожная карта в `docs/ARCHITECTURE.md`, раздел «План работ».
