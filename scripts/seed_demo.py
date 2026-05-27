"""Создаёт демо-БД `data/hh_demo.db` с вымышленными данными для скриншотов.

Жёстко прибит к пути `data/hh_demo.db`. Никогда не пишет в реальную БД.

Запуск:
    python -m scripts.seed_demo            # создать, если нет
    python -m scripts.seed_demo --force    # пересоздать с нуля

Запуск сервера на demo-БД:
    DB_PATH=data/hh_demo.db make run
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Жёстко зашитый путь — НЕ из env, НЕ из settings.
DEMO_DB = Path(__file__).resolve().parent.parent / "data" / "hh_demo.db"


def _assert_not_real_db() -> None:
    """Защита: не даём случайно перетереть реальную БД."""
    from app.config import settings

    real = Path(settings.DB_PATH).resolve()
    demo = DEMO_DB.resolve()
    if real == demo:
        raise RuntimeError(f"DEMO_DB совпадает с реальной БД ({real}). Прекращаю чтобы не перетереть данные.")
    if (
        demo.name in ("hh.db",)
        or demo.parent.name == ""
        or demo.parent != Path(__file__).resolve().parent.parent / "data"
    ):
        raise RuntimeError(f"DEMO_DB указывает не туда, куда ожидалось: {demo}")


PROFILE = {
    "resume_id": "demo_resume_1",
    "hhid": "00000001",
    "full_name": "Иван Петров",
    "title": "Senior Python Developer",
    "years_experience": 7.0,
    "salary_expected_from": 200000,
    "salary_currency": "RUR",
    "skills": json.dumps(
        ["python", "django", "fastapi", "postgresql", "docker", "kubernetes", "redis", "asyncio"],
        ensure_ascii=False,
    ),
    "formats": json.dumps(["REMOTE", "HYBRID"], ensure_ascii=False),
    "raw_resume": "{}",
}

EMPLOYERS = [
    {
        "id": 1001,
        "name": "ООО Ромашка",
        "is_accredited_it": 1,
        "all_topic_count": 120,
        "read_topic_percent": 85,
        "reply_working_days": 1.5,
    },
    {
        "id": 1002,
        "name": "Технополис",
        "is_accredited_it": 1,
        "all_topic_count": 450,
        "read_topic_percent": 92,
        "reply_working_days": 0.8,
    },
    {
        "id": 1003,
        "name": "АйТи Лаб",
        "is_accredited_it": 0,
        "all_topic_count": 30,
        "read_topic_percent": 60,
        "reply_working_days": 3.0,
    },
    {
        "id": 1004,
        "name": "Маркетплейс ШОП",
        "is_accredited_it": 1,
        "all_topic_count": 800,
        "read_topic_percent": 75,
        "reply_working_days": 2.0,
    },
    {
        "id": 1005,
        "name": "Финтех Плюс",
        "is_accredited_it": 1,
        "all_topic_count": 200,
        "read_topic_percent": 50,
        "reply_working_days": 5.0,
    },
    {
        "id": 1006,
        "name": "Облачные Решения",
        "is_accredited_it": 1,
        "all_topic_count": 60,
        "read_topic_percent": 95,
        "reply_working_days": 0.5,
    },
    {
        "id": 1007,
        "name": "Геймдев Студио",
        "is_accredited_it": 0,
        "all_topic_count": 15,
        "read_topic_percent": 40,
        "reply_working_days": 7.0,
    },
    {
        "id": 1008,
        "name": "ЕдТех Платформа",
        "is_accredited_it": 1,
        "all_topic_count": 95,
        "read_topic_percent": 70,
        "reply_working_days": 2.5,
    },
]

# (id, name, employer_id, area, salary_from, salary_to, is_remote, level, stack, archived, disappeared)
VACANCIES = [
    (
        90000001,
        "Senior Python Developer (Django/DRF)",
        1001,
        "Москва",
        280000,
        350000,
        1,
        "senior",
        ["python", "django", "drf", "postgresql", "redis"],
        False,
        False,
    ),
    (
        90000002,
        "Python Backend Engineer (FastAPI)",
        1002,
        "Удалённо",
        300000,
        400000,
        1,
        "senior",
        ["python", "fastapi", "asyncio", "postgresql", "docker"],
        False,
        False,
    ),
    (
        90000003,
        "Middle Python Developer",
        1003,
        "Санкт-Петербург",
        200000,
        260000,
        0,
        "middle",
        ["python", "django", "celery", "rabbitmq"],
        False,
        False,
    ),
    (
        90000004,
        "Lead Python / Tech Lead",
        1004,
        "Москва",
        400000,
        500000,
        1,
        "lead",
        ["python", "django", "kubernetes", "microservices"],
        False,
        False,
    ),
    (
        90000005,
        "Senior Python Developer (B2C / Telegram / AI)",
        1005,
        "Удалённо",
        300000,
        450000,
        1,
        "senior",
        ["python", "fastapi", "ml"],
        True,
        False,
    ),
    (
        90000006,
        "Backend Developer (Python + ClickHouse)",
        1006,
        "Удалённо",
        250000,
        330000,
        1,
        "senior",
        ["python", "clickhouse", "kafka", "docker"],
        False,
        False,
    ),
    (
        90000007,
        "Python Developer (Junior+)",
        1007,
        "Москва",
        100000,
        150000,
        0,
        "junior",
        ["python", "flask", "postgresql"],
        False,
        False,
    ),
    (
        90000008,
        "Senior Backend (Python/Go)",
        1008,
        "Удалённо",
        320000,
        420000,
        1,
        "senior",
        ["python", "postgresql", "kafka", "microservices"],
        False,
        False,
    ),
    (
        90000009,
        "Python разработчик (highload)",
        1002,
        "Москва",
        280000,
        380000,
        1,
        "senior",
        ["python", "asyncio", "redis", "postgresql", "kubernetes"],
        False,
        False,
    ),
    (
        90000010,
        "Middle/Senior Python",
        1004,
        "Удалённо",
        220000,
        310000,
        1,
        "middle",
        ["python", "django", "tests"],
        False,
        False,
    ),
    (
        90000011,
        "Python Team Lead",
        1001,
        "Москва",
        450000,
        550000,
        0,
        "lead",
        ["python", "django", "postgresql", "aws", "kubernetes"],
        False,
        False,
    ),
    (
        90000012,
        "Senior Python Engineer (ML/Data)",
        1003,
        "Удалённо",
        350000,
        450000,
        1,
        "senior",
        ["python", "ml", "postgresql"],
        False,
        False,
    ),
    (
        90000013,
        "Backend Python (RabbitMQ/Celery)",
        1005,
        "Санкт-Петербург",
        230000,
        290000,
        0,
        "middle",
        ["python", "django", "celery", "rabbitmq", "redis"],
        True,
        False,
    ),
    (
        90000014,
        "Python Developer (Microservices)",
        1006,
        "Удалённо",
        270000,
        360000,
        1,
        "senior",
        ["python", "fastapi", "microservices", "docker", "kubernetes"],
        False,
        False,
    ),
    (
        90000015,
        "Python (FastAPI + Postgres)",
        1008,
        "Удалённо",
        240000,
        320000,
        1,
        "middle",
        ["python", "fastapi", "postgresql", "redis"],
        False,
        True,
    ),
    (
        90000016,
        "Senior Python Developer",
        1007,
        "Удалённо",
        290000,
        380000,
        1,
        "senior",
        ["python", "django", "postgresql"],
        False,
        False,
    ),
    (
        90000017,
        "Backend Engineer (Python, Linux, Nginx)",
        1002,
        "Москва",
        250000,
        330000,
        0,
        "middle",
        ["python", "linux", "nginx", "docker"],
        False,
        False,
    ),
    (
        90000018,
        "Python Senior Developer (search/elasticsearch)",
        1004,
        "Удалённо",
        310000,
        410000,
        1,
        "senior",
        ["python", "elasticsearch", "postgresql", "redis"],
        False,
        False,
    ),
    (
        90000019,
        "Python Developer (стажёр)",
        1007,
        "Санкт-Петербург",
        60000,
        90000,
        0,
        "intern",
        ["python", "flask"],
        False,
        False,
    ),
    (
        90000020,
        "Lead Backend Python (платформа)",
        1001,
        "Удалённо",
        480000,
        600000,
        1,
        "lead",
        ["python", "kubernetes", "aws", "microservices", "kafka"],
        False,
        True,
    ),
]

# (negotiation_id, vacancy_id, employer_id, last_state, last_employer_state, archived, viewed_by_opponent, has_response_letter)
NEGOTIATIONS = [
    (5000001, 90000001, 1001, "RESPONSE", "RESPONSE", 0, 1, 1),
    (5000002, 90000002, 1002, "INTERVIEW", "INVITATION", 0, 1, 1),
    (5000003, 90000003, 1003, "DISCARD", "DISCARD", 0, 1, 0),
    (5000004, 90000004, 1004, "INVITATION", "INVITATION", 0, 1, 1),
    (5000005, 90000005, 1005, "RESPONSE", "RESPONSE", 1, 0, 0),
    (5000006, 90000006, 1006, "RESPONSE", "RESPONSE", 0, 1, 1),
    (5000007, 90000008, 1008, "INTERVIEW", "INVITATION", 0, 1, 1),
    (5000008, 90000009, 1002, "DISCARD", "DISCARD", 0, 1, 0),
    (5000009, 90000010, 1004, "RESPONSE", "RESPONSE", 0, 0, 0),
    (5000010, 90000011, 1001, "INVITATION", "INVITATION", 0, 1, 1),
    (5000011, 90000012, 1003, "RESPONSE", "RESPONSE", 0, 1, 0),
    (5000012, 90000014, 1006, "RESPONSE", "RESPONSE", 0, 0, 1),
    (5000013, 90000015, 1008, "DISCARD", "DISCARD", 1, 1, 0),
    (5000014, 90000017, 1002, "RESPONSE", "RESPONSE", 0, 1, 1),
    (5000015, 90000018, 1004, "INTERVIEW", "INVITATION", 0, 1, 1),
]

SEARCHES = [
    ("Python Senior Remote", {"text": "python senior", "area": "113", "schedule": "remote"}, 1),
    ("Lead Python", {"text": "lead python", "area": "113"}, 1),
    ("FastAPI Backend", {"text": "fastapi backend"}, 1),
    ("Python ML", {"text": "python ml"}, 0),
]

VACANCY_STATUSES = {
    90000002: ("applied", "Прошёл скрининг, ждём финал"),
    90000004: ("interested", "посмотреть подробнее"),
    90000007: ("skipped", None),
    90000019: ("skipped", None),
    90000011: ("interested", None),
}


async def seed(force: bool) -> None:
    _assert_not_real_db()
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
    if DEMO_DB.exists():
        if not force:
            print(f"⚠ {DEMO_DB} уже существует. Используй --force чтобы пересоздать.")
            sys.exit(1)
        for ext in ("", "-wal", "-shm"):
            f = DEMO_DB.with_suffix(DEMO_DB.suffix + ext) if ext else DEMO_DB
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    # Подменим путь в модуле app.db.db чтобы init_db создал миграции в нужном месте
    from app import config as cfg_mod
    from app.db import db as dbm

    cfg_mod.settings.DB_PATH = str(DEMO_DB)
    dbm.DB_PATH = DEMO_DB

    await dbm.init_db()

    import aiosqlite

    conn = await aiosqlite.connect(DEMO_DB)
    await conn.execute("PRAGMA foreign_keys = OFF")

    # profile
    await conn.execute(
        """INSERT OR REPLACE INTO profile (id, resume_id, hhid, full_name, title, years_experience,
           salary_expected_from, salary_currency, skills, formats, raw_resume, updated_at)
           VALUES (1, :resume_id, :hhid, :full_name, :title, :years_experience,
           :salary_expected_from, :salary_currency, :skills, :formats, :raw_resume, CURRENT_TIMESTAMP)""",
        PROFILE,
    )

    # employers
    for e in EMPLOYERS:
        await conn.execute(
            """INSERT OR REPLACE INTO employers
               (id, name, is_accredited_it, all_topic_count, read_topic_percent, reply_working_days, raw_json, updated_at)
               VALUES (:id, :name, :is_accredited_it, :all_topic_count, :read_topic_percent, :reply_working_days, '{}', CURRENT_TIMESTAMP)""",
            e,
        )

    # vacancies
    for vid, name, eid, area, sf, st, rem, lvl, stack, archived, disappeared in VACANCIES:
        await conn.execute(
            """INSERT OR REPLACE INTO vacancies
               (id, name, company_id, company_name, area_id, area_name,
                salary_from, salary_to, salary_currency, salary_rub,
                work_schedule, employment, work_formats,
                is_remote, is_remote_text, level, parsed_stack,
                responses_count, total_responses_count, online_users_count,
                description, raw_json, url, archived_at, disappeared_at,
                seen_at, updated_at)
               VALUES (?, ?, ?, ?, NULL, ?,
                       ?, ?, 'RUR', ?,
                       ?, 'FULL', '[]',
                       ?, ?, ?, ?,
                       ?, ?, ?,
                       'Описание вакансии. Стек: ' || ?, '{}', ?,
                       CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                       CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (
                vid,
                name,
                eid,
                next(e["name"] for e in EMPLOYERS if e["id"] == eid),
                area,
                sf,
                st,
                (sf + st) // 2,
                "remote" if rem else "fullDay",
                rem,
                rem,
                lvl,
                json.dumps(stack, ensure_ascii=False),
                10 + vid % 30,
                50 + vid % 200,
                vid % 8,
                ", ".join(stack),
                f"https://hh.ru/vacancy/{vid}",
                1 if archived else 0,
                1 if disappeared else 0,
            ),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO vacancy_status (vacancy_id, status) VALUES (?, 'new')",
            (vid,),
        )

    # vacancy_status overrides
    for vid, (st, note) in VACANCY_STATUSES.items():
        await conn.execute(
            "UPDATE vacancy_status SET status = ?, note = ? WHERE vacancy_id = ?",
            (st, note, vid),
        )

    # negotiations
    for nid, vid, eid, ls, les, arch, viewed, hrl in NEGOTIATIONS:
        await conn.execute(
            """INSERT OR REPLACE INTO negotiations
               (id, vacancy_id, employer_id, last_state, last_employer_state,
                archived, viewed_by_opponent, has_response_letter,
                conversation_messages, creation_time, last_modified, raw_json, seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                       datetime('now', '-' || ? || ' days'),
                       datetime('now', '-' || ? || ' days'),
                       '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (nid, vid, eid, ls, les, arch, viewed, hrl, hrl * 2, nid % 30 + 1, nid % 14),
        )

    # searches
    for name, params, active in SEARCHES:
        await conn.execute(
            """INSERT INTO searches (name, params, is_active, last_run_at)
               VALUES (?, ?, ?, datetime('now', '-1 hours'))""",
            (name, json.dumps(params, ensure_ascii=False), active),
        )

    await conn.commit()
    await conn.close()

    print(f"✓ demo БД создана: {DEMO_DB}")
    print(
        f"  profile: 1, employers: {len(EMPLOYERS)}, vacancies: {len(VACANCIES)}, "
        f"negotiations: {len(NEGOTIATIONS)}, searches: {len(SEARCHES)}"
    )
    print(
        f"\n  Запуск сервера: DB_PATH={DEMO_DB.relative_to(Path.cwd()) if DEMO_DB.is_relative_to(Path.cwd()) else DEMO_DB} make run"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Создаёт демо-БД для скриншотов.")
    ap.add_argument("--force", action="store_true", help="пересоздать БД если уже существует")
    args = ap.parse_args()
    asyncio.run(seed(args.force))


if __name__ == "__main__":
    main()
