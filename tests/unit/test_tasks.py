import asyncio

import pytest

from app import tasks as task_mod


@pytest.fixture(autouse=True)
def _clean_registry():
    # каждый тест получает чистый реестр задач
    task_mod._tasks.clear()
    task_mod._subs.clear()
    yield
    task_mod._tasks.clear()
    task_mod._subs.clear()


@pytest.mark.asyncio
async def test_run_completes_done():
    async def factory(ctx):
        ctx.update(current=1, total=2)
        ctx.update(current=2, total=2)
        return {"ok": True}

    t = await task_mod.run("k1", "label", factory)
    await t._async_task
    assert t.status == "done"
    assert t.result == {"ok": True}
    assert t.progress == 100
    # current синхронизировался с total
    assert t.current == 2
    # message не задавали явно — подменяется на "готово"
    assert t.message == "готово"


@pytest.mark.asyncio
async def test_run_completes_keeps_custom_message():
    async def factory(ctx):
        ctx.update(message="custom-final")
        return None

    t = await task_mod.run("k1b", "label", factory)
    await t._async_task
    assert t.status == "done"
    # явно поставленный message не подменяется
    assert t.message == "custom-final"


@pytest.mark.asyncio
async def test_run_error_sets_error_status():
    async def factory(ctx):
        raise RuntimeError("boom")

    t = await task_mod.run("k_err", "label", factory)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # дождёмся завершения runner
    try:
        await t._async_task
    except Exception:
        pass
    assert t.status == "error"
    assert t.error == "boom"
    assert "ошибка" in t.message


@pytest.mark.asyncio
async def test_run_already_running_rejects():
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory(ctx):
        started.set()
        await release.wait()
        return None

    t1 = await task_mod.run("dup", "first", factory)
    await started.wait()
    # вторая попытка должна упасть с TaskAlreadyRunning
    with pytest.raises(task_mod.TaskAlreadyRunning) as ei:
        await task_mod.run("dup", "second", factory)
    assert ei.value.kind == "dup"
    assert ei.value.task_id == t1.id

    release.set()
    await t1._async_task


@pytest.mark.asyncio
async def test_run_cancel_previous_replaces():
    started1 = asyncio.Event()
    release1 = asyncio.Event()

    async def f1(ctx):
        started1.set()
        await release1.wait()

    async def f2(ctx):
        return "second"

    t1 = await task_mod.run("k2", "first", f1)
    await started1.wait()
    t2 = await task_mod.run("k2", "second", f2, if_running="cancel_previous")
    assert t1.id != t2.id

    # первая должна быть отменена
    try:
        await t1._async_task
    except asyncio.CancelledError:
        pass
    assert t1.status == "cancelled"

    await t2._async_task
    assert t2.status == "done"


@pytest.mark.asyncio
async def test_progress_ctx_updates_percent():
    async def factory(ctx):
        ctx.update(current=3, total=10, message="working")
        return None

    t = await task_mod.run("k_pr", "label", factory)
    await t._async_task
    assert t.total == 10
    # progress сначала 30 (3/10), потом 100 после done
    assert t.status == "done"
    assert t.progress == 100
    assert t.current == 10


@pytest.mark.asyncio
async def test_list_tasks_filters_finished():
    async def f_quick(ctx):
        return None

    t = await task_mod.run("k_x", "x", f_quick)
    await t._async_task
    all_items = task_mod.list_tasks(include_finished=True)
    only_active = task_mod.list_tasks(include_finished=False)
    assert any(i["id"] == t.id for i in all_items)
    assert not any(i["id"] == t.id for i in only_active)


@pytest.mark.asyncio
async def test_find_running_returns_active():
    started = asyncio.Event()
    release = asyncio.Event()

    async def f(ctx):
        started.set()
        await release.wait()

    t = await task_mod.run("findk", "x", f)
    await started.wait()
    found = task_mod.find_running("findk")
    assert found is not None and found.id == t.id

    release.set()
    await t._async_task
    assert task_mod.find_running("findk") is None


@pytest.mark.asyncio
async def test_cancel_returns_false_when_no_task():
    assert await task_mod.cancel("nothing") is False


@pytest.mark.asyncio
async def test_cancel_active_task():
    started = asyncio.Event()
    release = asyncio.Event()

    async def f(ctx):
        started.set()
        await release.wait()

    t = await task_mod.run("ck", "x", f)
    await started.wait()
    ok = await task_mod.cancel("ck")
    assert ok is True
    assert t.status == "cancelled"
    try:
        await t._async_task
    except asyncio.CancelledError:
        pass


def test_task_to_dict_shape():
    t = task_mod.Task(id="abc", kind="k", label="L")
    d = t.to_dict()
    assert d["id"] == "abc"
    assert d["kind"] == "k"
    assert d["status"] == "queued"
    assert d["progress"] == 0
    assert "result" in d and "error" in d


def test_prune_keeps_max_history():
    # забьём _tasks > MAX_HISTORY завершёнными задачами
    import time as _t

    for i in range(task_mod._MAX_HISTORY + 5):
        t = task_mod.Task(id=f"t{i}", kind="x", label="L")
        t.status = "done"
        t.finished_at = _t.time() + i
        task_mod._tasks[t.id] = t
    task_mod._prune()
    assert len(task_mod._tasks) == task_mod._MAX_HISTORY
