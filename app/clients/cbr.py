"""Кеш курсов ЦБ РФ → рубли.

Источник: https://www.cbr-xml-daily.ru/daily_json.js (бесплатно, без auth, обновляется ежедневно).
Сохраняем в cookie_store.value под ключом 'fx_rates' как JSON.
"""

import json
import logging
import time
from typing import Any

import aiosqlite
import httpx

from app.parsers import salary as salary_parser

log = logging.getLogger(__name__)

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
CACHE_KEY = "fx_rates"
CACHE_TTL_SEC = 24 * 3600


async def _fetch_cbr() -> dict[str, float]:
    import time as _t

    from app.db.logs_repo import log_request

    t0 = _t.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(CBR_URL)
            r.raise_for_status()
            data = r.json()
        dt = int((_t.monotonic() - t0) * 1000)
        log_request(path=CBR_URL, status=r.status_code, duration_ms=dt, size_bytes=len(r.content), kind="cbr")
    except Exception as e:
        log_request(path=CBR_URL, error=f"{e}", kind="cbr_err")
        raise
    rates = {"RUR": 1.0, "RUB": 1.0}
    for code, info in (data.get("Valute") or {}).items():
        value = info.get("Value")
        nominal = info.get("Nominal") or 1
        if value:
            rates[code] = value / nominal
    return rates


async def get_rates(db: aiosqlite.Connection, force: bool = False) -> dict[str, float]:
    if not force:
        cur = await db.execute("SELECT value, updated_at FROM cookie_store WHERE key=?", (CACHE_KEY,))
        row = await cur.fetchone()
        if row:
            payload = json.loads(row[0])
            if (time.time() - payload.get("_ts", 0)) < CACHE_TTL_SEC:
                rates = payload.get("rates") or {}
                if rates:
                    return rates
    try:
        rates = await _fetch_cbr()
    except Exception as e:
        log.warning("CBR fetch failed: %s, fallback to static rates", e)
        return salary_parser.FX_TO_RUB
    await db.execute(
        "INSERT INTO cookie_store(key, value, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (CACHE_KEY, json.dumps({"_ts": time.time(), "rates": rates}, ensure_ascii=False)),
    )
    await db.commit()
    return rates


async def refresh_salary_module(db: aiosqlite.Connection) -> dict[str, Any]:
    rates = await get_rates(db, force=False)
    salary_parser.FX_TO_RUB.update(rates)
    return {"count": len(rates), "USD": rates.get("USD"), "EUR": rates.get("EUR")}
