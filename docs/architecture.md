# Архитектура

[← Назад к README](../README.md)

---

## Структура каталогов

```
app/
  clients/         — HH stealth-клиент, cookies, rate-limit, CBR курсы
  collector/       — сбор вакансий / откликов / резюме / backfill / refresh_resume_search_token
  db/              — schema.sql + per-table repos (aiosqlite), включая llm_repo
  llm/             — изолированный LLM-пайплайн (client / prompts / registry / settings / tasks)
  parsers/         — extract_initial_state, stack/level/remote/salary
  scoring/         — match-score + predict (heuristic + sklearn, cross-val AUC)
  web/             — FastAPI app + Jinja2 templates (index/funnel/vacancy/compare/analytics/jobs/llm_logs/profile/logs)
  events.py        — in-process event bus + SSE
  scheduler.py     — apscheduler (8 джобов: personal × 2, backfill, searches, fx, ml, dedup, llm_parse_requirements)
  tasks.py         — реестр фоновых задач с прогресс-баром + SSE
data/              — SQLite БД + ML model + dataset (gitignored)
scripts/           — export_dataset.py, seed_demo.py, llm_parse.py
tests/             — pytest: tests/{unit, integration, e2e}/ — 476 тестов
.github/workflows/ — CI GitHub Actions (pytest на каждый push)
docs/              — внутренняя документация + images/
```

## Точки входа

### Страницы

| URL | Что |
|---|---|
| `/` | Таблица вакансий со всеми фильтрами, колоночные popup ▾, multi-column sort (Shift+клик), bulk-смена статуса |
| `/funnel` | Воронка откликов, топ работодателей, гистограммы |
| `/profile` | Импорт из HH-резюме + ручная правка для match-score + переключение LLM-модели и включённых анализаторов |
| `/vacancy/{id}` | Детальная карточка с разбивкой match-score + LLM-блоки (требования / зарплата / тип компании / резюме / match-эссе / подготовка к собесу) |
| `/compare?ids=1&ids=2` | Поколоночное сравнение до 6 вакансий |
| `/analytics` | Топ-стек, по строгости/категории, тип компании, топ-вопросов и тем к собесу |
| `/jobs` | Журнал прогонов всех фоновых джобов (фильтры + развёртка JSON-результата) |
| `/llm-logs` | Журнал LLM-вызовов с фокусом на конкретный run |
| `/logs` | Лог HTTP-запросов к HH с фильтрами по статусу, путям, ошибкам |

### API

| Endpoint | Что |
|---|---|
| `GET /api/health` | JSON со статусом клиента, расписания, активных задач |
| `GET /api/status` / `GET /api/status/stream` | Снимок статуса и SSE-стрим вместо polling — UI авто-обновляется на done-джобах |
| `GET /api/export.csv` / `GET /api/export.json` | Экспорт текущей выборки |
| `POST /api/dedup` | Помечает дубликаты по нормализованной паре (название + компания) как «Скип» |
| `POST /api/ml/train` | Прогон обучения ML-модели на текущих данных |
| `POST /api/scheduler/{job_id}/run-now` | Ручной запуск любого джоба |
| `POST /api/client/unpause` | Сброс антибот-паузы из UI |
| `POST /api/searches` / `DELETE /api/searches/{sid}` | Создание/удаление сохранённых поисков |
| `POST /api/searches/recommendations` | Добавляет рекомендации hh.ru как saved_search (с `?resume=<hash>`, см. scraping.md) |
| `POST /api/searches/{bulk-max-pages, bulk-early-stop}` | Массовая правка глубины и порога early-stop по всем активным поискам |
| `POST /api/searches/{sid}/{run, toggle, max-pages}` | Прогон одного поиска / включить-выключить / правка глубины |
| `POST /api/searches/sync-all` | Прогон всех активных |
| `POST /api/vacancies/bulk-status` | Bulk-смена статуса выделенных строк таблицы |
| `POST /api/vacancy/{vid}/status` | Смена статуса одной вакансии |
| `POST /api/vacancy/{vid}/refresh` | Перетянуть карточку с HH |
| `POST /api/vacancy/{vid}/analyze` | Прогнать выбранные LLM-анализаторы (`kinds=[...]`) по одной вакансии |
| `GET /api/llm/analyzers` / `POST /api/llm/analyzers/enabled` | Список анализаторов и глобальный набор включённых |
| `GET /api/llm/runs` | Лента LLM-вызовов |
| `POST /api/llm/parse-corpus` | Прогон включённых анализаторов по необработанному корпусу |
| `POST /api/settings/llm-model` | Переключение активной LLM-модели (qwen3:14b / qwen2.5:14b / llama3.1:8b / custom) |
| `POST /api/profile` | Сохранение профиля для match-score |

