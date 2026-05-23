"""Apscheduler для периодических задач.

Расписание (по умолчанию):
- personal_refresh: каждые 6 часов — обновляет отклики и работодателей
- fx_refresh: раз в сутки в 03:30 — курсы ЦБ
- ml_retrain: раз в сутки в 04:00 — пересчёт ML-модели если данных достаточно
"""
import asyncio
import functools
import logging
import time
from pathlib import Path
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.clients.cbr import refresh_salary_module
from app.db import job_runs_repo
from app.clients.cookies import save_jar
from app.collector import personal as personal_collector
from app.db.db import get_db

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_state: dict[str, Any] = {"jobs": {}, "started_at": None}


def _record(job_id: str):
    """Декоратор — пишет старт/финиш в job_runs."""
    def decorator(coro_func):
        @functools.wraps(coro_func)
        async def wrapper(*args, **kwargs):
            run_id = await job_runs_repo.start(job_id)
            t0 = time.monotonic()
            try:
                res = await coro_func(*args, **kwargs)
                await job_runs_repo.finish(run_id, "ok", result=res, started_mono=t0)
                _state["jobs"][job_id] = {"ok": True, "result": res}
                return res
            except asyncio.CancelledError:
                await job_runs_repo.finish(run_id, "cancelled", started_mono=t0)
                raise
            except Exception as e:
                await job_runs_repo.finish(run_id, "error", error=str(e), started_mono=t0)
                _state["jobs"][job_id] = {"ok": False, "error": str(e)}
                raise
        return wrapper
    return decorator


@_record("personal_refresh")
async def _job_personal_refresh(hh_client, full: bool = False) -> dict:
    db = await get_db()
    try:
        res = await personal_collector.collect_negotiations(hh_client, db, max_pages=5, full=full)
        await save_jar(db, hh_client.client)
        return res
    finally:
        await db.close()


@_record("personal_full_refresh")
async def _job_personal_full_refresh(hh_client) -> dict:
    db = await get_db()
    try:
        res = await personal_collector.collect_negotiations(hh_client, db, max_pages=20, full=True)
        await save_jar(db, hh_client.client)
        return res
    finally:
        await db.close()


@_record("fx_refresh")
async def _job_fx_refresh() -> dict:
    db = await get_db()
    try:
        return await refresh_salary_module(db)
    finally:
        await db.close()


@_record("sync_searches")
async def _job_sync_searches(hh_client) -> dict:
    db = await get_db()
    try:
        from app.collector import vacancies as col
        from app.db import searches_repo
        searches = [s for s in await searches_repo.list_searches(db) if s.get("is_active")]
        if not searches:
            return {"ran": 0, "reason": "нет активных"}
        if hh_client.status.get("paused_now"):
            return {"ran": 0, "reason": "клиент на паузе"}
        results = []
        for s in searches:
            params = dict(s["params"])
            max_pages = int(params.pop("max_pages", 5))
            try:
                r = await col.collect_search(hh_client, db, params, max_pages=max_pages, search_id=s["id"])
                results.append({"id": s["id"], "name": s["name"], **r})
            except Exception as e:
                results.append({"id": s["id"], "error": str(e)})
                break
        await save_jar(db, hh_client.client)
        return {"ran": len(results), "results": results}
    finally:
        await db.close()


