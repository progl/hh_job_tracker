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
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.clients.cbr import refresh_salary_module
from app.clients.cookies import save_jar
from app.collector import personal as personal_collector
from app.db import job_runs_repo, vacancies_repo
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

        # Уведомление о новых INVITATION/INTERVIEW за последний час
        try:
            from app import notify

            if await notify.is_enabled(db):
                cur = await db.execute(
                    """
                    SELECT n.last_state, v.name, v.company_name
                      FROM negotiations n
                      LEFT JOIN vacancies v ON v.id = n.vacancy_id
                     WHERE n.last_state IN ('INVITATION', 'INTERVIEW')
                       AND datetime(n.last_modified) >= datetime('now', '-1 hour')
                    """
                )
                fresh = await cur.fetchall()
                if fresh:
                    msg_lines = []
                    for r in fresh[:3]:
                        st = "📩 " if r["last_state"] == "INVITATION" else "🎤 "
                        msg_lines.append(f"{st}{(r['name'] or '?')[:50]} — {(r['company_name'] or '?')[:30]}")
                    more = f" (+{len(fresh) - 3})" if len(fresh) > 3 else ""
                    await notify.send(
                        title=f"HH: {len(fresh)} приглашений/собесов{more}",
                        message="\n".join(msg_lines),
                    )
        except Exception as e:
            log.warning("personal_refresh: notification failed: %s", e)

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
        from app.collector import personal as personal_col
        from app.collector import vacancies as col
        from app.db import searches_repo

        # Перед прогоном — обновим resume-хеш у активных рекомендаций
        # (HH периодически меняет ?resume=... — без этого синк отдаст пустоту/403)
        try:
            ref = await personal_col.sync_resume_token_into_searches(hh_client, db)
            if ref.get("refreshed") and ref.get("searches_updated"):
                log.info(
                    "sync_searches: refreshed resume token, updated %s searches", ref["searches_updated"]
                )
        except Exception as e:
            log.warning("sync_searches: refresh resume token failed: %s", e)

        searches = [s for s in await searches_repo.list_searches(db) if s.get("is_active")]
        if not searches:
            return {"ran": 0, "reason": "нет активных"}
        if hh_client.status.get("paused_now"):
            return {"ran": 0, "reason": "клиент на паузе"}
        results = []
        for s in searches:
            params = dict(s["params"])
            max_pages = int(params.pop("max_pages", 5))
            # Early-stop: для рекомендаций (по resume) — K=5 (порядок может тасоваться),
            # для обычных по publication_time — K=3 (строгий порядок).
            es_k = int(params.pop("early_stop_seen", 5 if params.get("resume") else 3))
            try:
                r = await col.collect_search(
                    hh_client,
                    db,
                    params,
                    max_pages=max_pages,
                    search_id=s["id"],
                    early_stop_consecutive_seen=es_k,
                )
                results.append({"id": s["id"], "name": s["name"], **r})
            except Exception as e:
                results.append({"id": s["id"], "error": str(e)})
                break
        await save_jar(db, hh_client.client)
        try:
            dd = await vacancies_repo.mark_duplicates_as_skipped(db)
            if dd.get("marked"):
                log.info("dedup after sync_searches: groups=%s marked=%s", dd["groups"], dd["marked"])
        except Exception as e:
            log.warning("dedup after sync_searches failed: %s", e)

        # Уведомления о новых вакансиях с высоким match-score (≥75)
        # Берём те что появились за последний час с подсчитанным локально score.
        try:
            from app import notify
            from app.db import employers_repo, profile_repo
            from app.scoring.match import score_vacancy

            if await notify.is_enabled(db):
                cur = await db.execute(
                    """
                    SELECT v.* FROM vacancies v
                    LEFT JOIN vacancy_status s ON s.vacancy_id = v.id
                    WHERE datetime(v.seen_at) >= datetime('now', '-1 hour')
                      AND COALESCE(s.status, 'new') = 'new'
                      AND v.disappeared_at IS NULL
                    """
                )
                rows = await cur.fetchall()
                if rows:
                    profile = await profile_repo.get_profile(db)
                    emp_map = await employers_repo.get_map(db)
                    new_high = []
                    for r in rows:
                        rd = dict(r)
                        import json as _json

                        for f in ("parsed_stack", "work_formats", "key_skills"):
                            if isinstance(rd.get(f), str):
                                try:
                                    rd[f] = _json.loads(rd[f])
                                except Exception:
                                    rd[f] = []
                        emp_pol = emp_map.get(rd.get("company_id")) if rd.get("company_id") else None
                        sc = score_vacancy(rd, profile, emp_pol)
                        if sc["score"] >= 75:
                            new_high.append((sc["score"], rd["name"], rd.get("company_name") or "?"))
                    if new_high:
                        new_high.sort(reverse=True)
                        top = new_high[:3]
                        msg_lines = [f"{s}% · {n[:50]} — {c[:30]}" for s, n, c in top]
                        more = f" (+{len(new_high) - len(top)})" if len(new_high) > len(top) else ""
                        await notify.send(
                            title=f"HH: {len(new_high)} новых вакансий с match ≥75{more}",
                            message="\n".join(msg_lines),
                        )
        except Exception as e:
            log.warning("sync_searches: notification failed: %s", e)

        return {"ran": len(results), "results": results}
    finally:
        await db.close()


