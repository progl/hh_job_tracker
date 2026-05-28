"""Тесты scheduler-джобов: _job_personal_refresh, _job_fx_refresh, _job_dedup_vacancies,
_job_sync_searches (включая авто-дедуп после save_jar), _job_backfill_pending, _job_ml_retrain.

Используем tmp_db, мокаем hh_client и внешние коллекторы по точкам входа.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def sched_mod(monkeypatch):
    """Возвращает СВЕЖИЙ app.scheduler с выровненными ссылками на актуальные модули БД.

    e2e conftest перезагружает app.* — без этого получаем разные объекты модуля.
    """
    import importlib

    import app.scheduler as sm

    # перезагрузим, чтобы получить актуальные референсы на (возможно) перезагруженные app.db.*
    sm = importlib.reload(sm)
    import app.collector.personal as pc
    import app.db.db as dbm
    import app.db.job_runs_repo as jr
    import app.db.vacancies_repo as vr

    monkeypatch.setattr(jr, "get_db", dbm.get_db)
    monkeypatch.setattr(sm, "job_runs_repo", jr)
    monkeypatch.setattr(sm, "vacancies_repo", vr)
    monkeypatch.setattr(sm, "personal_collector", pc)
    sm._state["jobs"] = {}
    yield sm
    sm._state["jobs"] = {}


def _mk_client(paused: bool = False):
    cli = MagicMock()
    cli.status = {"paused_now": paused, "paused_until": 0}
    cli.client = MagicMock()
    cli.client.cookies = MagicMock()
    cli.client.cookies.jar = []
    return cli


# ----- _job_personal_refresh ----


@pytest.mark.asyncio
async def test_job_personal_refresh_runs_and_saves_jar(tmp_db, sched_mod, monkeypatch):
    cli = _mk_client()

    # подменяем коллектор
    async def fake_collect(client, db, max_pages, full=False):
        return {"saved_negotiations": 3, "pages": 1, "mode": "incremental"}

    monkeypatch.setattr(sched_mod.personal_collector, "collect_negotiations", fake_collect)

    save_jar_mock = AsyncMock()
    monkeypatch.setattr(sched_mod, "save_jar", save_jar_mock)

    res = await sched_mod._job_personal_refresh(cli)
    assert res["saved_negotiations"] == 3
    save_jar_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_job_personal_full_refresh(tmp_db, sched_mod, monkeypatch):
    cli = _mk_client()
    captured = {}

    async def fake_collect(client, db, max_pages, full=False):
        captured["max_pages"] = max_pages
        captured["full"] = full
        return {"saved_negotiations": 10}

    monkeypatch.setattr(sched_mod.personal_collector, "collect_negotiations", fake_collect)
    monkeypatch.setattr(sched_mod, "save_jar", AsyncMock())

    res = await sched_mod._job_personal_full_refresh(cli)
    assert res["saved_negotiations"] == 10
    assert captured["full"] is True
    assert captured["max_pages"] == 20


# ----- _job_fx_refresh ----


@pytest.mark.asyncio
async def test_job_fx_refresh(tmp_db, sched_mod, monkeypatch):
    async def fake_refresh(db):
        return {"ok": True, "rates": {"USD": 90}}

    monkeypatch.setattr(sched_mod, "refresh_salary_module", fake_refresh)

    res = await sched_mod._job_fx_refresh()
    assert res["ok"] is True


# ----- _job_dedup_vacancies ----


@pytest.mark.asyncio
async def test_job_dedup_vacancies_calls_repo(tmp_db, sched_mod):
    # положим пару дубликатов и проверим, что job сработает
    from app.db import vacancies_repo
    from tests.integration.test_dedup import _v

    await vacancies_repo.upsert(tmp_db, _v(1, "Python", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(2, "Python", "Acme"))
    await tmp_db.commit()

    res = await sched_mod._job_dedup_vacancies()
    assert res == {"groups": 1, "marked": 1}


@pytest.mark.asyncio
async def test_job_dedup_vacancies_empty(tmp_db, sched_mod):
    res = await sched_mod._job_dedup_vacancies()
    assert res == {"groups": 0, "marked": 0}


# ----- _job_sync_searches ----


@pytest.mark.asyncio
async def test_job_sync_searches_no_active(tmp_db, sched_mod, monkeypatch):
    cli = _mk_client()
    res = await sched_mod._job_sync_searches(cli)
    assert res["ran"] == 0
    assert "нет активных" in res["reason"]


@pytest.mark.asyncio
async def test_job_sync_searches_paused_client(tmp_db, sched_mod, monkeypatch):
    from app.db import searches_repo

    await searches_repo.create_search(tmp_db, "s1", {"text": "x"})
    cli = _mk_client(paused=True)
    res = await sched_mod._job_sync_searches(cli)
    assert res["ran"] == 0
    assert res["reason"] == "клиент на паузе"


@pytest.mark.asyncio
async def test_job_sync_searches_runs_collect_and_dedup(tmp_db, sched_mod, monkeypatch):
    from app.db import searches_repo, vacancies_repo
    from tests.integration.test_dedup import _v

    await searches_repo.create_search(tmp_db, "s1", {"text": "py", "max_pages": 2})
    # дубликат для проверки авто-дедупа
    await vacancies_repo.upsert(tmp_db, _v(1, "Python", "Acme"))
    await vacancies_repo.upsert(tmp_db, _v(2, "Python", "Acme"))
    await tmp_db.commit()

    cli = _mk_client()
    captured = {}

    async def fake_collect_search(client, db, params, max_pages, search_id, **kwargs):
        captured["params"] = params
        captured["max_pages"] = max_pages
        captured["search_id"] = search_id
        return {"saved": 5, "pages": 1, "total_results": 5, "disappeared": 0, "search_id": search_id}

    # коллектор подгружается локально
    from app.collector import vacancies as col_mod

    monkeypatch.setattr(col_mod, "collect_search", fake_collect_search)
    monkeypatch.setattr(sched_mod, "save_jar", AsyncMock())

    res = await sched_mod._job_sync_searches(cli)
    assert res["ran"] == 1
    assert captured["max_pages"] == 2
    assert captured["params"] == {"text": "py"}  # max_pages должен быть pop'нут
    # авто-дедуп после save_jar — проверим в БД
    cur = await tmp_db.execute("SELECT status FROM vacancy_status WHERE vacancy_id=2")
    assert (await cur.fetchone())[0] == "skipped"


@pytest.mark.asyncio
async def test_job_sync_searches_break_on_error(tmp_db, sched_mod, monkeypatch):
    from app.db import searches_repo

    await searches_repo.create_search(tmp_db, "s1", {"text": "py"})
    await searches_repo.create_search(tmp_db, "s2", {"text": "go"})

    cli = _mk_client()

    async def boom(client, db, params, max_pages, search_id, **kwargs):
        raise RuntimeError("hh down")

    from app.collector import vacancies as col_mod

    monkeypatch.setattr(col_mod, "collect_search", boom)
    monkeypatch.setattr(sched_mod, "save_jar", AsyncMock())

    res = await sched_mod._job_sync_searches(cli)
    assert res["ran"] == 1
    assert res["results"][0].get("error") == "hh down"


@pytest.mark.asyncio
async def test_job_sync_searches_dedup_exception_swallowed(tmp_db, sched_mod, monkeypatch):
    from app.db import searches_repo

    await searches_repo.create_search(tmp_db, "s1", {"text": "py"})
    cli = _mk_client()

    async def fake_collect(*a, **kw):
        return {"saved": 0, "pages": 0, "total_results": 0, "disappeared": 0, "search_id": 1}

    from app.collector import vacancies as col_mod

    monkeypatch.setattr(col_mod, "collect_search", fake_collect)
    monkeypatch.setattr(sched_mod, "save_jar", AsyncMock())

    async def bad_dedup(db):
        raise ValueError("dedup boom")

    monkeypatch.setattr(sched_mod.vacancies_repo, "mark_duplicates_as_skipped", bad_dedup)

    # не должно бросить — внутри есть try/except
    res = await sched_mod._job_sync_searches(cli)
    assert res["ran"] == 1


# ----- _job_backfill_pending ----


@pytest.mark.asyncio
async def test_job_backfill_pending_zero_remaining(tmp_db, sched_mod, monkeypatch):
    cli = _mk_client()
    res = await sched_mod._job_backfill_pending(cli)
    assert res == {"remaining": 0, "skipped": True}


@pytest.mark.asyncio
async def test_job_backfill_pending_paused(tmp_db, sched_mod, monkeypatch):
    # положим negotiation без соответствующей вакансии
    from app.db import negotiations_repo

    item = {"id": 1001, "vacancyId": 9999, "lastState": "RESPONSE", "lastModified": "2024-01-01"}
    await negotiations_repo.upsert_and_snapshot(tmp_db, negotiations_repo.from_topic_item(item))
    await tmp_db.commit()

    cli = _mk_client(paused=True)
    res = await sched_mod._job_backfill_pending(cli)
    assert res["remaining"] >= 1
    assert res["skipped"] == "paused"


@pytest.mark.asyncio
async def test_job_backfill_pending_runs(tmp_db, sched_mod, monkeypatch):
    from app.db import negotiations_repo

    item = {"id": 2002, "vacancyId": 8888, "lastState": "RESPONSE", "lastModified": "2024-01-01"}
    await negotiations_repo.upsert_and_snapshot(tmp_db, negotiations_repo.from_topic_item(item))
    await tmp_db.commit()

    cli = _mk_client()
    fake_backfill = AsyncMock(
        return_value={
            "requested": 1,
            "saved": 1,
            "failed": 0,
            "paused": False,
            "remaining": 0,
            "hint": None,
            "pause_reason": None,
        }
    )
    from app.collector import vacancies as col_mod

    monkeypatch.setattr(col_mod, "backfill_from_negotiations", fake_backfill)
    monkeypatch.setattr(sched_mod, "save_jar", AsyncMock())

    res = await sched_mod._job_backfill_pending(cli)
    assert res["saved"] == 1
    fake_backfill.assert_awaited_once()


# ----- _job_ml_retrain ----


@pytest.mark.asyncio
async def test_job_ml_retrain_ok(tmp_db, sched_mod, monkeypatch):
    async def fake_train():
        return {"trained": True, "samples": 100}

    # модуль ml импортируется внутри функции
    import app.scoring.ml as ml

    monkeypatch.setattr(ml, "train_if_enough_data", fake_train)

    res = await sched_mod._job_ml_retrain()
    assert res["trained"] is True
    assert sched_mod._state["jobs"]["ml_retrain"]["ok"] is True


@pytest.mark.asyncio
async def test_job_ml_retrain_swallows_exception(tmp_db, sched_mod, monkeypatch):
    async def boom():
        raise RuntimeError("nope")

    import app.scoring.ml as ml

    monkeypatch.setattr(ml, "train_if_enough_data", boom)

    # не должно бросить — внутри try/except. _record потом перезатрёт _state на ok=True
    # из-за того, что функция формально вернула None успешно. Это особенность кода.
    res = await sched_mod._job_ml_retrain()
    assert res is None


# ----- _job_generate_cover_letters ----


async def _seed_pipeline_vacancy(conn, vid: int, *, status: str | None = None, negotiation: bool = False):
    await conn.execute(
        "INSERT INTO vacancies (id, name, description) VALUES (?, ?, ?)", (vid, "Py", "d" * 200)
    )
    if status is not None:
        await conn.execute("INSERT INTO vacancy_status (vacancy_id, status) VALUES (?, ?)", (vid, status))
    if negotiation:
        await conn.execute("INSERT INTO negotiations (id, vacancy_id) VALUES (?, ?)", (vid * 10, vid))


@pytest.mark.asyncio
async def test_job_generate_cover_letters_runs_for_pipeline_only(tmp_db, sched_mod, monkeypatch):
    await tmp_db.execute("INSERT INTO profile (id, raw_resume) VALUES (1, '{\"x\":1}')")
    await _seed_pipeline_vacancy(tmp_db, 1, status="interested")  # попадёт
    await _seed_pipeline_vacancy(tmp_db, 2, negotiation=True)  # попадёт
    await _seed_pipeline_vacancy(tmp_db, 3, status="new")  # НЕ попадёт (не в пайплайне)
    await _seed_pipeline_vacancy(tmp_db, 4)  # НЕ попадёт (нет статуса/отклика)
    await tmp_db.commit()

    from app.llm import registry

    seen: list[int] = []

    async def fake_analyze_one(db, vid, kinds, model=None):
        seen.append(vid)
        assert kinds == ["cover_letter"]
        return [
            registry.AnalysisResult(
                ok=True, kind="cover_letter", data={"letter": "hi"}, llm_run_id=1, model="m", latency_ms=5
            )
        ]

    monkeypatch.setattr(registry, "analyze_one", fake_analyze_one)

    res = await sched_mod._job_generate_cover_letters()
    assert res == {"processed": 2, "ok": 2}
    assert sorted(seen) == [1, 2]


@pytest.mark.asyncio
async def test_job_generate_cover_letters_skips_without_resume(tmp_db, sched_mod):
    await tmp_db.execute("INSERT INTO profile (id) VALUES (1)")  # raw_resume NULL
    await _seed_pipeline_vacancy(tmp_db, 1, status="interested")
    await tmp_db.commit()
    res = await sched_mod._job_generate_cover_letters()
    assert res == {"processed": 0, "skipped": "no_resume"}


@pytest.mark.asyncio
async def test_job_generate_cover_letters_no_pending(tmp_db, sched_mod):
    await tmp_db.execute("INSERT INTO profile (id, raw_resume) VALUES (1, '{\"x\":1}')")
    await _seed_pipeline_vacancy(tmp_db, 1, status="new")  # не в пайплайне
    await tmp_db.commit()
    res = await sched_mod._job_generate_cover_letters()
    assert res == {"processed": 0, "skipped": "no_pending"}


# ----- start ----


def test_start_creates_scheduler_and_idempotent(sched_mod, monkeypatch):
    # apscheduler.start() реально стартует event loop — патчим
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    monkeypatch.setattr(AsyncIOScheduler, "start", lambda self: None)
    sched_mod._scheduler = None
    cli = _mk_client()
    s1 = sched_mod.start(cli, personal_interval_hours=3)
    assert s1 is not None
    s2 = sched_mod.start(cli)
    assert s2 is s1
    # 9 джобов: 7 базовых + llm_parse_requirements + cover_letter_generate
    assert len(s1.get_jobs()) == 9
    sched_mod._scheduler = None
