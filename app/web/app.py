import asyncio
import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import scheduler as scheduler_mod
from app import tasks as task_mod
from fastapi.responses import StreamingResponse
from app.clients import cbr as cbr_client
from app.clients.cookies import load_jar, save_jar
from app.clients.hh import AntibotChallengeError, HHClient, SessionExpiredError
from app.collector import favorites as fav_collector
from app.collector import personal as personal_collector
from app.collector import vacancies as collector
from app.config import settings
from app.db import employers_repo, funnel_repo, logs_repo, negotiations_repo, profile_repo, searches_repo, vacancies_repo
from app.db.db import get_db, init_db
from app.parsers.state import extract_initial_state
from app.scoring import ml as ml_module
from app.scoring.match import score_vacancy
from app.scoring.predict import predict_invite_prob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# auto_reload=False + дефолтный кеш — иначе jinja2 парсит шаблоны на каждый запрос (2-3 сек на большой странице)
templates.env.auto_reload = False


def _human_money(x: int | float | None) -> str:
    if x is None or x == 0:
        return "—"
    try:
        x = int(x)
    except (TypeError, ValueError):
        return "—"
    if abs(x) >= 1_000_000:
        return f"{x / 1_000_000:.1f}М".replace(".0М", "М")
    if abs(x) >= 1_000:
        return f"{x // 1_000}К"
    return str(x)


templates.env.filters["money"] = _human_money