@_record("backfill_pending")
async def _job_backfill_pending(hh_client) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT COUNT(DISTINCT n.vacancy_id)
              FROM negotiations n
         LEFT JOIN vacancies v ON v.id = n.vacancy_id
             WHERE n.vacancy_id IS NOT NULL AND v.id IS NULL
            """
        )
        remaining = (await cur.fetchone())[0]
        if remaining == 0:
            return {"remaining": 0, "skipped": True}
        if hh_client.status.get("paused_now"):
            return {"remaining": remaining, "skipped": "paused"}
        from app.collector import vacancies as col
        res = await col.backfill_from_negotiations(hh_client, db, limit=25)
        await save_jar(db, hh_client.client)
        return res
    finally:
        await db.close()


@_record("ml_retrain")
async def _job_ml_retrain() -> dict:
    try:
        from app.scoring import ml
        res = await ml.train_if_enough_data()
        _state["jobs"]["ml_retrain"] = {"ok": True, **res}
        return res
    except Exception as e:
        log.warning("ml_retrain failed: %s", e)
        _state["jobs"]["ml_retrain"] = {"ok": False, "error": str(e)}


def start(hh_client, personal_interval_hours: int = 6) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler:
        return _scheduler
    # MemoryJobStore — потому что job-args (hh_client с httpx.Client) не picklable.
    # Расписание восстанавливается из кода при старте, история прогонов хранится в job_runs (БД).
    _scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    _scheduler.add_job(
        _job_personal_refresh, IntervalTrigger(hours=personal_interval_hours),
        args=[hh_client], id="personal_refresh", replace_existing=True,
    )
    _scheduler.add_job(
        _job_fx_refresh, CronTrigger(hour=3, minute=30),
        id="fx_refresh", replace_existing=True,
    )
    _scheduler.add_job(
        _job_ml_retrain, CronTrigger(hour=4, minute=0),
        id="ml_retrain", replace_existing=True,
    )
    _scheduler.add_job(
        _job_backfill_pending, IntervalTrigger(minutes=20),
        args=[hh_client], id="backfill_pending", replace_existing=True,
    )
    _scheduler.add_job(
        _job_sync_searches, IntervalTrigger(hours=4),
        args=[hh_client], id="sync_searches", replace_existing=True,
    )
    _scheduler.add_job(
        _job_personal_full_refresh, CronTrigger(hour=2, minute=0),
        args=[hh_client], id="personal_full_refresh", replace_existing=True,
    )
    _scheduler.start()
    import time as _t
    _state["started_at"] = _t.time()
    log.info("scheduler started: personal_refresh every %sh, fx_refresh 03:30, ml_retrain 04:00, backfill_pending every 20m", personal_interval_hours)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def status() -> dict[str, Any]:
    if not _scheduler:
        return {"running": False}
    jobs = []
    for j in _scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run": str(j.next_run_time) if j.next_run_time else None,
            "last_result": _state["jobs"].get(j.id),
        })
    return {"running": True, "started_at": _state.get("started_at"), "jobs": jobs}


_JOB_LABELS = {
    "personal_refresh": "Обновлять отклики (инкрем.)",
    "personal_full_refresh": "Полный sync откликов",
    "fx_refresh": "Курсы ЦБ",
    "ml_retrain": "Обучение ML",
    "backfill_pending": "Дотянуть вакансии",
    "sync_searches": "Синк сохранённых поисков",
}


_JOBS_NEED_CLIENT = {"backfill_pending", "personal_refresh", "sync_searches"}


async def run_now(job_id: str) -> dict[str, Any]:
    """Запускает cron-джоб немедленно. Регистрирует в task_mod, чтобы было видно в UI."""
    if not _scheduler:
        return {"ok": False, "reason": "scheduler not started"}
    j = _scheduler.get_job(job_id)
    if not j:
        return {"ok": False, "reason": f"job {job_id} not found"}
    # проверка паузы клиента
    if job_id in _JOBS_NEED_CLIENT:
        client = (j.args or [None])[0]
        if client and client.status.get("paused_now"):
            import time as _t
            wait_s = max(0, int(client.status.get("paused_until", 0) - _t.monotonic()))
            return {"ok": False, "reason": "client_paused", "wait_seconds": wait_s,
                    "message": f"клиент HH на паузе ещё ~{wait_s//60}м из-за anti-bot; попробуй позже"}
    from app import tasks as task_mod

    async def _wrap(ctx):
        ctx.update(message="запущено по требованию…")
        await j.func(*(j.args or ()), **(j.kwargs or {}))
        return _state["jobs"].get(job_id)

    try:
        t = await task_mod.run(job_id, _JOB_LABELS.get(job_id, job_id), _wrap)
        return {"ok": True, "started": job_id, "task_id": t.id}
    except task_mod.TaskAlreadyRunning:
        return {"ok": False, "reason": "already_running", "kind": job_id}
