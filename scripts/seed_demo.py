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
import random
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

# --- LLM-демо-данные (чтобы снапшот показывал /analytics и LLM-секции вакансий) ---

LLM_MODELS = ["qwen3:14b", "llama3.1:8b"]

# тип компании по работодателю: (kind, confidence, reasoning)
COMPANY_KIND_BY_EMPLOYER = {
    1001: ("outsource", 0.78, "Проекты на заказ для внешних клиентов."),
    1002: ("product", 0.9, "Собственный продукт, продуктовая разработка."),
    1003: ("outsource", 0.7, "Аутсорс-студия, проектная работа."),
    1004: ("ecommerce", 0.85, "Крупный маркетплейс, e-commerce."),
    1005: ("bank", 0.88, "Финтех/банковская сфера."),
    1006: ("product", 0.82, "SaaS-продукт, облачная платформа."),
    1007: ("gamedev", 0.75, "Разработка игр."),
    1008: ("edu", 0.84, "EdTech-платформа, онлайн-образование."),
}

# Общий пул вопросов/тем — повторяются между вакансиями, чтобы /analytics агрегировал
INTERVIEW_QUESTIONS = [
    ("Расскажите про GIL и его влияние на многопоточность", "Python core"),
    ("Как устроены индексы в PostgreSQL: когда B-tree, когда GIN", "БД"),
    ("В чём разница между WSGI и ASGI", "Web"),
    ("Как работает event loop в asyncio", "asyncio"),
    ("Чем процесс отличается от потока, что такое корутина", "Concurrency"),
    ("Как бы вы масштабировали сервис под высокой нагрузкой", "System design"),
    ("Что такое ACID и уровни изоляции транзакций", "БД"),
    ("Как организовать кеширование и инвалидацию в Redis", "Кеш"),
]
INTERVIEW_TOPICS = ["Python", "PostgreSQL", "asyncio", "Docker", "Kubernetes", "System Design", "Redis"]

PROFILE_SKILLS = {"python", "django", "fastapi", "postgresql", "docker", "kubernetes", "redis", "asyncio"}
YEARS_BY_LEVEL = {"intern": 0, "junior": 1, "middle": 3, "senior": 5, "lead": 8}

# подмножества вакансий с «дорогими» анализаторами
INTERVIEW_VIDS = {90000001, 90000002, 90000004, 90000008, 90000009, 90000011, 90000014, 90000018}
MATCH_VIDS = {90000001, 90000002, 90000004, 90000005, 90000008, 90000009, 90000011, 90000018}
COVER_VIDS = {90000001, 90000002, 90000004, 90000006, 90000014}


def _build_requirements(stack: list[str], level: str) -> list[tuple[str, str, str]]:
    """(kind, category, text) — must=стек+опыт, nice=soft/edu, plus=бонусы."""
    years = YEARS_BY_LEVEL.get(level, 3)
    reqs: list[tuple[str, str, str]] = [("must", "stack", s) for s in stack]
    reqs.append(("must", "exp", f"Опыт коммерческой разработки от {years}+ лет"))
    reqs.append(("nice", "soft", "Опыт код-ревью и менторинга"))
    reqs.append(("nice", "edu", "Английский — чтение технической документации"))
    if level in ("senior", "lead"):
        reqs.append(("plus", "soft", "Опыт лидирования команды"))
    reqs.append(("plus", "stack", "Опыт настройки CI/CD"))
    return reqs


def _build_match_essay(stack: list[str], level: str) -> dict:
    overlap = [s for s in stack if s in PROFILE_SKILLS]
    gaps = [s for s in stack if s not in PROFILE_SKILLS]
    score = min(95, 45 + len(overlap) * 10)
    verdict = "match" if score >= 75 else ("stretch" if score >= 55 else "skip")
    return {
        "score": score,
        "verdict": verdict,
        "matches": [f"Совпадает по стеку: {s}" for s in overlap[:4]] or ["Базовый Python-стек совпадает"],
        "gaps": [f"Нужно подтянуть: {s}" for s in gaps[:3]],
        "reasoning": f"Уровень {level}, пересечение по стеку {len(overlap)} из {len(stack)} пунктов.",
    }


def _build_interview_prep(vid: int) -> dict:
    qs = [INTERVIEW_QUESTIONS[(vid + i) % len(INTERVIEW_QUESTIONS)] for i in range(4)]
    topics = [INTERVIEW_TOPICS[(vid + i) % len(INTERVIEW_TOPICS)] for i in range(3)]
    return {
        "likely_questions": [{"q": q, "why": why} for q, why in qs],
        "topics": list(dict.fromkeys(topics)),
        "code_tasks": ["Реализовать LRU-кеш", "Найти дубликаты в массиве за O(n)"],
        "red_flags": ["Уточнить про переработки", "Спросить про процесс код-ревью"],
    }


