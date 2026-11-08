# Архитектура video-uniq

## 1. Что такое «вариант»

Соцсети ловят дубли по перцептивному хешу кадров, аудио-отпечатку и хешу файла, а не по
метаданным. Вариант — это точка в непрерывном пространстве параметров обработки. Метаданные —
дополнительный слой.

| Слой | Параметры | Порядок состояний |
|---|---|---|
| Геометрия | зум 1–8 %, сдвиг кадра, нечётное разрешение, pad 0–24 px, SAR | 10^5 |
| Время | скорость ±1–6 % шагом 0.1 %, fps 24–30, смещение старта | 10^3 |
| Цвет | яркость, контраст, насыщенность, оттенок, гамма, виньетка | 10^5 |
| Звук | темп, тон ±2 %, EQ, микрозадержка, стерео-ширина | 10^4 |
| Кодек | CRF/битрейт, GOP, профиль, x264 / x265 / NVENC | 10^2 |
| Метаданные | модель × версия ОС × дата × пояс × координаты × имя файла | 10^8 |

Произведение ~10^25. Миллион уникальных выходов из одного исходника — не ограничение.

## 2. Генератор без повторов

1. `seed = HMAC_SHA256(SERVICE_SECRET, f"{job_id}:{copy_index}")`.
2. Из сида детерминированно разворачивается `RenderPlan` (dataclass со всеми параметрами).
   Один и тот же вход никогда не даёт одинаковую копию; копия воспроизводима по сиду.
3. После рендера считаются: pHash каждого 10-го кадра, Chromaprint аудио, SHA-256 файла.
   Пишутся в `fingerprints`. Если расстояние Хэмминга до любой прежней копии того же исходника
   ниже порога — перерендер с `copy_index + 1000`.
4. Профили метаданных — таблица `metadata_profiles` (JSON). По умолчанию режим «редактор»
   (CapCut, InShot, Premiere, DaVinci) + очистка. Профили «камера» (make/model/OS/GPS) —
   отдельный флаг в настройках пользователя, включается явно, фиксируется в оферте.

## 3. Пайплайн копии (один проход FFmpeg)

```
-ss <start_offset>
-i input
-vf  scale=<w>:<h>, crop=<cw>:<ch>:<x>:<y>, [pad=...], setsar=<sar>,
     eq=brightness=..:contrast=..:saturation=..:gamma=.., hue=h=..,
     [vignette=..], [noise=..],            # только HD+
     setpts=<speed>*PTS, fps=<fps>
-af  atempo=<1/speed>, [asetrate/aresample для тона], equalizer=..., adelay=<ms>
-c:v libx264 -preset <p> -crf <crf> -g <gop> -pix_fmt yuv420p
-c:a aac -b:a <abr>
-map_metadata -1  -metadata ... (из профиля)  -movflags +faststart+use_metadata_tags
```

Имя выходного файла — из шаблона профиля (`IMG_NNNN.mp4`, `VID_YYYYMMDD_HHMMSS.mp4`,
`reel_N.mp4`, `Sequence 01_N.mp4`). Apple-специфичные атомы при необходимости дописываются
exiftool поверх выхода FFmpeg.

## 4. Инфраструктура

- **Очередь**: arq, `queue_name=uniqualize`, `max_tries=3`, `job_timeout=900`.
- **Воркеры**: FFmpeg, лимит 2 CPU / 2 GB на реплику, горизонтальное масштабирование по
  длине очереди. 16-ядерный дедик ≈ 15–20 копий минутного 1080p в минуту ≈ 20k копий/сутки.
  GPU-нода с NVENC для пиков (×5–8).
- **Хранилище**: S3-совместимое (MinIO / Hetzner Object Storage). Исходники и результаты —
  TTL 24 ч, затем удаление. Логируются только хеши.
- **Входы**: Telegram-бот (webhook), веб-кабинет (вход через Telegram Login), REST API
  (`Authorization: Bearer <token>`). Все три кладут задачи в одну очередь.
- **Прокси**: централизованный Caddy вне compose, `webapps_network`, без `ports:`.

## 5. Биллинг и анти-фрод

- Кредиты: `balance`, таблица `transactions`, списание при постановке в очередь, возврат при
  ошибке, сгорание через 30 дней после последней покупки (cron-задача).
- Оплата: Telegram Stars (`send_invoice`, currency `XTR`) с первого дня; карты через
  Tribute / ЮKassa, крипта через Platega — вторая очередь.
- Рефералка: `start=<code>`, +N кредитов обоим после **первой обработки** приглашённого.
- Лимиты: размер файла 150 МБ, число задач в очереди на пользователя, бан-лист SHA-256
  исходников.

## 6. Контроль качества

Для каждой копии считается расстояние pHash и Chromaprint до оригинала; копия выдаётся
только при превышении порога. Пороги калибруются на тестовых аккаунтах (TikTok, Instagram,
YouTube Shorts) партиями по 50 копий. Это замер, который конкурент не делает.

## 7. Схема БД (черновик)

```
users(id, tg_id, username, balance, mode, copies, camera_profiles, api_token, ref_code,
      referred_by, created_at, last_purchase_at)
transactions(id, user_id, amount, kind, provider, provider_payload, created_at)
jobs(id, user_id, source_sha256, source_key, mode, copies, status, created_at, finished_at)
copies(id, job_id, copy_index, seed, plan_json, output_key, profile_id, status)
fingerprints(copy_id, phash_json, chromaprint, sha256)
metadata_profiles(id, kind, make, model, os_version, software, filename_template,
                  tz_offsets, geo_boxes, weight, enabled)
```

## 8. План работ

1. Пайплайн уникализации, генератор плана из сида, реестр отпечатков, режим Base, прогон
   тестового ролика с теми же метриками, что в `docs/COMPETITOR_UNIQUEVID.md`.
2. Кредиты, Stars, рефералка, экраны бота (меню, баланс, настройки, помощь).
3. Профили метаданных: скрипт извлечения exiftool → JSON из FloreView / Daxing / Video-ACID,
   стартовые 20–30 профилей.
4. Режимы HD / HD+, S3, второй воркер, REST API, веб-кабинет.
5. Калибровка порогов на тестовых аккаунтах, оферта, лендинг.