def render(name: str, **ctx: Any) -> HTMLResponse:
    return HTMLResponse(
        templates.get_template(name).render(**ctx),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


hh_client = HHClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    db = await get_db()
    try:
        cookies = await load_jar(db, env_cookie_header=settings.HH_COOKIE)
        await cbr_client.refresh_salary_module(db)
    finally:
        await db.close()
    await hh_client.start(initial_cookies=cookies)
    ml_module.reload_model()
    scheduler_mod.start(hh_client)
    try:
        yield
    finally:
        scheduler_mod.shutdown()
        db = await get_db()
        try:
            await save_jar(db, hh_client.client)
        except Exception as e:
            logging.warning("save jar failed: %s", e)
        finally:
            await db.close()
        await hh_client.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


STATUSES = ["new", "viewed", "applied", "interview", "rejected", "offer", "skipped"]
STATUS_LABELS = {
    "new": "Новое", "viewed": "Просмотрел", "applied": "Откликнулся",
    "interview": "Собес", "rejected": "Отказ", "offer": "Оффер", "skipped": "Скип",
}
STATUS_COLORS = {
    "new": "bg-blue-100 text-blue-800",
    "viewed": "bg-neutral-100 text-neutral-700",
    "applied": "bg-amber-100 text-amber-800",
    "interview": "bg-violet-100 text-violet-800",
    "rejected": "bg-red-100 text-red-700",
    "offer": "bg-emerald-100 text-emerald-800",
    "skipped": "bg-neutral-100 text-neutral-500 line-through",
}

NEGOTIATION_STATE_LABELS = {
    None: "—", "RESPONSE": "ждёт", "INVITATION": "приглашение", "INTERVIEW": "собес",
    "DISCARD": "отказ", "DISCARD_NO_INTERACTION": "отказ (без интер.)",
    "DISCARD_BY_APPLICANT": "отозвал", "HIRED": "оффер",
}
NEGOTIATION_STATE_COLORS = {
    "RESPONSE": "bg-amber-100 text-amber-800",
    "INVITATION": "bg-emerald-100 text-emerald-800",
    "INTERVIEW": "bg-violet-100 text-violet-800",
    "DISCARD": "bg-red-100 text-red-700",
    "DISCARD_NO_INTERACTION": "bg-red-100 text-red-700",
    "DISCARD_BY_APPLICANT": "bg-neutral-100 text-neutral-500",
    "HIRED": "bg-emerald-200 text-emerald-900",
}


async def _enrich_with_scoring(db, rows: list[dict]) -> list[dict]:
    profile = await profile_repo.get_profile(db)
    emp_map = await employers_repo.get_map(db)
    neg_map = await negotiations_repo.map_vacancy_to_state(db)
    out = []
    for r in rows:
        emp_pol = emp_map.get(r.get("company_id")) if r.get("company_id") else None
        sc = score_vacancy(r, profile, emp_pol)
        pred = predict_invite_prob(sc["score"], emp_pol, r.get("total_responses_count"), r)
        r["score"] = sc["score"]
        r["score_parts"] = sc["parts"]
        r["politeness"] = emp_pol
        r["predict"] = pred["prob"]
        r["predict_source"] = pred.get("source", "heuristic")
        r["predict_explain"] = pred.get("explain")
        neg = neg_map.get(r["id"])
        if neg:
            ls = neg.get("last_state")
            r["neg_state"] = ls
            r["neg_label"] = NEGOTIATION_STATE_LABELS.get(ls, ls or "—")
            r["neg_color"] = NEGOTIATION_STATE_COLORS.get(ls, "bg-neutral-100 text-neutral-700")
            r["neg_archived"] = neg.get("archived")
            r["neg_viewed"] = neg.get("viewed_by_opponent")
        out.append(r)
    return out


def _filters_from_query(statuses, only_remote, text, stack, level, salary_rub_min,
                        sort_by=None, sort_dir="desc", statuses_exclude=None,
                        neg_states=None, neg_states_exclude=None,
                        show_disappeared="hide", show_archived="hide"):
    return {
        "statuses": statuses or None,
        "statuses_exclude": statuses_exclude or None,
        "neg_states": neg_states or None,
        "neg_states_exclude": neg_states_exclude or None,
        "only_remote": only_remote,
        "text": text or None,
        "stack_any": stack or None,
        "level": level or None,
        "salary_rub_min": salary_rub_min,
        "sort_by": sort_by or None,
        "sort_dir": sort_dir or "desc",
        "show_disappeared": show_disappeared,
        "show_archived": show_archived,
    }


PY_SORT_KEYS = {"score", "predict"}


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    status: list[str] | None = Query(None),
    hide_status: list[str] | None = Query(None),
    neg: list[str] | None = Query(None),
    hide_neg: list[str] | None = Query(None),
    only_remote: bool = Query(False),
    q: str | None = Query(None),
    stack: list[str] | None = Query(None),
    level: str | None = Query(None),
    salary_rub_min: int | None = Query(None),
    sort: str | None = Query(None),
    dir: str = Query("desc"),
    disappeared: str = Query("hide"),  # hide | only | all
    archived: str = Query("hide"),     # hide | only | all
):
    # «Скип» всегда скрыт автоматически — кроме режима архива (когда пользователь явно показывает только skipped).
    archive_mode = bool(status and "skipped" in status)
    if not archive_mode:
        hide_status = list(hide_status or [])
        if "skipped" not in hide_status:
            hide_status.append("skipped")

    db = await get_db()
    try:
        counts = await vacancies_repo.count_vacancies(db)
        sql_sort = None if sort in PY_SORT_KEYS else sort
        filters = _filters_from_query(status, only_remote, q, stack, level, salary_rub_min,
                                      sql_sort, dir, hide_status, neg, hide_neg,
                                      disappeared, archived)
        rows = await vacancies_repo.list_vacancies(db, **filters, limit=400)
        rows = await _enrich_with_scoring(db, rows)
        if sort in PY_SORT_KEYS:
            reverse = (dir or "desc").lower() == "desc"
            rows.sort(key=lambda r: (r.get(sort) is None, r.get(sort) or 0), reverse=reverse)
        funnel = await negotiations_repo.counters(db)
        profile = await profile_repo.get_profile(db)
        searches = await searches_repo.list_searches(db)
        # counts: сколько вакансий с disappeared_at / archived_at для кнопок в UI
        cur = await db.execute("SELECT COUNT(*) FROM vacancies WHERE disappeared_at IS NOT NULL")
        disappeared_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM vacancies WHERE archived_at IS NOT NULL")
        archived_count = (await cur.fetchone())[0]
    finally:
        await db.close()
    return render(
        "index.html", request=request, status=hh_client.status,
        counts=counts, funnel=funnel, profile=profile, rows=rows, searches=searches,
        disappeared_count=disappeared_count,
        archived_count=archived_count,
        statuses=STATUSES, status_labels=STATUS_LABELS, status_colors=STATUS_COLORS,
        applied={
            "status": status or [],
            "hide_status": hide_status or [],
            "neg": neg or [],
            "hide_neg": hide_neg or [],
            "only_remote": only_remote,
            "q": q or "",
            "stack": stack or [],
            "level": level or "",
            "salary_rub_min": salary_rub_min or "",
            "sort": sort or "",
            "dir": dir,
            "disappeared": disappeared,
            "archived": archived,
        },
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "client": hh_client.status, "scheduler": scheduler_mod.status()}