## База данных

`app/db/schema.sql` — все таблицы, индексы.

**Основной слой:** `vacancies`, `vacancy_status`, `vacancy_collected_via`, `searches`, `search_vacancy_seen`, `employers`, `negotiations`, `status_snapshots`, `profile`, `request_logs`, `job_runs`, `cookie_store`.

**LLM-слой:**
- `llm_runs` — полный лог каждого вызова Ollama: `task_kind`, `target_kind/id`, `model`, `prompt`, `response`, `parsed`, `tokens`, `latency`, `ok`. Источник для `/llm-logs`.
- `vacancy_analysis` — универсальное хранилище результата анализа: `(vacancy_id, kind) → parsed JSON`. Все простые анализаторы (`salary`, `company_kind`, `summary`, `match_essay`, `interview_prep`) пишутся сюда.
- `vacancy_requirements` — отдельная нормализованная таблица для `requirements` (must/nice/plus × stack/exp/soft/edu/other), потому что по ней строится `/analytics` и фильтрация в таблице.

Все три читаются на `/vacancy/{id}` и `/analytics`. Если LLM выключен — таблицы пустые, UI просто скрывает соответствующие блоки.

## LLM-пайплайн (`app/llm/`)

Изолированный модуль, не зависит от `HHClient` и от scheduler — его можно дёргать как из cron, так и из HTTP-эндпоинта.

```
app/llm/
  client.py         — async-клиент к Ollama /api/generate (httpx). think=False для qwen3*/deepseek-r1*
  prompts.py        — текстовые шаблоны под каждый анализатор
  registry.py       — реестр Analyzer-ов + analyze_one()
  settings.py       — выбор модели и набора включённых анализаторов из profile
  tasks/
    requirements.py — крон-задача llm_parse_requirements (батч по 20 необработанных)
```

**Реестр анализаторов (`ANALYZERS` в `registry.py`):** 6 базовых — `requirements`, `salary`, `company_kind`, `summary`, `match_essay`, `interview_prep` — плюс `soft_skills_employer`. По умолчанию включён только `requirements`, остальные — на `/profile`.

**Поток:**

```
analyze_one(vacancy_id, kinds=[...], model=None)
  └─► для каждого kind:
        ├─► _load_vacancy_for_analysis() (title, description, key_skills, profile)
        ├─► prompts.<kind>() → prompt
        ├─► client.generate(model, prompt, format="json", think=auto)
        ├─► json.loads(response) → parsed
        ├─► llm_runs INSERT (prompt + response + parsed + tokens + latency + ok)
        └─► vacancy_analysis / vacancy_requirements UPSERT
```

**Модель** хранится в `profile.llm_model`, переключается через `POST /api/settings/llm-model` или на `/profile`. Для thinking-моделей (`qwen3*`, `deepseek-r1*`) клиент автоматически выставляет `think=False` — иначе в `format=json` модель часто возвращает `{}` (вся «мысль» съедается до структурированного ответа).

Ollama — внешняя зависимость, опциональная. Если её нет — `llm_parse_requirements` тихо падает с network-error, и это видно на `/llm-logs`.

## Apscheduler — фоновые задачи

**Зачем нужны фоновые джобы.** Поиск работы — это процесс на недели. Ручной «нажми обновить» = пропущенные приглашения, неактуальные ЗП, устаревший match-score. Apscheduler гарантирует что:
- состояние откликов всегда свежее (HR посмотрел / пригласил / отказал — увидишь сразу)
- новые вакансии из сохранённых поисков подтягиваются без участия пользователя
- ML переобучается ночью на новых данных, не блокируя UI
- LLM-анализ корпуса идёт фоном, а не блокирует открытие карточки

| Джоб | Когда | Что делает |
|---|---|---|
| `personal_refresh` | каждые 6 ч | инкрементальный sync откликов (smart-stop по `lastModified`) |
| `personal_full_refresh` | 02:00 ежедневно | полный sync (на случай если HH молча поменял состояние старого отклика) |
| `backfill_pending` | каждые 20 мин | дотягивает полные карточки вакансий из откликов (пачкой по 25) |
| `sync_searches` | каждые 4 ч | прогон всех активных сохранённых поисков (включая рекомендации с `refresh_resume_search_token` перед стартом) |
| `dedup_vacancies` | 03:45 ежедневно | пометка дубликатов по нормализованной паре (название + компания) |
| `fx_refresh` | 03:30 ежедневно | курсы валют ЦБ (56 валют) |
| `ml_retrain` | 04:00 ежедневно | переобучает LogisticRegression если ≥5 positives и ≥5 negatives (cross-val AUC + StratifiedKFold в логах) |
| `llm_parse_requirements` | каждый час | прогоняет включённые анализаторы по 20 необработанным вакансиям через Ollama |
| `cover_letter_generate` | каждые 2 ч | генерит сопроводительные письма для вакансий «в пайплайне» (есть отклик или статус interested/applied) без письма |
| `embed_vacancies` | каждые 30 мин | RAG-индексация: эмбедит вакансии без вектора (no-op, если extra `rag` не установлен) |

