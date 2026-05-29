import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import scheduler as sched_mod


@pytest.fixture(autouse=True)
def _resync_get_db(monkeypatch):
    """e2e conftest может перезагрузить app.db.* модули; здесь синхронизируем,
    чтобы _record-декоратор писал в актуальный tmp_db."""
    import app.db.db as dbm
    import app.db.job_runs_repo as jr

    monkeypatch.setattr(jr, "get_db", dbm.get_db)
    # внутри scheduler.py есть `from app.db import job_runs_repo` — это ссылка
    # на конкретный объект модуля. Если e2e его перезагрузил, поправим.
    monkeypatch.setattr(sched_mod, "job_runs_repo", jr)


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    # каждый тест получает чистый _state и _scheduler
    sched_mod._scheduler = None
    sched_mod._state["jobs"] = {}
    sched_mod._state["started_at"] = None
    yield
    sched_mod._scheduler = None
    sched_mod._state["jobs"] = {}
    sched_mod._state["started_at"] = None


def test_status_not_running():
    assert sched_mod.status() == {"running": False}


def test_status_with_jobs(monkeypatch):
    job = MagicMock()
    job.id = "personal_refresh"
    job.next_run_time = "2026-05-23 03:00:00"
    fake_scheduler = MagicMock()
    fake_scheduler.get_jobs.return_value = [job]
    sched_mod._scheduler = fake_scheduler
    sched_mod._state["jobs"]["personal_refresh"] = {"ok": True, "result": {"saved": 5}}
    sched_mod._state["started_at"] = 12345.0

    s = sched_mod.status()
    assert s["running"] is True
    assert s["started_at"] == 12345.0
    assert len(s["jobs"]) == 1
    j = s["jobs"][0]
    assert j["id"] == "personal_refresh"
    assert j["next_run"] == "2026-05-23 03:00:00"
    assert j["last_result"] == {"ok": True, "result": {"saved": 5}}


def test_status_job_without_next_run_time():
    job = MagicMock()
    job.id = "paused_job"
    job.next_run_time = None
    fake_scheduler = MagicMock()
    fake_scheduler.get_jobs.return_value = [job]
    sched_mod._scheduler = fake_scheduler
    s = sched_mod.status()
    assert s["jobs"][0]["next_run"] is None


def test_shutdown_clears_scheduler():
    fake = MagicMock()
    sched_mod._scheduler = fake
    sched_mod.shutdown()
    fake.shutdown.assert_called_once_with(wait=False)
    assert sched_mod._scheduler is None


def test_shutdown_when_not_started():
    # не должен бросать
    sched_mod.shutdown()
    assert sched_mod._scheduler is None


@pytest.mark.asyncio
async def test_run_now_returns_when_not_started():
    res = await sched_mod.run_now("any_job")
    assert res == {"ok": False, "reason": "scheduler not started"}


@pytest.mark.asyncio
async def test_run_now_returns_when_job_not_found():
    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = None
    sched_mod._scheduler = fake_scheduler
    res = await sched_mod.run_now("missing")
    assert res["ok"] is False
    assert "not found" in res["reason"]


@pytest.mark.asyncio
async def test_run_now_returns_when_client_paused():
    client = MagicMock()
    client.status = {"paused_now": True, "paused_until": 0}
    job = MagicMock()
    job.args = [client]
    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = job
    sched_mod._scheduler = fake_scheduler
    res = await sched_mod.run_now("personal_refresh")
    assert res["ok"] is False
    assert res["reason"] == "client_paused"
    assert "wait_seconds" in res


@pytest.mark.asyncio
async def test_run_now_starts_task_and_calls_job(monkeypatch):
    # подготовим mock job
    job = MagicMock()
    job.args = ()
    job.kwargs = {}
    job.func = AsyncMock(return_value=None)

    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = job
    sched_mod._scheduler = fake_scheduler

    sched_mod._state["jobs"]["fx_refresh"] = {"ok": True, "result": "ok"}

    res = await sched_mod.run_now("fx_refresh")
    assert res["ok"] is True
    assert res["started"] == "fx_refresh"
    assert "task_id" in res

    # дождёмся завершения внутренней task
    from app import tasks as task_mod

    t = task_mod._tasks.get(res["task_id"])
    assert t is not None
    await t._async_task
    job.func.assert_awaited_once()

    # очистим registry
    task_mod._tasks.clear()