@app.post("/api/backfill")
async def backfill(limit: int = Form(200)):
    async def job(ctx):
        db = await get_db()
        try:
            res = await collector.backfill_from_negotiations(
                hh_client, db, limit=limit,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(current, total, message),
            )
            await save_jar(db, hh_client.client)
            return res
        finally:
            await db.close()
    try:
        t = await task_mod.run("backfill", "Подтянуть вакансии", job)
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


@app.post("/api/vacancy/{vid}/refresh")
async def refresh_vacancy(vid: int):
    db = await get_db()
    try:
        ok = await collector.collect_one_vacancy(hh_client, db, vid)
        await db.commit()
        await save_jar(db, hh_client.client)
        return {"ok": ok, "id": vid}
    finally:
        await db.close()


@app.post("/api/dedup")
async def dedup_vacancies():
    """Помечает дубликаты по нормализованной паре (name, company_name) как skipped.
    В каждой группе остаётся вакансия с минимальным id, остальные → skipped с пометкой 'дубликат #ID'.
    """
    db = await get_db()
    try:
        res = await vacancies_repo.mark_duplicates_as_skipped(db)
        return {"ok": True, **res}
    finally:
        await db.close()


@app.post("/api/fx/refresh")
async def fx_refresh():
    async def job(ctx):
        ctx.update(message="запрос к cbr-xml-daily…")
        db = await get_db()
        try:
            res = await cbr_client.refresh_salary_module(db)
            return res
        finally:
            await db.close()
    try:
        t = await task_mod.run("fx_refresh", "Курсы ЦБ", job, if_running="reject")
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


@app.post("/api/ml/train")
async def ml_train():
    async def job(ctx):
        ctx.update(message="обучаю на текущих негоциях…")
        res = await ml_module.train_if_enough_data()
        ml_module.reload_model()
        return res
    try:
        t = await task_mod.run("ml_train", "Обучить ML", job, if_running="reject")
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


@app.get("/api/export.csv")
async def export_csv():
    db = await get_db()
    try:
        rows = await vacancies_repo.list_vacancies(db, limit=10000)
        rows = await _enrich_with_scoring(db, rows)
    finally:
        await db.close()
    fields = [
        "id", "name", "company_name", "area_name", "url",
        "salary_rub", "salary_currency", "is_remote", "level",
        "score", "predict", "status", "neg_label",
        "responses_count", "total_responses_count", "online_users_count",
        "parsed_stack", "updated_at",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        out = dict(r)
        out["parsed_stack"] = ", ".join(r.get("parsed_stack") or [])
        w.writerow(out)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="vacancies.csv"'},
    )


@app.get("/api/export.json")
async def export_json():
    db = await get_db()
    try:
        rows = await vacancies_repo.list_vacancies(db, limit=10000)
        rows = await _enrich_with_scoring(db, rows)
    finally:
        await db.close()
    return Response(
        content=json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="vacancies.json"'},
    )


