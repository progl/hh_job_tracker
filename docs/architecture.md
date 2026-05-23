# Архитектура

[← Назад к README](../README.md)

---

## Структура каталогов

```
app/
  clients/         — HH stealth-клиент, cookies, rate-limit, CBR курсы
  collector/       — сбор вакансий / откликов / резюме / backfill
  db/              — schema.sql + per-table repos (aiosqlite)
  parsers/         — extract_initial_state, stack/level/remote/salary
  scoring/         — match-score + predict (heuristic + sklearn)
  web/             — FastAPI app + Jinja2 templates
  events.py        — in-process event bus + SSE
  scheduler.py     — apscheduler (6 джобов: sync, backfill, ml, fx)
  tasks.py         — реестр фоновых задач с прогресс-баром + SSE
data/              — SQLite БД + ML model + dataset (gitignored)
scripts/           — export_dataset.py, seed_demo.py (фикстура для скринов)
tests/             — pytest: unit / integration / e2e (237 тестов, 66% coverage)
.github/workflows/ — CI GitHub Actions (pytest на каждый push)
docs/              — внутренняя документация + images/
```

## Точки входа

| URL | Что |
|---|---|
| `/` | Таблица вакансий со всеми фильтрами |
| `/funnel` | Воронка откликов, топ работодателей, гистограммы |
| `/profile` | Импорт из HH-резюме + ручная правка для match-score |
| `/vacancy/{id}` | Детальная карточка с разбивкой match-score |
| `/compare?ids=1&ids=2` | Поколоночное сравнение до 6 вакансий |
| `/logs` | Логи всех HTTP-запросов к HH (с фильтрами по статусу, путям, ошибкам) |
| `/api/health` | JSON со статусом клиента, расписания, активных задач |
| `/api/export.csv` / `/api/export.json` | Экспорт текущей выборки |

## Apscheduler — фоновые задачи

**Зачем нужны фоновые джобы.** Поиск работы — это процесс на недели. Ручной «нажми обновить» = пропущенные приглашения, неактуальные ЗП, устаревший match-score. Apscheduler гарантирует что:
- состояние откликов всегда свежее (HR посмотрел / пригласил / отказал — увидишь сразу)
- новые вакансии из сохранённых поисков подтягиваются без участия пользователя
- ML переобучается ночью на новых данных, не блокируя UI

| Джоб | Когда | Что делает |
|---|---|---|
| `personal_refresh` | каждые 6 ч | инкрементальный sync откликов (smart-stop по `lastModified`) |
| `personal_full_refresh` | 02:00 ежедневно | полный sync (на случай если HH молча поменял состояние старого отклика) |
| `backfill_pending` | каждые 20 мин | дотягивает полные карточки вакансий из откликов (пачкой по 25) |
| `sync_searches` | каждые 4 ч | прогон всех активных сохранённых поисков |
| `fx_refresh` | 03:30 ежедневно | курсы валют ЦБ (56 валют) |
| `ml_retrain` | 04:00 ежедневно | переобучает LogisticRegression если ≥10 positives |

**Под капотом:**
- `MemoryJobStore` (не SQLAlchemy) — потому что `hh_client` с httpx.AsyncClient не picklable. Расписание восстанавливается из кода при старте, история прогонов хранится в таблице `job_runs`
- декоратор `@_record(job_id)` оборачивает каждый джоб — пишет в `job_runs` start/finish/error
- защита от двойного запуска: `task_mod.TaskAlreadyRunning` — если джоб уже идёт (ручной или плановый), повторный запуск отклоняется
- защита от anti-bot: джобы которым нужен `hh_client` (`backfill_pending`, `personal_refresh`, `sync_searches`) проверяют `client.status.paused_now` и пропускают тик, если клиент на паузе

**Ручной запуск.** Каждый джоб видно в правой панели «Статус» → раздел «Расписание» → кнопка **▶**. Прогресс идёт через SSE-канал, виден в реальном времени. Если клиент на паузе из-за anti-bot — кнопка вернёт «клиент HH на паузе ещё ~Nм».
