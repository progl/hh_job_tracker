# Антибот

[← Назад к README](../README.md)

---

HH активно защищается: rate-limit, fingerprint браузера, проверка sec-ch-* и referer-цепочек, периодический 403-challenge. Архитектура клиента (`app/clients/hh.py` + `rate_limit.py` + `headers.py` + `cookies.py`) построена вокруг одного принципа: **не отличаться от живого пользователя в Chrome**.

## 1. Rate-limit с jitter (`rate_limit.py`)

Запросы НЕ идут пачкой:
- **3–6 сек** между запросами (`random.uniform`) — имитирует чтение страницы
- **минутный лимит ≈25 запросов** — после этого ждём до конца минуты + 0.5–2 сек джиттера
- **«rest»** — каждые 50 запросов пауза 45±10 сек, имитация «ушёл налить кофе»
- всё под `asyncio.Lock` — никаких гонок при параллельных вызовах из джобов

Это не «защитный лимит на всякий случай», а **поведенческий fingerprint**: реальные клики ровно так и распределены по времени.

## 2. Реалистичные браузерные headers (`headers.py`)

Каждый запрос несёт полный набор Chrome-headers, не дефолтный httpx:
- `user-agent`, `sec-ch-ua`, `sec-ch-ua-platform`, `sec-ch-ua-mobile` — берутся из `.env` (скопированы из реального DevTools)
- `accept` с полной цепочкой (`text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,...`)
- `sec-fetch-dest=document`, `sec-fetch-mode=navigate`, `sec-fetch-user=?1`, `sec-fetch-site=same-origin`
- `upgrade-insecure-requests: 1`, `priority: u=0, i`

XHR-запросы (если потребуются) идут с отдельным набором (`headers_xhr`): `accept: */*`, `sec-fetch-mode=cors`, `origin`.

## 3. Smart referer (`hh.py:_smart_referer`)

Реальный пользователь приходит на `/vacancy/{id}` НЕ с пустым referer, а с поиска или из откликов. Клиент это эмулирует:

```python
/vacancy/{id}     → последний /search/vacancy или /applicant/*
/search/vacancy   → /
/applicant/...    → /
/applicant/resumes → /applicant/negotiations
/resume/{id}      → /applicant/resumes
```

Без правильного referer запрос на `/vacancy/{id}` ловит 403 на втором-третьем срабатывании.

## 4. HTTP/2 + persistent connection

`httpx.AsyncClient(http2=True)` — HH отдаёт HTTP/2, и любой не-HTTP/2 клиент сразу подозрителен. Один клиент на весь lifetime приложения, никаких пересозданий per request.

## 5. Persistent cookie jar (`cookies.py`)

HH ротирует часть cookies на каждом запросе через `Set-Cookie` (anti-bot tokens). Если их не сохранять — следующий запуск приложения уже под подозрением.

- jar пишется в SQLite `cookie_store` через `save_jar()` после каждого джоба
- при старте — `load_jar()` восстанавливает состояние
- если в `.env` обновился `hhtoken`/`hhuid`/`_xsrf` (стабильные cookies сессии) — БД-jar выбрасывается, берётся свежий из `.env`. Это решает кейс «протухла сессия — обновил .env — рестартнул»

## 6. Ветвление 403 (`hh.py:get_page`)

Не всякий 403 — anti-bot. HH отдаёт 403 и на скрытых/снятых вакансиях. Если их считать challenge — мгновенно влетим в паузу при backfill старых откликов.

```
GET → response
 ├─► 200            → OK, сбрасываем consecutive_403=0
 ├─► 30x → login    → SessionExpiredError (надо обновить .env)
 ├─► 30x → другой   → возвращаем "" (логируем редирект)
 ├─► 403:
 │    ├─► тело содержит "Вам недоступна эта вакансия" / "HH-PageLayout-Description"
 │    │   → VacancyUnavailableError (НЕ счётчик anti-bot, помечаем disappeared_at)
 │    └─► иначе:
 │         ├─► consecutive_403 < 3 → одиночный 403, warn (вероятно снятая вакансия)
 │         └─► consecutive_403 ≥ 3 → AntibotChallengeError + auto-pause
 │              ├─► первая пауза: 10 минут
 │              └─► повторная:    30 минут (экспоненциальный backoff)
 └─► network error  → пробрасываем httpx.RequestError
```

`paused_until` = `time.monotonic() + N сек`. Любой `get_page` во время паузы сразу падает с `AntibotChallengeError`, не делая запрос. Джобы это уважают — `sync_searches`/`backfill_pending` видят `client.status.paused_now` и skip-ят.

## 7. Кнопка «снять паузу» в UI

Если уверен что куки свежие — `client.unpause()` в Статус-панели сбрасывает `paused_until`. Счётчик `challenge_count` остаётся (для статистики).

## 8. Логи всех запросов

Каждый запрос пишется в `request_logs`: path, params, referer, status, duration, kind (`ok`/`hidden`/`antibot`/`network`/`skipped_paused`). Видно на `/logs` с фильтрами. Если HH что-то поменял — это первое место куда смотреть.