@app.get("/api/probe")
async def probe():
    try:
        html = await hh_client.get_page("/applicant/negotiations")
    except SessionExpiredError as e:
        return JSONResponse({"ok": False, "reason": "session_expired", "detail": str(e)}, status_code=401)
    except AntibotChallengeError as e:
        return JSONResponse({"ok": False, "reason": "antibot", "detail": str(e)}, status_code=429)
    state = extract_initial_state(html)
    if not state:
        return JSONResponse({"ok": False, "reason": "no_initial_state"}, status_code=500)
    account = state.get("account", {})
    info = state.get("applicantInfo", {})
    db = await get_db()
    try:
        await save_jar(db, hh_client.client)
    finally:
        await db.close()
    return {
        "ok": True, "hhid": state.get("hhid"),
        "name": f"{account.get('firstName', '')} {account.get('lastName', '')}".strip(),
        "email": account.get("email"), "pro": state.get("stateHhPro"),
        "resumes_total": info.get("total"), "resumes_finished": info.get("finished"),
        "negotiations_counters": state.get("applicantNegotiationsCounters"),
    }


def _task_response(t) -> JSONResponse:
    return JSONResponse({"ok": True, "task": t.to_dict()})


@app.post("/api/collect")
async def collect(
    text: str = Form("python"),
    only_remote: bool = Form(False),
    area: str | None = Form(None),
    max_pages: int = Form(5),
    order_by: str = Form("publication_time"),
):
    params: dict[str, Any] = {"text": text, "items_on_page": 20, "order_by": order_by}
    if only_remote:
        params["schedule"] = "remote"
    if area:
        params["area"] = area

    async def job(ctx):
        db = await get_db()
        try:
            ctx.update(message="ищу на hh.ru…")
            res = await collector.collect_search(
                hh_client, db, params, max_pages=max_pages,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(current, total, message),
            )
            await save_jar(db, hh_client.client)
            return res
        finally:
            await db.close()

    try:
        t = await task_mod.run("collect_vacancies", f"Сбор «{text}»", job)
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


@app.post("/api/collect/personal")
async def collect_personal(
    max_pages: int = Form(5),
    import_resume: bool = Form(True),
    import_favorites: bool = Form(False),
    full: bool = Form(False),
):
    async def job(ctx):
        db = await get_db()
        try:
            ctx.update(message=("полный sync…" if full else "инкрем. sync…"))
            neg_res = await personal_collector.collect_negotiations(
                hh_client, db, max_pages=max_pages, full=full,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(current, total, message),
            )
            resume_res = None
            if import_resume:
                ctx.update(message="импорт резюме…")
                try:
                    resume_res = await personal_collector.collect_resume(hh_client, db, neg_res.get("resume_id"))
                except Exception as e:
                    resume_res = {"ok": False, "error": str(e)}
            fav_res = None
            if import_favorites:
                ctx.update(message="избранное…")
                try:
                    fav_res = await fav_collector.collect_favorites(hh_client, db, max_pages=3)
                except Exception as e:
                    fav_res = {"ok": False, "error": str(e)}
            await save_jar(db, hh_client.client)
            return {"negotiations": neg_res, "resume": resume_res, "favorites": fav_res}
        finally:
            await db.close()

    try:
        t = await task_mod.run("collect_personal", "Обновить мои отклики", job)
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


@app.get("/api/tasks")
async def list_tasks_endpoint():
    return {"tasks": task_mod.list_tasks(limit=30)}


@app.post("/api/tasks/{kind}/cancel")
async def cancel_task(kind: str):
    ok = await task_mod.cancel(kind)
    return {"ok": ok, "kind": kind}


@app.get("/api/tasks/stream")
async def tasks_stream():
    async def gen():
        async for chunk in task_mod.subscribe():
            yield chunk
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/searches")
async def searches_list():
    db = await get_db()
    try:
        return {"searches": await searches_repo.list_searches(db)}
    finally:
        await db.close()


