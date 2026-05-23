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
- `negotiations` — smart-stop: HH сортирует по `lastModified DESC`, на странице без новых элементов sync останавливается (см. `personal.py:142`)
- удалённые с HH — помечаются `disappeared_at`, в выдаче скрыты
- архивные на HH (страница возвращает 200, но плашка «В архиве с …») — помечаются `archived_at`, в выдаче скрыты под отдельным фильтром

## Backfill из откликов

На странице откликов есть только `vacancy_id`, а карточка вакансии — отдельный запрос. `backfill_from_negotiations()` тянет недостающие карточки пачкой по 25, чтобы не выжечь rate-limit за раз. Висит на cron каждые 20 минут.