@pytest.mark.asyncio
async def test_run_now_already_running(monkeypatch):
    from app import tasks as task_mod

    task_mod._tasks.clear()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        started.set()
        await release.wait()

    job = MagicMock()
    job.args = ()
    job.kwargs = {}
    job.func = slow

    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = job
    sched_mod._scheduler = fake_scheduler

    res1 = await sched_mod.run_now("fx_refresh")
    assert res1["ok"] is True
    # дождёмся, что наша корутина начала
    await started.wait()

    res2 = await sched_mod.run_now("fx_refresh")
    assert res2["ok"] is False
    assert res2["reason"] == "already_running"
    assert res2["kind"] == "fx_refresh"

    release.set()
    t = task_mod._tasks.get(res1["task_id"])
    await t._async_task
    task_mod._tasks.clear()


@pytest.mark.asyncio
async def test_record_decorator_writes_ok(tmp_db):
    # _record декоратор пишет start/finish в job_runs
    from app.db import job_runs_repo

    @sched_mod._record("my_test_job")
    async def my_coro(x):
        return {"x": x}

    res = await my_coro(7)
    assert res == {"x": 7}
    runs = await job_runs_repo.list_runs(job_id="my_test_job")
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert '"x": 7' in runs[0]["result"]
    assert sched_mod._state["jobs"]["my_test_job"]["ok"] is True


@pytest.mark.asyncio
async def test_record_decorator_writes_error(tmp_db):
    from app.db import job_runs_repo

    @sched_mod._record("err_job")
    async def my_coro():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await my_coro()
    runs = await job_runs_repo.list_runs(job_id="err_job")
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["error"] == "nope"
    assert sched_mod._state["jobs"]["err_job"]["ok"] is False


@pytest.mark.asyncio
async def test_record_decorator_cancelled(tmp_db):
    from app.db import job_runs_repo

    @sched_mod._record("cancel_job")
    async def my_coro():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await my_coro()
    runs = await job_runs_repo.list_runs(job_id="cancel_job")
    assert len(runs) == 1
    assert runs[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_run_not_running(tmp_db):
    # нет ни в реестре, ни в БД → not_running
    res = await sched_mod.cancel_run(999999)
    assert res == {"ok": False, "reason": "not_running", "run_id": 999999}


@pytest.mark.asyncio
async def test_cancel_run_marks_orphan_interrupted(tmp_db):
    # осиротевшая 'running' строка из «прошлого процесса»: есть в БД, нет в _running
    from app.db import job_runs_repo

    run_id = await job_runs_repo.start("orphan_job")
    res = await sched_mod.cancel_run(run_id)
    assert res == {"ok": True, "run_id": run_id, "action": "marked_interrupted"}
    runs = await job_runs_repo.list_runs(job_id="orphan_job")
    assert runs[0]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_cancel_run_cancels_running_job(tmp_db):
    from app.db import job_runs_repo

    started = asyncio.Event()

    @sched_mod._record("long_job")
    async def my_coro():
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(my_coro())
    await started.wait()
    # wrapper уже зарегистрировал таск в _running по run_id
    assert len(sched_mod._running) == 1
    run_id = next(iter(sched_mod._running))

    res = await sched_mod.cancel_run(run_id)
    assert res == {"ok": True, "run_id": run_id, "action": "cancelled"}

    with pytest.raises(asyncio.CancelledError):
        await task

    # _record поймал CancelledError → записал 'cancelled' и почистил реестр
    runs = await job_runs_repo.list_runs(job_id="long_job")
    assert runs[0]["status"] == "cancelled"
    assert sched_mod._running == {}