@app.post("/api/searches")
async def searches_create(
    name: str = Form(...),
    text: str = Form(""),
    area: str = Form(""),
    only_remote: bool = Form(False),
    max_pages: int = Form(5),
    order_by: str = Form("publication_time"),
):
    params: dict[str, Any] = {"text": text, "items_on_page": 20, "order_by": order_by, "max_pages": max_pages}
    if only_remote:
        params["schedule"] = "remote"
    if area:
        params["area"] = area
    db = await get_db()
    try:
        sid = await searches_repo.create_search(db, name, params)
        return {"ok": True, "id": sid}
    finally:
        await db.close()


@app.delete("/api/searches/{sid}")
async def searches_delete(sid: int):
    db = await get_db()
    try:
        await searches_repo.delete_search(db, sid)
        return {"ok": True}
    finally:
        await db.close()


@app.post("/api/searches/{sid}/toggle")
async def searches_toggle(sid: int):
    db = await get_db()
    try:
        cur = await searches_repo.get(db, sid)
        if not cur:
            return JSONResponse({"ok": False, "reason": "not_found"}, status_code=404)
        await searches_repo.update_search(db, sid, is_active=int(not cur["is_active"]))
        return {"ok": True, "is_active": int(not cur["is_active"])}
    finally:
        await db.close()


@app.post("/api/searches/{sid}/run")
async def searches_run_one(sid: int):
    async def job(ctx):
        db = await get_db()
        try:
            s = await searches_repo.get(db, sid)
            if not s:
                raise RuntimeError(f"search {sid} not found")
            params = dict(s["params"])
            max_pages = int(params.pop("max_pages", 5))
            ctx.update(message=f"«{s['name']}»…")
            res = await collector.collect_search(
                hh_client, db, params, max_pages=max_pages, search_id=sid,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(current, total, message),
            )
            await save_jar(db, hh_client.client)
            return {"name": s["name"], **res}
        finally:
            await db.close()
    try:
        t = await task_mod.run(f"search_{sid}", f"Поиск #{sid}", job)
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


@app.post("/api/searches/sync-all")
async def searches_sync_all():
    async def job(ctx):
        db = await get_db()
        try:
            searches = [s for s in await searches_repo.list_searches(db) if s.get("is_active")]
            if not searches:
                return {"ok": True, "ran": 0, "note": "нет активных поисков"}
            ctx.update(current=0, total=len(searches), message=f"найдено {len(searches)}")
            results = []
            for i, s in enumerate(searches, 1):
                params = dict(s["params"])
                max_pages = int(params.pop("max_pages", 5))
                ctx.update(current=i, total=len(searches), message=f"{i}/{len(searches)}: «{s['name']}»")
                try:
                    r = await collector.collect_search(
                        hh_client, db, params, max_pages=max_pages, search_id=s["id"],
                    )
                    results.append({"id": s["id"], "name": s["name"], **r})
                except (SessionExpiredError, AntibotChallengeError) as e:
                    results.append({"id": s["id"], "name": s["name"], "error": str(e)})
                    break
            await save_jar(db, hh_client.client)
            return {"ran": len(results), "results": results}
        finally:
            await db.close()
    try:
        t = await task_mod.run("sync_searches", "Синхронизировать все поиски", job)
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


