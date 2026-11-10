"""Exit 1 if the Bot API server cannot deliver updates to us (run after a deploy).

    docker compose exec -T bot python -m app.scripts.check_webhook

/health only proves the bot process is up; this proves Telegram -> bot delivery works:
no delivery error newer than the bot start, or the queue drains within the timeout.
"""

from __future__ import annotations

import asyncio
import sys
import time

from app.core.bot import build_bot

_TIMEOUT_S = 30


async def check() -> bool:
    bot = build_bot()
    started = time.time() - 5
    try:
        deadline = time.monotonic() + _TIMEOUT_S
        while True:
            info = await bot.get_webhook_info()
            last_error = info.last_error_date.timestamp() if info.last_error_date else 0
            healthy = info.pending_update_count == 0 or last_error < started
            if healthy and last_error < started:
                print(f"webhook ok: pending={info.pending_update_count}")
                return True
            if time.monotonic() > deadline:
                print(
                    f"webhook broken: pending={info.pending_update_count} "
                    f"error={info.last_error_message!r}"
                )
                return False
            await asyncio.sleep(3)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(check()) else 1)