@_record("dedup_vacancies")
async def _job_dedup_vacancies() -> dict:
    db = await get_db()
    try:
        return await vacancies_repo.mark_duplicates_as_skipped(db)
    finally:
        await db.close()


@_record("llm_parse_requirements")
async def _job_llm_parse_requirements() -> dict:
    """Раз в час: берёт N вакансий без 'requirements' анализа (или без хоть одного включённого
    анализа) и прогоняет ВСЕ включённые анализаторы.
    Каждый под-анализ отдельно логируется в llm_runs (через registry.analyze_one)."""
    batch = 20
    db = await get_db()
    try:
        from app.llm.registry import analyze_one, get_enabled_analyzers

        enabled = await get_enabled_analyzers(db)
        if not enabled:
            return {"processed": 0, "skipped": "no_enabled_analyzers"}
        # «не обработано» = нет ни одного из vacancy_requirements (для 'requirements')
        # либо нет строки в vacancy_analysis с любым из enabled kinds (для остальных).
        # Простой эвристикой: смотрим только requirements (это всегда включено по умолчанию)
        cur = await db.execute(
            """
            SELECT v.id FROM vacancies v
            LEFT JOIN vacancy_requirements r ON r.vacancy_id = v.id
            WHERE v.description IS NOT NULL AND length(v.description) > 100
              AND r.id IS NULL
            ORDER BY v.id DESC
            LIMIT ?
            """,
            (batch,),
        )
        ids = [r[0] for r in await cur.fetchall()]
        if not ids:
            return {"processed": 0, "skipped": "no_unparsed", "enabled": enabled}
        total_ok = 0
        per_kind: dict[str, int] = dict.fromkeys(enabled, 0)
        for vid in ids:
            try:
                results = await analyze_one(db, vid, enabled)
                for r in results:
                    if r.ok:
                        total_ok += 1
                        per_kind[r.kind] = per_kind.get(r.kind, 0) + 1
            except Exception as e:
                log.warning("llm_parse_requirements: vid=%s failed: %s", vid, e)
        return {"processed": len(ids), "ok": total_ok, "per_kind": per_kind, "enabled": enabled}
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
        _job_personal_refresh,
        IntervalTrigger(hours=personal_interval_hours),
        args=[hh_client],
        id="personal_refresh",
        replace_existing=True,
    )
    _scheduler.add_job(
        _job_fx_refresh,
        CronTrigger(hour=3, minute=30),
        id="fx_refresh",
        replace_existing=True,
    )
    _scheduler.add_job(
        _job_ml_retrain,
        CronTrigger(hour=4, minute=0),
        id="ml_retrain",
        replace_existing=True,
    )
    _scheduler.add_job(
        _job_backfill_pending,
        IntervalTrigger(minutes=20),
        args=[hh_client],
        id="backfill_pending",
        replace_existing=True,
    )
    _scheduler.add_job(
        _job_sync_searches,
        IntervalTrigger(hours=4),
        args=[hh_client],
        id="sync_searches",
        replace_existing=True,
    )
    _scheduler.add_job(
        _job_personal_full_refresh,
        CronTrigger(hour=2, minute=0),
        args=[hh_client],
        id="personal_full_refresh",
        replace_existing=True,
    )
    _scheduler.add_job(
        _job_dedup_vacancies,
        CronTrigger(hour=3, minute=45),
        id="dedup_vacancies",
        replace_existing=True,
    )
    _scheduler.add_job(
        _job_llm_parse_requirements,
        IntervalTrigger(hours=1),
        id="llm_parse_requirements",
        replace_existing=True,
    )
    _scheduler.start()
    import time as _t

    _state["started_at"] = _t.time()
    log.info(
        "scheduler started: personal_refresh every %sh, fx_refresh 03:30, ml_retrain 04:00, backfill_pending every 20m",
        personal_interval_hours,
    )
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
        jobs.append(
            {
                "id": j.id,
                "next_run": str(j.next_run_time) if j.next_run_time else None,
                "last_result": _state["jobs"].get(j.id),
            }
        )
    return {"running": True, "started_at": _state.get("started_at"), "jobs": jobs}


_JOB_LABELS = {
    "personal_refresh": "Обновлять отклики (инкрем.)",
    "personal_full_refresh": "Полный sync откликов",
    "fx_refresh": "Курсы ЦБ",
    "ml_retrain": "Обучение ML",
    "backfill_pending": "Дотянуть вакансии",
    "sync_searches": "Синк сохранённых поисков",
    "dedup_vacancies": "Дедуп вакансий",
    "llm_parse_requirements": "LLM: разбор требований",
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
            return {
                "ok": False,
                "reason": "client_paused",
                "wait_seconds": wait_s,
                "message": f"клиент HH на паузе ещё ~{wait_s // 60}м из-за anti-bot; попробуй позже",
            }
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
