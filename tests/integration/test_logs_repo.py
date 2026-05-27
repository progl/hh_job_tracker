import pytest

from app.db import logs_repo


@pytest.fixture(autouse=True)
def _resync_get_db(monkeypatch):
    """e2e conftest перезагружает app.db.db; logs_repo держит старую ссылку на get_db.
    Здесь возвращаем актуальную."""
    import app.db.db as dbm

    monkeypatch.setattr(logs_repo, "get_db", dbm.get_db)


async def _insert(record_overrides=None):
    base = {
        "method": "GET",
        "path": "/search/vacancy",
        "params": None,
        "status": 200,
        "duration_ms": 50,
        "size_bytes": 1024,
        "referer": None,
        "redirect_to": None,
        "error": None,
        "kind": "search",
    }
    if record_overrides:
        base.update(record_overrides)
    await logs_repo._insert(base)


@pytest.mark.asyncio
async def test_insert_basic(tmp_db):
    await _insert()
    cur = await tmp_db.execute("SELECT path, status, kind FROM request_logs")
    r = await cur.fetchone()
    assert r["path"] == "/search/vacancy"
    assert r["status"] == 200
    assert r["kind"] == "search"


@pytest.mark.asyncio
async def test_list_logs_only_errors(tmp_db):
    await _insert({"status": 200})
    await _insert({"status": 403})
    await _insert({"status": 500})
    await _insert({"status": None, "error": "timeout"})

    rows = await logs_repo.list_logs(only_errors=True)
    assert len(rows) == 3
    # 200 не попал
    statuses = [r["status"] for r in rows]
    assert 200 not in statuses


@pytest.mark.asyncio
async def test_list_logs_status_filter_classes(tmp_db):
    await _insert({"status": 200, "path": "/a"})
    await _insert({"status": 301, "path": "/b"})
    await _insert({"status": 404, "path": "/c"})
    await _insert({"status": 500, "path": "/d"})

    for cls, expect in [("2xx", "/a"), ("3xx", "/b"), ("4xx", "/c"), ("5xx", "/d")]:
        rows = await logs_repo.list_logs(status_filter=cls)
        assert len(rows) == 1, cls
        assert rows[0]["path"] == expect


@pytest.mark.asyncio
async def test_list_logs_path_filter_and_limit(tmp_db):
    await _insert({"path": "/api/foo"})
    await _insert({"path": "/api/bar"})
    await _insert({"path": "/other"})

    rows = await logs_repo.list_logs(path_filter="api")
    assert len(rows) == 2
    assert all("api" in r["path"] for r in rows)

    rows_lim = await logs_repo.list_logs(limit=1, path_filter="api")
    assert len(rows_lim) == 1


@pytest.mark.asyncio
async def test_stats_counters(tmp_db):
    await _insert({"status": 200, "duration_ms": 100})
    await _insert({"status": 200, "duration_ms": 200})
    await _insert({"status": 301, "duration_ms": 50})
    await _insert({"status": 403, "duration_ms": 10})
    await _insert({"status": 429, "duration_ms": 20})
    await _insert({"status": None, "error": "fail"})

    s = await logs_repo.stats()
    assert s["total"] == 6
    assert s["ok_2xx"] == 2
    assert s["redir_3xx"] == 1
    assert s["antibot"] == 2  # 403 + 429
    assert s["errors"] == 1
    assert s["max_ms"] == 200


@pytest.mark.asyncio
async def test_cleanup_keeps_only_latest(tmp_db):
    for i in range(10):
        await _insert({"path": f"/p{i}"})
    deleted = await logs_repo.cleanup(keep=3)
    assert deleted == 7
    cur = await tmp_db.execute("SELECT COUNT(*) FROM request_logs")
    cnt = (await cur.fetchone())[0]
    assert cnt == 3


def test_log_request_no_running_loop_does_not_raise():
    # log_request пытается create_task; вне event loop → ловит RuntimeError и пропускает
    # не должен бросать наружу
    logs_repo.log_request(path="/x", status=200)