def _build_cover_letter(role: str, company: str) -> dict:
    letter = (
        f"Здравствуйте!\n\nУвидел вакансию «{role}» в компании «{company}» и хочу откликнуться. "
        "Последние годы пишу backend на Python (Django/FastAPI), проектирую сервисы на PostgreSQL "
        "и поддерживаю их в Docker/Kubernetes. Близок продуктовый подход и работа с высокой нагрузкой.\n\n"
        "Буду рад обсудить, чем могу быть полезен команде. Спасибо!"
    )
    return {
        "letter": letter,
        "highlights": ["Python + FastAPI/Django", "PostgreSQL и оптимизация запросов", "Docker/Kubernetes"],
        "tone_note": "дружелюбно-деловой",
    }


_SOFT_TONE_BY_EMP = {
    1001: "warm",
    1002: "warm",
    1003: "neutral",
    1004: "demanding",
    1005: "demanding",
    1006: "warm",
    1007: "neutral",
    1008: "warm",
}


def _build_soft_skills(eid: int) -> dict:
    tone = _SOFT_TONE_BY_EMP.get(eid, "neutral")
    wlb, growth = {"warm": (8, 8), "neutral": (6, 6), "demanding": (4, 5), "aggressive": (2, 3)}[tone]
    return {
        "tone": tone,
        "wlb_score": wlb,
        "team_culture": "modern" if tone == "warm" else "traditional",
        "growth_opportunities": growth,
        "red_flags": [] if tone == "warm" else ["упомянуты переработки"],
        "green_flags": ["адекватная команда", "современный стек"],
        "summary": "демо-оценка работодателя по тону описания",
    }


def _build_summary(role: str, level: str, stack: list[str], rem: int, area: str) -> dict:
    where = "удалёнка" if rem else f"офис, {area}"
    return {"summary": f"{role}: уровень {level}, стек {', '.join(stack[:4])}. {where.capitalize()}."}


async def _add_llm_run(conn, task_kind: str, vid: int, model: str, parsed: dict) -> int:
    payload = json.dumps(parsed, ensure_ascii=False)
    cur = await conn.execute(
        """INSERT INTO llm_runs
           (task_kind, target_kind, target_id, model, prompt_version,
            user_prompt, response_raw, parsed_json, ok, latency_ms,
            prompt_tokens, response_tokens, created_at)
           VALUES (?, 'vacancy', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, datetime('now', '-' || ? || ' hours'))""",
        (
            task_kind,
            str(vid),
            model,
            f"{task_kind}_v1",
            f"<demo-промпт {task_kind} для вакансии {vid}>",
            payload,
            payload,
            random.randint(800, 4500),
            random.randint(600, 1800),
            random.randint(200, 900),
            vid % 48,
        ),
    )
    return cur.lastrowid


async def _add_analysis(conn, vid: int, kind: str, data: dict, run_id: int) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO vacancy_analysis (vacancy_id, kind, data_json, llm_run_id) VALUES (?, ?, ?, ?)",
        (vid, kind, json.dumps(data, ensure_ascii=False), run_id),
    )


