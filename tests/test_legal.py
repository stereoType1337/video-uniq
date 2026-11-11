from __future__ import annotations

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.core.config import settings
from app.web import legal


@pytest.fixture(autouse=True)
def _fresh_render() -> None:
    legal.render.cache_clear()


async def _get(path: str) -> tuple[int, str]:
    app = web.Application()
    legal.setup(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(path)
        return response.status, await response.text()


@pytest.mark.parametrize("path", ["/terms", "/privacy"])
async def test_pages_render_without_leftover_placeholders(path: str) -> None:
    status, html = await _get(path)
    assert status == 200 and "<h1>" in html
    assert not re.findall(r"\$\w+", html)


async def test_terms_numbers_follow_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "credit_ttl_days", 45)
    monkeypatch.setattr(settings, "free_credits", 7)
    _, html = await _get("/terms")
    assert "действуют 45 дней" in html and "начисляется 7 бесплатных" in html
    assert "hd_plus — 3 кр." in html, "prices come from the billing table"


async def test_draft_banner_until_operator_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "operator_name", "")
    _, draft = await _get("/terms")
    assert "Черновик" in draft and "[наименование исполнителя]" in draft
    legal.render.cache_clear()
    monkeypatch.setattr(settings, "operator_name", "ИП Тестов <script>")
    _, final = await _get("/terms")
    assert "Черновик" not in final
    assert "ИП Тестов &lt;script&gt;" in final, "values are HTML-escaped"


async def test_privacy_states_real_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    _, html = await _get("/privacy")
    assert f"<b>{settings.files_ttl_hours} часов</b>" in html
    assert f"{settings.fingerprint_retention_days} дней" in html
    assert "Германии" in html