@app.get("/api/cleanup/preview")
async def cleanup_preview():
    """Сколько вакансий считается мусором: без сохранённого поиска, без отклика, без атрибуции запроса."""
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT COUNT(*) FROM vacancies v
             WHERE NOT EXISTS (SELECT 1 FROM search_vacancy_seen WHERE vacancy_id = v.id)
               AND NOT EXISTS (SELECT 1 FROM negotiations       WHERE vacancy_id = v.id)
               AND NOT EXISTS (SELECT 1 FROM vacancy_collected_via WHERE vacancy_id = v.id)
            """
        )
        n = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM vacancies")
        total = (await cur.fetchone())[0]
        return {"will_delete": n, "total": total, "keep": total - n}
    finally:
        await db.close()


@app.post("/api/cleanup")
async def cleanup(also_resync: bool = Form(False)):
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT id FROM vacancies v
             WHERE NOT EXISTS (SELECT 1 FROM search_vacancy_seen WHERE vacancy_id = v.id)
               AND NOT EXISTS (SELECT 1 FROM negotiations       WHERE vacancy_id = v.id)
               AND NOT EXISTS (SELECT 1 FROM vacancy_collected_via WHERE vacancy_id = v.id)
            """
        )
        ids = [r[0] for r in await cur.fetchall()]
        if ids:
            placeholders = ",".join("?" * len(ids))
            await db.execute(f"DELETE FROM vacancy_status WHERE vacancy_id IN ({placeholders})", ids)
            await db.execute(f"DELETE FROM vacancies WHERE id IN ({placeholders})", ids)
            await db.commit()
    finally:
        await db.close()

    if also_resync:
        try:
            async def job(ctx):
                inner_db = await get_db()
                try:
                    searches = [s for s in await searches_repo.list_searches(inner_db) if s.get("is_active")]
                    ctx.update(current=0, total=len(searches), message=f"{len(searches)} активных")
                    results = []
                    for i, s in enumerate(searches, 1):
                        params = dict(s["params"])
                        max_pages = int(params.pop("max_pages", 5))
                        ctx.update(current=i, total=len(searches), message=f"{i}/{len(searches)}: «{s['name']}»")
                        try:
                            r = await collector.collect_search(hh_client, inner_db, params, max_pages=max_pages, search_id=s["id"])
                            results.append({"id": s["id"], "name": s["name"], **r})
                        except (SessionExpiredError, AntibotChallengeError) as e:
                            results.append({"id": s["id"], "error": str(e)})
                            break
                    await save_jar(inner_db, hh_client.client)
                    return {"ran": len(results), "results": results}
                finally:
                    await inner_db.close()
            await task_mod.run("sync_searches", "Пересборка сохранённых поисков", job, if_running="cancel_previous")
        except Exception as e:
            return {"ok": True, "deleted": len(ids), "resync_started": False, "error": str(e)}

    return {"ok": True, "deleted": len(ids), "resync_started": bool(also_resync)}


@app.post("/api/scheduler/{job_id}/run-now")
async def scheduler_run_now(job_id: str):
    return await scheduler_mod.run_now(job_id)


@app.post("/api/client/unpause")
async def client_unpause():
    return {"ok": True, **hh_client.unpause()}


@app.get("/api/status")
async def status_endpoint():
    return {
        "client": hh_client.status,
        "scheduler": scheduler_mod.status(),
        "tasks": task_mod.list_tasks(limit=10),
    }


@app.get("/api/vacancies", response_class=HTMLResponse)
async def vacancies_fragment(
    request: Request,
    status: list[str] | None = Query(None),
    hide_status: list[str] | None = Query(None),
    neg: list[str] | None = Query(None),
    hide_neg: list[str] | None = Query(None),
    only_remote: bool = Query(False),
    q: str | None = Query(None),
    stack: list[str] | None = Query(None),
    level: str | None = Query(None),
    salary_rub_min: int | None = Query(None),
    sort: str | None = Query(None),
    dir: str = Query("desc"),
    disappeared: str = Query("hide"),
    archived: str = Query("hide"),
):
    db = await get_db()
    try:
        sql_sort = None if sort in PY_SORT_KEYS else sort
        filters = _filters_from_query(status, only_remote, q, stack, level, salary_rub_min,
                                      sql_sort, dir, hide_status, neg, hide_neg,
                                      disappeared, archived)
        rows = await vacancies_repo.list_vacancies(db, **filters, limit=500)
        rows = await _enrich_with_scoring(db, rows)
        if sort in PY_SORT_KEYS:
            reverse = (dir or "desc").lower() == "desc"
            rows.sort(key=lambda r: (r.get(sort) is None, r.get(sort) or 0), reverse=reverse)
    finally:
        await db.close()
    return render(
        "_table.html", request=request, rows=rows,
        status_labels=STATUS_LABELS, status_colors=STATUS_COLORS, statuses=STATUSES,
    )


