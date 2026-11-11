"""Public terms (/terms) and privacy policy (/privacy), served by the bot's aiohttp app.

Texts live in app/web/legal/*.html as string.Template; numbers (credit TTL, bonuses, file
retention) come from settings so the documents always match what the bot really does.
Operator details come from .env; until OPERATOR_NAME is set the pages carry a "draft" banner.
"""

from __future__ import annotations

from functools import lru_cache
from html import escape
from pathlib import Path
from string import Template

from aiohttp import web

from app.core.config import Settings, settings
from app.services.billing import PRICE_PER_COPY

_DIR = Path(__file__).resolve().parent / "legal"
_TITLES = {"terms": "Оферта", "privacy": "Политика конфиденциальности"}
_PAGE = Template("""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title · $service</title>
<style>
  :root { color-scheme: light dark; }
  body { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; font: 16px/1.55 system-ui,
         -apple-system, "Segoe UI", sans-serif; }
  h1 { font-size: 1.6em; line-height: 1.25; } h2 { font-size: 1.2em; margin-top: 1.8em; }
  .meta { opacity: .7; } .draft { padding: 12px 16px; border-radius: 8px;
          background: #fff3cd; color: #664d03; }
  table { border-collapse: collapse; width: 100%; font-size: .95em; }
  td, th { border: 1px solid #8884; padding: 8px; vertical-align: top; text-align: left; }
  nav { margin-bottom: 24px; } nav a { margin-right: 16px; }
</style></head><body>
<nav><a href="/terms">Оферта</a><a href="/privacy">Конфиденциальность</a></nav>
$banner
$body
</body></html>""")
_DRAFT = (
    '<p class="draft">Черновик: реквизиты исполнителя ещё не заполнены. '
    "Документ будет окончательным после их указания.</p>"
)


def _values(cfg: Settings) -> dict[str, str]:
    prices = ", ".join(f"{m.value} — {p} кр." for m, p in PRICE_PER_COPY.items())
    return {
        "service": escape(cfg.service_name),
        "operator": escape(cfg.operator_name or "[наименование исполнителя]"),
        "operator_details": escape(cfg.operator_details or "[реквизиты: ИНН / ОГРН(ИП)]"),
        "email": escape(cfg.contact_email or "[e-mail для обращений]"),
        "support": escape(cfg.support_contact or "/support в боте"),
        "bot": escape(cfg.bot_username and f"@{cfg.bot_username}" or "бот сервиса"),
        "site": escape(cfg.public_url.rstrip("/")),
        "effective_date": escape(cfg.legal_effective_date),
        "hosting": escape(cfg.hosting_provider),
        "credit_ttl_days": str(cfg.credit_ttl_days),
        "files_ttl_hours": str(cfg.files_ttl_hours),
        "free_credits": str(cfg.free_credits),
        "referral_bonus": str(cfg.referral_bonus),
        "fingerprint_days": str(cfg.fingerprint_retention_days),
        "prices": escape(prices),
    }


@lru_cache(maxsize=4)
def render(name: str) -> str:
    values = _values(settings)
    body = Template((_DIR / f"{name}.html").read_text()).substitute(values)
    return _PAGE.substitute(
        title=_TITLES[name],
        service=values["service"],
        banner="" if settings.operator_name else _DRAFT,
        body=body,
    )


async def terms(_: web.Request) -> web.Response:
    return web.Response(text=render("terms"), content_type="text/html")


async def privacy(_: web.Request) -> web.Response:
    return web.Response(text=render("privacy"), content_type="text/html")


def setup(app: web.Application) -> None:
    app.router.add_get("/terms", terms)
    app.router.add_get("/privacy", privacy)