async def _seed_llm(conn) -> dict[str, int]:
    """Сеет requirements + анализаторы + llm_runs. Возвращает счётчики для отчёта."""
    random.seed(42)
    counts = {"requirements": 0, "analyses": 0, "llm_runs": 0}
    for vid, name, eid, _area, _sf, _st, rem, lvl, stack, _arch, _dis in VACANCIES:
        model = LLM_MODELS[vid % len(LLM_MODELS)]
        company = next(e["name"] for e in EMPLOYERS if e["id"] == eid)

        # requirements (отдельная таблица) + один llm_run на разбор
        reqs = _build_requirements(stack, lvl)
        req_run = await _add_llm_run(
            conn,
            "requirements",
            vid,
            model,
            {"requirements": [{"kind": k, "category": c, "text": t} for k, c, t in reqs]},
        )
        counts["llm_runs"] += 1
        for k, c, t in reqs:
            await conn.execute(
                """INSERT OR IGNORE INTO vacancy_requirements
                   (vacancy_id, kind, category, text, source, llm_run_id)
                   VALUES (?, ?, ?, ?, 'llm', ?)""",
                (vid, k, c, t, req_run),
            )
            counts["requirements"] += 1

        # summary + company_kind — на всех вакансиях
        summ = _build_summary(name, lvl, stack, rem, _area)
        run = await _add_llm_run(conn, "summary", vid, model, summ)
        await _add_analysis(conn, vid, "summary", summ, run)

        ck = COMPANY_KIND_BY_EMPLOYER[eid]
        ckd = {"kind": ck[0], "confidence": ck[1], "reasoning": ck[2]}
        run = await _add_llm_run(conn, "company_kind", vid, model, ckd)
        await _add_analysis(conn, vid, "company_kind", ckd, run)
        counts["analyses"] += 2
        counts["llm_runs"] += 2

        # soft_skills_employer — на всех (чтобы в /funnel был soft-score у каждого работодателя)
        ss = _build_soft_skills(eid)
        run = await _add_llm_run(conn, "soft_skills_employer", vid, model, ss)
        await _add_analysis(conn, vid, "soft_skills_employer", ss, run)
        counts["analyses"] += 1
        counts["llm_runs"] += 1

        if vid in MATCH_VIDS:
            me = _build_match_essay(stack, lvl)
            run = await _add_llm_run(conn, "match_essay", vid, model, me)
            await _add_analysis(conn, vid, "match_essay", me, run)
            counts["analyses"] += 1
            counts["llm_runs"] += 1
        if vid in INTERVIEW_VIDS:
            ip = _build_interview_prep(vid)
            run = await _add_llm_run(conn, "interview_prep", vid, model, ip)
            await _add_analysis(conn, vid, "interview_prep", ip, run)
            counts["analyses"] += 1
            counts["llm_runs"] += 1
        if vid in COVER_VIDS:
            cl = _build_cover_letter(name, company)
            run = await _add_llm_run(conn, "cover_letter", vid, model, cl)
            await _add_analysis(conn, vid, "cover_letter", cl, run)
            counts["analyses"] += 1
            counts["llm_runs"] += 1
    return counts


def _demo_vector(tokens: list[str], dim: int) -> list[float]:
    """Детерминированный псевдо-эмбеддинг из токенов (для демо без Ollama).
    Вакансии с общим стеком получаются близкими по косинусу."""
    import hashlib
    import math

    v = [0.0] * dim
    for t in tokens:
        if not t:
            continue
        h = int(hashlib.sha1(t.lower().encode("utf-8")).hexdigest(), 16)
        v[h % dim] += 1.0
        v[(h // dim) % dim] += 0.5
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


async def _seed_embeddings(conn) -> int:
    """Сеет детерминированные эмбеддинги в vec_vacancies (если RAG доступен)."""
    import hashlib

    from app.config import settings
    from app.db import embeddings_repo
    from app.llm import rag

    if not rag.is_available():
        return 0
    await embeddings_repo.ensure_ready(conn)
    n = 0
    for vid, name, _eid, _area, _sf, _st, _rem, lvl, stack, _arch, _dis in VACANCIES:
        tokens = [*stack, lvl, *name.lower().replace("(", " ").replace(")", " ").split()]
        vec = _demo_vector(tokens, settings.EMBED_DIM)
        text = name + " " + " ".join(stack)
        await embeddings_repo.upsert(conn, vid, "demo-hash", vec, hashlib.sha1(text.encode()).hexdigest())
        n += 1
    return n


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
        company_name = next(e["name"] for e in EMPLOYERS if e["id"] == eid)
        description = (
            f"Компания «{company_name}» ищет разработчика уровня {lvl}. "
            f"Основной стек: {', '.join(stack)}. "
            f"Задачи: проектирование и разработка backend-сервисов, code review, поддержка и развитие "
            f"highload-систем, работа с базами данных и очередями. "
            f"Требования: уверенный {stack[0]}, опыт коммерческой разработки, умение писать тесты и "
            f"работать в команде. Формат: {'удалённо' if rem else 'офис, ' + area}. "
            f"Мы предлагаем интересные задачи, современный стек и адекватную команду."
        )
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
                       ?, '{}', ?,
                       CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                       CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (
                vid,
                name,
                eid,
                company_name,
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
                description,
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

    # LLM-данные (требования, анализаторы, llm_runs) — для /analytics и LLM-секций
    llm_counts = await _seed_llm(conn)

    # RAG-эмбеддинги (детерминированные) — для «похожих вакансий» в снапшоте
    embed_count = await _seed_embeddings(conn)

    await conn.commit()
    await conn.close()

    print(f"✓ demo БД создана: {DEMO_DB}")
    print(
        f"  profile: 1, employers: {len(EMPLOYERS)}, vacancies: {len(VACANCIES)}, "
        f"negotiations: {len(NEGOTIATIONS)}, searches: {len(SEARCHES)}"
    )
    print(
        f"  LLM: requirements: {llm_counts['requirements']}, "
        f"analyses: {llm_counts['analyses']}, llm_runs: {llm_counts['llm_runs']}"
    )
    print(
        f"  RAG: эмбеддингов посеяно: {embed_count}"
        + ("" if embed_count else " (sqlite-vec не доступен — пропущено)")
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