@app.post("/api/vacancy/{vid}/status", response_class=HTMLResponse)
async def set_vacancy_status(request: Request, vid: int, status: str = Form(...), note: str | None = Form(None)):
    if status not in STATUSES:
        raise HTTPException(400, f"unknown status: {status}")
    db = await get_db()
    try:
        await vacancies_repo.set_status(db, vid, status, note)
        row = await vacancies_repo.get_vacancy(db, vid)
        rows = await _enrich_with_scoring(db, [row]) if row else []
    finally:
        await db.close()
    if not rows:
        raise HTTPException(404)
    return render(
        "_row.html", request=request, v=rows[0],
        status_labels=STATUS_LABELS, status_colors=STATUS_COLORS, statuses=STATUSES,
    )


@app.get("/vacancy/{vid}", response_class=HTMLResponse)
async def vacancy_detail(request: Request, vid: int):
    db = await get_db()
    try:
        v = await vacancies_repo.get_vacancy(db, vid)
        v_enriched = (await _enrich_with_scoring(db, [v]))[0] if v else None
    finally:
        await db.close()
    if not v_enriched:
        raise HTTPException(404)
    return render(
        "vacancy.html", request=request, v=v_enriched,
        statuses=STATUSES, status_labels=STATUS_LABELS, status_colors=STATUS_COLORS,
    )


@app.get("/compare", response_class=HTMLResponse)
async def compare(request: Request, ids: list[int] = Query(None)):
    ids = ids or []
    db = await get_db()
    try:
        vacs = []
        for vid in ids[:6]:
            v = await vacancies_repo.get_vacancy(db, vid)
            if v:
                vacs.append(v)
        vacs = await _enrich_with_scoring(db, vacs)
    finally:
        await db.close()

    def fmt_money(x):
        return f"{x:,}".replace(",", " ") if x else "—"

    fields = [
        {"label": "Должность", "html": True, "vals": [
            f'<a class="text-blue-600 hover:underline" href="/vacancy/{v["id"]}">{v["name"]}</a>' for v in vacs
        ]},
        {"label": "Компания", "vals": [v.get("company_name") for v in vacs]},
        {"label": "Город", "vals": [v.get("area_name") for v in vacs]},
        {"label": "Match score", "vals": [v.get("score") for v in vacs]},
        {"label": "Предсказание приглашения, %", "vals": [v.get("predict") for v in vacs]},
        {"label": "ЗП, ₽", "vals": [fmt_money(v.get("salary_rub")) for v in vacs]},
        {"label": "Формат", "vals": ["удалёнка" if (v.get("is_remote") or v.get("is_remote_text")) else "офис" for v in vacs]},
        {"label": "Уровень", "vals": [v.get("level") or "—" for v in vacs]},
        {"label": "Опыт (HH)", "vals": [v.get("work_experience") or "—" for v in vacs]},
        {"label": "Стек (распознанный)", "html": True, "vals": [
            ", ".join(v.get("parsed_stack") or []) or "—" for v in vacs
        ]},
        {"label": "Откликов (мои / всего)", "vals": [
            f'{v.get("responses_count") or "—"} / {v.get("total_responses_count") or "—"}' for v in vacs
        ]},
        {"label": "Сейчас смотрят", "vals": [v.get("online_users_count") or "—" for v in vacs]},
        {"label": "Вежливость работодателя", "vals": [
            (f'{v["politeness"]["read_topic_percent"]}% за {v["politeness"]["reply_working_days"]} раб.дн.' if v.get("politeness") else "—")
            for v in vacs
        ]},
        {"label": "Мой статус", "vals": [STATUS_LABELS.get(v.get("status"), v.get("status")) for v in vacs]},
        {"label": "Состояние отклика", "vals": [v.get("neg_label") if v.get("neg_state") else "—" for v in vacs]},
    ]
    return render("compare.html", request=request, rows=vacs, fields=fields)


