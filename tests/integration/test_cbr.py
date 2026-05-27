import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.clients import cbr


@pytest.mark.asyncio
async def test_get_rates_from_cache(tmp_db):
    """Если кэш свежий — http не дёргается."""
    rates = {"USD": 90.5, "EUR": 100.1, "RUR": 1.0, "RUB": 1.0}
    payload = json.dumps({"_ts": time.time(), "rates": rates}, ensure_ascii=False)
    await tmp_db.execute(
        "INSERT INTO cookie_store(key, value) VALUES (?, ?)",
        (cbr.CACHE_KEY, payload),
    )
    await tmp_db.commit()

    with patch.object(cbr, "_fetch_cbr", new=AsyncMock(side_effect=AssertionError("must not be called"))):
        result = await cbr.get_rates(tmp_db, force=False)
    assert result["USD"] == 90.5
    assert result["EUR"] == 100.1


@pytest.mark.asyncio
async def test_get_rates_stale_cache_refetches(tmp_db):
    """Старый кэш игнорируется."""
    rates_old = {"USD": 50.0, "RUR": 1.0, "RUB": 1.0}
    stale_ts = time.time() - (cbr.CACHE_TTL_SEC + 10)
    payload = json.dumps({"_ts": stale_ts, "rates": rates_old}, ensure_ascii=False)
    await tmp_db.execute("INSERT INTO cookie_store(key, value) VALUES (?, ?)", (cbr.CACHE_KEY, payload))
    await tmp_db.commit()

    new_rates = {"USD": 100.0, "EUR": 110.0, "RUR": 1.0, "RUB": 1.0}
    with patch.object(cbr, "_fetch_cbr", new=AsyncMock(return_value=new_rates)):
        result = await cbr.get_rates(tmp_db, force=False)
    assert result["USD"] == 100.0


@pytest.mark.asyncio
async def test_get_rates_no_cache_calls_fetch(tmp_db):
    new_rates = {"USD": 88.0, "RUR": 1.0, "RUB": 1.0}
    with patch.object(cbr, "_fetch_cbr", new=AsyncMock(return_value=new_rates)):
        result = await cbr.get_rates(tmp_db)
    assert result["USD"] == 88.0
    # должен записать в БД
    cur = await tmp_db.execute("SELECT value FROM cookie_store WHERE key=?", (cbr.CACHE_KEY,))
    row = await cur.fetchone()
    assert row is not None
    saved = json.loads(row[0])
    assert saved["rates"]["USD"] == 88.0


@pytest.mark.asyncio
async def test_get_rates_force_refetches(tmp_db):
    rates_cached = {"USD": 10.0, "RUR": 1.0, "RUB": 1.0}
    payload = json.dumps({"_ts": time.time(), "rates": rates_cached}, ensure_ascii=False)
    await tmp_db.execute("INSERT INTO cookie_store(key, value) VALUES (?, ?)", (cbr.CACHE_KEY, payload))
    await tmp_db.commit()
    new_rates = {"USD": 200.0, "RUR": 1.0, "RUB": 1.0}
    with patch.object(cbr, "_fetch_cbr", new=AsyncMock(return_value=new_rates)):
        result = await cbr.get_rates(tmp_db, force=True)
    assert result["USD"] == 200.0


@pytest.mark.asyncio
async def test_get_rates_fetch_failure_returns_static(tmp_db):
    from app.parsers import salary as salary_parser

    with patch.object(cbr, "_fetch_cbr", new=AsyncMock(side_effect=RuntimeError("net"))):
        result = await cbr.get_rates(tmp_db)
    # упало → static FX_TO_RUB
    assert result == salary_parser.FX_TO_RUB


@pytest.mark.asyncio
async def test_refresh_salary_module_updates_static(tmp_db):
    new_rates = {"USD": 95.5, "EUR": 105.5, "RUR": 1.0, "RUB": 1.0}
    with patch.object(cbr, "_fetch_cbr", new=AsyncMock(return_value=new_rates)):
        info = await cbr.refresh_salary_module(tmp_db)
    assert info["count"] >= 4
    assert info["USD"] == 95.5
    assert info["EUR"] == 105.5
    # модуль salary_parser обновился: берём из самого cbr (тот же объект, что использует и refresh)
    assert cbr.salary_parser.FX_TO_RUB["USD"] == 95.5


@pytest.mark.asyncio
async def test_fetch_cbr_parses_valute(monkeypatch):
    """Проверяем парсинг JSON ответа ЦБ."""

    class FakeResp:
        status_code = 200
        content = b"x"

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "Valute": {
                    "USD": {"Value": 95.0, "Nominal": 1},
                    "JPY": {"Value": 60.0, "Nominal": 100},
                    "BAD": {"Value": None, "Nominal": 1},
                }
            }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(cbr.httpx, "AsyncClient", FakeClient)
    rates = await cbr._fetch_cbr()
    assert rates["USD"] == 95.0
    assert rates["JPY"] == 0.6  # 60 / 100
    assert "BAD" not in rates
    assert rates["RUR"] == 1.0
    assert rates["RUB"] == 1.0
