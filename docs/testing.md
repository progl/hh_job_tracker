# Тесты

[← Назад к README](../README.md)

---

237 тестов, прогон ~10 сек.

## Слои

| Слой | Файлов | Тестов | Что покрывает |
|---|---|---|---|
| **unit** | 15 | 121 | парсеры (level/remote/salary/stack/state/cookies/headers), скоринг (match/predict/ml), детектор архивности, events, rate-limit, tasks, scheduler |
| **integration** | 13 | 102 | репозитории (`vacancies/negotiations/profile/employers/job_runs/logs/searches/funnel`), cookies-jar, cbr-клиент, дедуп, collector/favorites |
| **e2e** | 1 | 14 | FastAPI через `httpx.ASGITransport` с мокнутыми HHClient/scheduler/cbr |

## Команды

```bash
make test       # все тесты с -v
make coverage   # тесты + текстовый отчёт + HTML в htmlcov/index.html
```

## Покрытие — 66% общее, ключевые модули 88–100%

| Модуль | Покрытие |
|---|---|
| `collector/favorites.py` | 100% |
| `db/funnel_repo.py` | 100% |
| `db/logs_repo.py` | 97% |
| `parsers/salary.py` | 97% |
| `db/job_runs_repo.py` | 96% |
| `db/searches_repo.py` | 94% |
| `scoring/predict.py` | 92% |
| `parsers/remote.py` | 91% |
| `db/profile_repo.py` | 88% |
| `scoring/match.py` | 88% |
| `app/tasks.py` | 88% |
| `db/vacancies_repo.py` | 76% |
| `scheduler.py` | 54% |
| `web/app.py` | 49% (lifespan мокается в e2e) |
| `clients/hh.py`, `collector/personal.py`, `collector/vacancies.py` | 11–31% (требуют живого HH-клиента) |

## CI на GitHub Actions

`.github/workflows/test.yml` запускает все 237 тестов на каждый push в `main` и на каждый PR. Покрытие парсится из `coverage.xml` и пишется в job summary. Первый прогон ~1.5–2 мин, последующие с кэшем uv — около минуты.

## Как устроены e2e

`tests/e2e/conftest.py` поднимает FastAPI через `httpx.ASGITransport` с заглушенным lifespan:
- `cbr_client.refresh_salary_module` → no-op (не идём в ЦБ)
- `hh_client.start/close` → no-op (не открываем сетевые сокеты)
- `scheduler_mod.start/shutdown` → no-op (без фоновых тиков)
- `ml_module.reload_model` → no-op

БД — `tmp_path/e2e.db`, изолированная per-тест. Никакого взаимодействия с реальной `data/hh.db`.
