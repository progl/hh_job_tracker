# Тесты

[← Назад к README](../README.md)

---

476 тестов, прогон ~25 сек. Структура: `tests/{unit, integration, e2e}/`.

## Слои

| Слой | Файлов | Тестов | Что покрывает |
|---|---|---|---|
| **unit** | 18 | ~172 | парсеры (level/remote/salary/stack/state/cookies/headers), скоринг (match/predict/ml), детектор архивности, events, rate-limit, tasks, scheduler, LLM-клиент к Ollama (мокнутый httpx) |
| **integration** | 20 | ~225 | репозитории (`vacancies/negotiations/profile/employers/job_runs/logs/searches/funnel/llm_repo`), cookies-jar, cbr-клиент, дедуп, collector/favorites, collector/vacancies (включая `workFormats` варианты), collector/personal (smart-stop, `refresh_resume_search_token`), LLM-реестр анализаторов, LLM-настройки, llm_parse_requirements |
| **e2e** | 6 | ~82 | FastAPI через `httpx.ASGITransport` с мокнутыми HHClient/scheduler/cbr/Ollama: `/api/*`, `/analytics`, `/jobs`, `/llm-logs`, рекомендации hh.ru, `/api/status/stream` |

## Команды

```bash
make test       # все тесты с -v
make coverage   # тесты + текстовый отчёт + HTML в htmlcov/index.html
make lint       # ruff check
make format     # ruff format
make check      # lint + format check + pytest (для CI/pre-commit)
```

## Покрытие — 89% общее, ключевые модули 88–100%

| Модуль | Покрытие |
|---|---|
| `collector/favorites.py` | 100% |
| `db/funnel_repo.py` | 100% |
| `db/llm_repo.py` | ≈100% |
| `db/logs_repo.py` | 97% |
| `parsers/salary.py` | 97% |
| `db/job_runs_repo.py` | 96% |
| `db/searches_repo.py` | 94% |
| `scoring/predict.py` | 92% |
| `parsers/remote.py` | 91% |
| `llm/registry.py` | ≈90% |
| `db/profile_repo.py` | 88% |
| `scoring/match.py` | 88% |
| `app/tasks.py` | 88% |
| `db/vacancies_repo.py` | 76% |
| `scheduler.py` | 54% |
| `web/app.py` | 49% (lifespan мокается в e2e) |
| `clients/hh.py`, `collector/personal.py`, `collector/vacancies.py` | 11–31% (требуют живого HH-клиента) |

## CI на GitHub Actions

`.github/workflows/test.yml` запускает все 476 тестов на каждый push в `main` и на каждый PR. Покрытие парсится из `coverage.xml` и пишется в job summary. Первый прогон ~1.5–2 мин, последующие с кэшем uv — около минуты.

## Как устроены e2e

`tests/e2e/conftest.py` поднимает FastAPI через `httpx.ASGITransport` с заглушенным lifespan:
- `cbr_client.refresh_salary_module` → no-op (не идём в ЦБ)
- `hh_client.start/close` → no-op (не открываем сетевые сокеты)
- `scheduler_mod.start/shutdown` → no-op (без фоновых тиков)
- `ml_module.reload_model` → no-op
- Ollama не вызывается — LLM-эндпоинты тестируются с моком `httpx.AsyncClient`

БД — `tmp_path/e2e.db`, изолированная per-тест. Никакого взаимодействия с реальной `data/hh.db`.

## Изоляция ML-модели

`tests/conftest.py::pytest_sessionstart` сразу при старте сессии переписывает `app.scoring.ml.MODEL_PATH` на несуществующий путь и обнуляет глобальный кэш `_MODEL`. Иначе, если в `data/model.pkl` лежит реальная обученная модель, она бы грузилась через глобальный кэш и тесты `test_scoring_predict.*` (которые ждут именно эвристику, а не ML) становились flaky. Дополнительно `autouse`-фикстура `_isolate_ml_model_path` сбрасывает кэш перед каждым тестом — на случай, если предыдущий тест monkey-патчил путь и что-то загрузил.

`DB_PATH` тоже подменяется в conftest до любого импорта `app.*`, иначе pydantic-settings подхватит реальный `.env` и тесты пойдут писать в `data/hh.db`.
