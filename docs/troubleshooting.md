# Когда что-то ломается

[← Назад к README](../README.md)

---

## Куки протухли (403/пауза)

**Симптом**: панель «Статус» показывает `на паузе (anti-bot)`, в логах `/logs` подряд 403.

**Решение**: получи свежий cookie из браузера (см. README → «Получить cookies из браузера») → обнови `HH_COOKIE` в `.env` → перезапусти uvicorn. Само приложение определит что куки сменились (сравнивает `hhtoken` с тем что в БД) и автоматически примет новые.

Если рестартить не хочешь — нажми «снять паузу» в панели Статус.

## Backfill не доходит до конца

**Симптом**: `requested: 25, saved: 5, paused: true`.

HH ограничивает интенсивность. Дай клиенту остыть 10-30 мин, потом нажми «Подтянуть вакансии» ещё раз — заберёт остаток. `backfill_pending` джоб делает это сам каждые 20 мин.

## ML «недостаточно данных»

`POST /api/ml/train` возвращает `{trained: false, positives: 3}` — нужно ≥5 positive (собес+приглашение) и ≥5 negative (отказ) (пороги MIN_POSITIVES/MIN_NEGATIVES снижены с 10 до 5). Накопится со временем — модель обучится автоматически в 04:00 через cron, в логах будет train AUC + cross-val AUC±std (StratifiedKFold) и веса фич.

## LLM-блоки пустые / `/llm-logs` показывает network error

**Симптом**: на `/vacancy/{id}` нет LLM-блоков, кнопка «Прогнать» молча отваливается, на `/llm-logs` у свежих run-ов `ok=0` и `response` начинается с `httpx.ConnectError` / `Connection refused 127.0.0.1:11434`.

**Решение**: не поднят Ollama. Без неё приложение работает — но cron `llm_parse_requirements` будет тихо падать раз в час, а UI-кнопки анализа возвращать ошибку. Подними:

```bash
brew install ollama
ollama serve &
ollama pull qwen3:14b
```

Активная модель — на `/profile` → «LLM-модель» (или `POST /api/settings/llm-model`). Для thinking-моделей (`qwen3*`, `deepseek-r1*`) клиент сам выставляет `think=False`, иначе в `format=json` ответ часто пустой `{}`.

## `refresh_resume_search_token` не нашёл хеш

**Симптом**: в логах `refresh_resume_search_token: не нашёл хеш в /applicant/resumes`, сохранённый поиск-«рекомендация» перестал тянуть новые вакансии.

Скорее всего HH сменил вёрстку `/applicant/resumes` и регулярка `/search/vacancy\?[^"']*?resume=([a-f0-9]{20,})` (см. `app/collector/personal.py:173`) не матчится. Открой страницу руками в браузере → DevTools → View source → найди реальный URL рекомендации и подгони регулярку. Старые поиски-рекомендации можно временно удалить и пересоздать через `POST /api/searches/recommendations` после фикса.

## Тесты падают из-за `data/model.pkl`

**Симптом**: `test_scoring_predict.*` flaky или валится локально, на CI всё ок.

В `data/model.pkl` лежит реальная обученная модель, которая раньше успевала загрузиться через глобальный кэш `_MODEL` и подменяла ожидания эвристических тестов. Сейчас это покрыто `tests/conftest.py::pytest_sessionstart` (переписывает `MODEL_PATH` на несуществующий путь до любого импорта `app.*`) — но если ты дописал свой тест, который сам перетирает `MODEL_PATH` и забыл откатить, autouse-фикстура `_isolate_ml_model_path` сбросит кэш перед следующим тестом. Если всё ещё ломается — просто удали `data/model.pkl`, тесты её не используют.

## Python 3.14 несовместимость

Если `uv sync` подтянул Python 3.14 — jinja2 кеш падает с `unhashable type: 'dict'`. Зависимость пинит `>=3.12,<3.13`. Если `.venv` уже на 3.14 — пересоздай:

```bash
rm -rf .venv uv.lock
uv venv --python 3.12
uv sync
```
