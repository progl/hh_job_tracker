import time

import pytest

from app.db import job_runs_repo


@pytest.fixture(autouse=True)
def _resync_get_db(monkeypatch):
    """e2e conftest перезагружает app.db.db; job_runs_repo держит старую ссылку на get_db."""
    import app.db.db as dbm

    monkeypatch.setattr(job_runs_repo, "get_db", dbm.get_db)


@pytest.mark.asyncio
async def test_start_creates_running_row(tmp_db):
    run_id = await job_runs_repo.start("personal_refresh", trigger="manual")
    assert isinstance(run_id, int) and run_id > 0
    cur = await tmp_db.execute("SELECT job_id, status, trigger FROM job_runs WHERE id=?", (run_id,))
    r = await cur.fetchone()
    assert r["job_id"] == "personal_refresh"
    assert r["status"] == "running"
    assert r["trigger"] == "manual"


@pytest.mark.asyncio
async def test_finish_updates_row(tmp_db):
    run_id = await job_runs_repo.start("fx_refresh")
    t0 = time.monotonic()
    await job_runs_repo.finish(run_id, "ok", result={"x": 1}, started_mono=t0)
    cur = await tmp_db.execute(
        "SELECT status, result, error, duration_ms, finished_at FROM job_runs WHERE id=?",
        (run_id,),
    )
    r = await cur.fetchone()
    assert r["status"] == "ok"
    assert r["result"] is not None and '"x": 1' in r["result"]
    assert r["error"] is None
    assert r["finished_at"] is not None
    assert r["duration_ms"] is not None and r["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_finish_with_error(tmp_db):
    run_id = await job_runs_repo.start("ml_retrain")
    await job_runs_repo.finish(run_id, "error", error="boom", started_mono=None)
    cur = await tmp_db.execute("SELECT status, error, duration_ms FROM job_runs WHERE id=?", (run_id,))
    r = await cur.fetchone()
    assert r["status"] == "error"
    assert r["error"] == "boom"
    # без started_mono — duration_ms остаётся None
    assert r["duration_ms"] is None


@pytest.mark.asyncio
async def test_list_runs_filters_by_job_id(tmp_db):
    a = await job_runs_repo.start("job_a")
    b = await job_runs_repo.start("job_b")
    c = await job_runs_repo.start("job_a")
    await job_runs_repo.finish(a, "ok")
    await job_runs_repo.finish(b, "ok")
    await job_runs_repo.finish(c, "ok")

    a_runs = await job_runs_repo.list_runs(job_id="job_a")
    assert {r["id"] for r in a_runs} == {a, c}

    all_runs = await job_runs_repo.list_runs(limit=10)
    assert len(all_runs) == 3
    # ORDER BY id DESC
    assert all_runs[0]["id"] >= all_runs[-1]["id"]


@pytest.mark.asyncio
async def test_list_runs_filters_by_status(tmp_db):
    a = await job_runs_repo.start("job_a")  # останется running
    b = await job_runs_repo.start("job_b")
    c = await job_runs_repo.start("job_a")
    await job_runs_repo.finish(b, "ok")
    await job_runs_repo.finish(c, "error", error="x")

    running = await job_runs_repo.list_runs(status="running")
    assert {r["id"] for r in running} == {a}

    # статус + job_id вместе
    a_ok = await job_runs_repo.list_runs(job_id="job_a", status="running")
    assert {r["id"] for r in a_ok} == {a}
    assert await job_runs_repo.list_runs(job_id="job_b", status="error") == []


@pytest.mark.asyncio
async def test_list_runs_respects_limit(tmp_db):
    for _ in range(5):
        rid = await job_runs_repo.start("job_x")
        await job_runs_repo.finish(rid, "ok")
    runs = await job_runs_repo.list_runs(limit=2)
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_last_per_job_returns_latest(tmp_db):
    a1 = await job_runs_repo.start("job_a")
    await job_runs_repo.finish(a1, "ok")
    a2 = await job_runs_repo.start("job_a")
    await job_runs_repo.finish(a2, "error", error="x")
    b1 = await job_runs_repo.start("job_b")
    await job_runs_repo.finish(b1, "ok")

    out = await job_runs_repo.last_per_job()
    assert set(out.keys()) == {"job_a", "job_b"}
    assert out["job_a"]["id"] == a2
    assert out["job_a"]["status"] == "error"
    assert out["job_b"]["id"] == b1


@pytest.mark.asyncio
async def test_last_per_job_empty(tmp_db):
    out = await job_runs_repo.last_per_job()
    assert out == {}