@app.get("/funnel", response_class=HTMLResponse)
async def funnel_page(request: Request):
    db = await get_db()
    try:
        await funnel_repo.backfill_employer_names(db)
        c = await negotiations_repo.counters(db)
        top = await funnel_repo.top_employers(db, limit=20)
        weeks = await funnel_repo.by_week(db)
        avg_h = await funnel_repo.avg_hr_response_hours(db)
    finally:
        await db.close()
    total = c["total"] or 1
    cards = [
        {"label": "Всего", "value": c["total"], "color": "", "pct": None},
        {"label": "Ждут", "value": c["waiting"], "color": "text-amber-700", "pct": round(c["waiting"] / total * 100)},
        {"label": "HR смотрел", "value": c["viewed"], "color": "text-blue-700", "pct": round(c["viewed"] / total * 100)},
        {"label": "Собес/Приглашения", "value": c["invited"], "color": "text-violet-700", "pct": round(c["invited"] / total * 100)},
        {"label": "Отказов", "value": c["rejected"], "color": "text-red-700", "pct": round(c["rejected"] / total * 100)},
        {"label": "Архив", "value": c["archived"], "color": "text-neutral-500", "pct": round(c["archived"] / total * 100)},
    ]
    return render(
        "funnel.html", request=request, cards=cards,
        top_employers=top, weeks=weeks, avg_hr_response_hours=avg_h,
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    status: str | None = Query(None),
    path: str | None = Query(None),
    only_errors: bool = Query(False),
    limit: int = Query(200),
):
    rows = await logs_repo.list_logs(limit=limit, status_filter=status, path_filter=path, only_errors=only_errors)
    st = await logs_repo.stats()
    return render(
        "logs.html", request=request, rows=rows, stats=st,
        cur_status=status, cur_path=path, cur_only_errors=only_errors, cur_limit=limit,
    )


@app.get("/api/logs")
async def logs_api(
    status: str | None = Query(None),
    path: str | None = Query(None),
    only_errors: bool = Query(False),
    limit: int = Query(200),
):
    return {
        "rows": await logs_repo.list_logs(limit=limit, status_filter=status, path_filter=path, only_errors=only_errors),
        "stats": await logs_repo.stats(),
    }


@app.post("/api/logs/cleanup")
async def logs_cleanup(keep: int = Form(5000)):
    deleted = await logs_repo.cleanup(keep=keep)
    return {"ok": True, "deleted": deleted, "kept": keep}


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    db = await get_db()
    try:
        p = await profile_repo.get_profile(db) or {"skills": [], "formats": []}
    finally:
        await db.close()
    return render("profile.html", request=request, p=p)


@app.post("/api/profile", response_class=HTMLResponse)
async def update_profile(
    title: str = Form(""),
    years_experience: float | None = Form(None),
    salary_expected_from: int | None = Form(None),
    salary_currency: str = Form("RUR"),
    skills_csv: str = Form(""),
    formats_csv: str = Form(""),
):
    skills = [s.strip() for s in skills_csv.split(",") if s.strip()]
    formats = [s.strip().upper() for s in formats_csv.split(",") if s.strip()]
    db = await get_db()
    try:
        await profile_repo.update_manual(db, {
            "title": title or None,
            "years_experience": years_experience,
            "salary_expected_from": salary_expected_from,
            "salary_currency": salary_currency,
            "skills": skills,
            "formats": formats,
        })
    finally:
        await db.close()
    return HTMLResponse("сохранено")