**Под капотом:**
- `MemoryJobStore` (не SQLAlchemy) — потому что `hh_client` с httpx.AsyncClient не picklable. Расписание восстанавливается из кода при старте, история прогонов хранится в таблице `job_runs`
- декоратор `@_record(job_id)` оборачивает каждый джоб — пишет в `job_runs` start/finish/error и регистрирует asyncio-таск в `_running` (для остановки из UI)
- защита от двойного запуска: `task_mod.TaskAlreadyRunning` — если джоб уже идёт (ручной или плановый), повторный запуск отклоняется
- защита от anti-bot: джобы которым нужен `hh_client` (`backfill_pending`, `personal_refresh`, `sync_searches`) проверяют `client.status.paused_now` и пропускают тик, если клиент на паузе
- LLM/RAG-джобы от `hh_client` не зависят — работают даже когда HH на паузе

**Ручной запуск и остановка.** Каждый джоб видно в правой панели «Статус» → раздел «Расписание» → кнопка **▶**. Прогресс идёт через SSE-канал `/api/status/stream`, виден в реальном времени. Полная история прогонов с фильтрами, сортировкой и **кнопкой ⏹ стоп** (отменяет любой running-прогон через `cancel_run` → `CancelledError` → статус `cancelled`) — `/jobs`. Если клиент на паузе из-за anti-bot — кнопка вернёт «клиент HH на паузе ещё ~Nм».

## RAG (`app/llm/rag.py`, опционально)

RAG включается отдельным extra `rag` (ставит `sqlite-vec`); без него `rag.is_available()` → False и все RAG-точки аккуратно выключены.

- **Хранение.** Вектор — в vec0-таблице `vec_vacancies` (sqlite-vec, `distance_metric=cosine`), создаётся лениво. Мета (модель, dim, `source_hash` для пере-эмбеддинга) — в обычной таблице `vacancy_embeddings` (даёт coverage даже без расширения).
- **Загрузка расширения.** `rag.load_vec(conn)` грузит sqlite-vec в каждое aiosqlite-соединение, которое обращается к vec0 (идемпотентно). Общий `get_db()` не трогаем — не-RAG путь без оверхеда.
- **Эмбеддинги.** `llm_client.embed()` → Ollama `/api/embed` (`nomic-embed-text`, dim 768). Джоб `embed_vacancies` индексирует вакансии без вектора; каждый вызов логируется в `llm_runs` (task_kind=`embed`).
- **Retrieval.** `similar(vid)` — KNN по вектору вакансии (блок «Похожие вакансии» на `/vacancy`); `semantic_search(query)` — эмбеддинг запроса → KNN (`/api/rag/search`).
- **Generation (полный RAG).** `ask(query)` = semantic_search → собирает контекст топ-k вакансий → `llm_client.generate` с требованием ссылаться на `[#id]` → ответ + источники (`/api/rag/ask`, страница `/search`). Лог в `llm_runs` (task_kind=`rag_answer`).
- **Ограничение.** dim vec0-таблицы фиксирован (768 под nomic). Смена embed-модели с другим dim требует пересоздания `vec_vacancies`.

## Уведомления (`app/notify.py`)

Два независимых канала, оба включаются на `/profile` (флаги в `cookie_store`):
- **macOS** (`notifications.enabled`) — `osascript display notification`, fire-and-forget.
- **Telegram** (`notifications.telegram`) — Bot API `sendMessage`; токен/chat_id из `.env` (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), без них канал — no-op.

`dispatch(db, title, message, event=...)` рассылает во все включённые каналы. **Категории событий** (`notifications.events`, по умолчанию вакансии/собесы/ошибки): `vacancies` (новые с match ≥ порога — порог в `notifications.match_threshold`), `negotiations` (приглашения/собесы), `job_errors`, `job_done`. Если категория выключена — `dispatch` молчит. Завершение/ошибки джоб эмитит декоратор `_record` через `_maybe_notify_job`.

## Soft-skills score работодателя

`employer_soft_score(data)` (`app/scoring/match.py`) сводит анализ `soft_skills_employer` (тон/WLB/рост) в число 0–100. `funnel_repo.soft_scores_by_employer` усредняет по вакансиям работодателя → колонка «Soft» в `/funnel`; на `/vacancy` — бейдж по конкретной вакансии.
