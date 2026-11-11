"""Where the bot points for the terms and the privacy policy (NotSpy pattern).

Default: our own pages on the public site (/terms, /privacy). The admin can paste any URL
(Telegraph, Notion, Google Docs from a lawyer...) in /admin -> "Юридические ссылки"; it is
stored in app_settings and overrides the default until reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

DOCS = ("terms", "privacy")
_KEY = "legal_url:{}"
_MAX_URL = 500
_ALLOWED = ("https://", "http://", "https://t.me/", "tg://")


class SettingsStore(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str) -> None: ...
    async def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class LegalUrls:
    terms: str
    privacy: str
    terms_custom: bool
    privacy_custom: bool


def is_valid_url(text: str) -> bool:
    return text.startswith(_ALLOWED) and len(text) <= _MAX_URL and " " not in text


class LegalLinks:
    def __init__(self, store: SettingsStore, public_url: str) -> None:
        self._store = store
        self._base = public_url.rstrip("/")

    async def urls(self) -> LegalUrls:
        terms = await self._store.get(_KEY.format("terms"))
        privacy = await self._store.get(_KEY.format("privacy"))
        return LegalUrls(
            terms=terms or f"{self._base}/terms",
            privacy=privacy or f"{self._base}/privacy",
            terms_custom=terms is not None,
            privacy_custom=privacy is not None,
        )

    async def set(self, doc: str, url: str) -> None:
        if doc not in DOCS or not is_valid_url(url):
            raise ValueError(f"bad legal url for {doc!r}")
        await self._store.set(_KEY.format(doc), url)

    async def reset(self) -> None:
        for doc in DOCS:
            await self._store.delete(_KEY.format(doc))
