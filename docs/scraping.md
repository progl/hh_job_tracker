# Как происходит скрейпинг

[← Назад к README](../README.md)

---

**Один источник правды — `HH-Lux-InitialState`.** HH в `<template id="HH-Lux-InitialState">` кладёт полный JSON-state страницы: список вакансий, отклики, индекс вежливости работодателя, информацию о резюме. Парсер (`app/parsers/state.py`) — это 15 строк: регулярка + `html.unescape` + `json.loads`. Никакого BeautifulSoup, никакого хрупкого DOM-парсинга.

## Что собирается

| Endpoint | Что вытаскиваем | Куда |
|---|---|---|
| `/search/vacancy?text=...&page=N` | `vacancySearchResult.vacancies[]` | `vacancies` (+ `vacancy_collected_via`) |
| `/vacancy/{id}` | `vacancyView` — полное описание, `keySkills` | `vacancies` (дополняет description, key_skills) |
| `/applicant/negotiations?page=N` | `applicantNegotiations.topicList[]` + `applicantEmployerPoliteness` + `account` | `negotiations`, `employers`, `profile` |
| `/applicant/resumes` → `/resume/{id}` | список резюме → нужное резюме | `profile` (skills, formats, expected ЗП) |
| `/applicant/favorites/vacancy` | избранное | `vacancies` (+ tag `favorite`) |

## Пайплайн обработки

`app/collector/vacancies.py:vacancy_from_search_item`:

```
HTML страницы
  └─► extract_initial_state → dict (JSON state)
        └─► для каждой вакансии:
              ├─► parsers/salary.normalize_compensation → ЗП в рублях по курсу ЦБ
              ├─► parsers/stack.extract_stack          → 50+ технологий (regex по описанию)
              ├─► parsers/level.detect_level           → junior/middle/senior/lead/intern
              ├─► parsers/remote.is_remote_by_text     → удалёнка из текста (если HH не отметил)
              └─► vacancies_repo.upsert (ON CONFLICT DO UPDATE)
```

## Дедуп и инкрементальный sync

- `vacancies` — upsert по `id`, дубликатов не бывает
- `vacancy_collected_via` — связывает вакансию с запросом, по которому её нашли (для аналитики «откуда пришла»)
- удалённые с HH — помечаются `disappeared_at`, в выдаче скрыты
- архивные на HH (страница возвращает 200, но плашка «В архиве с …») — помечаются `archived_at`, в выдаче скрыты под отдельным фильтром

### Early-stop инкремент поисков

`collect_search` (`app/collector/vacancies.py`) принимает `early_stop_consecutive_seen=K` и выходит, как только K подряд карточек на странице уже знакомы — «seen этим search_id за последние 24 ч ∪ всё, что глобально помечено как `skipped`». В типовом случае это 1–2 страницы вместо 10–200. По умолчанию K=3 для пользовательских поисков и K=5 для рекомендаций, настраивается per-search и через bulk-эндпоинты `POST /api/searches/{bulk-max-pages, bulk-early-stop}`. Принудительный полный прогон — Shift+клик на ▶ или `?full=true` (early-stop отключается).

### Smart-stop инкремент откликов

`negotiations` идут в `lastModified DESC`. `collect_negotiations` помнит `neg_last_sync` в `profile`, и на первом item с `lastModified <= last_sync` делает `break` прямо внутри страницы (см. `personal.py` ~ строка 127). На практике инкремент стал в 2 раза быстрее: вместо «дочитать всю страницу и идти на следующую, если ничего нового» — выходим сразу. Полный прогон (`personal_full_refresh`, 02:00) игнорирует `last_sync`.

## Backfill из откликов

На странице откликов есть только `vacancy_id`, а карточка вакансии — отдельный запрос. `backfill_from_negotiations()` тянет недостающие карточки пачкой по 25, чтобы не выжечь rate-limit за раз. Висит на cron каждые 20 минут.

## Рекомендации hh.ru и плавающий `?resume=<hash>`

Лента «Рекомендованные вам» открывается как обычный поиск по URL `/search/vacancy?resume=<hash>`. HH периодически меняет этот хеш, и сохранённый URL внезапно начинает отдавать пустоту. Поэтому перед каждым прогоном `sync_searches` зовёт `refresh_resume_search_token` (`app/collector/personal.py`): открывает `/applicant/resumes`, регуляркой `/search/vacancy\?[^"']*?resume=([a-f0-9]{20,})` достаёт актуальный хеш и обновляет URL во всех saved-search с типом `recommendations`. Создание рекомендации как сохранённого поиска — `POST /api/searches/recommendations`.

## Парсер `workFormats`

HH меняет схему формата работы в `vacancySearchResult`. `vacancy_from_search_item` поддерживает все три варианта:

- старый плоский: `["REMOTE", "HYBRID"]`
- старый dict-вариант: `[{"id": "REMOTE"}, {"id": "HYBRID"}]`
- новый: `[{"workFormatsElement": ["REMOTE", "HYBRID"]}]`

Все три сводятся к плоскому списку строк, который кладётся в `vacancies.work_formats` как JSON. Если HH ещё раз поменяет — это первое место, куда смотреть (см. `app/collector/vacancies.py:26`).
