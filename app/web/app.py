import asyncio
import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import scheduler as scheduler_mod
from app import tasks as task_mod
from app.clients import cbr as cbr_client
from app.clients.cookies import load_jar, save_jar
from app.clients.hh import AntibotChallengeError, HHClient, SessionExpiredError
from app.collector import favorites as fav_collector
from app.collector import personal as personal_collector
from app.collector import vacancies as collector
from app.config import settings
from app.db import (
    employers_repo,
    funnel_repo,
    logs_repo,
    negotiations_repo,
    profile_repo,
    searches_repo,
    vacancies_repo,
)
from app.db.db import get_db, init_db
from app.parsers.state import extract_initial_state
from app.scoring import ml as ml_module
from app.scoring.match import score_vacancy
from app.scoring.predict import predict_invite_prob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx логирует каждый запрос на INFO — это и спам (Telegram long-poll каждые 30с,
# Ollama-вызовы), и УТЕЧКА токена бота в логи (URL содержит bot<token>). Глушим до WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

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


def _localdt(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    from app.timeutil import to_local

    return to_local(value, fmt)


templates.env.filters["localdt"] = _localdt


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
    # Помечаем осиротевшие 'running' джобы (процесс упал/перезапущен посреди прогона) как interrupted
    from app.db import job_runs_repo

    n_orphans = await job_runs_repo.mark_running_interrupted()
    if n_orphans:
        logging.info("job_runs: помечено interrupted осиротевших running: %s", n_orphans)
    scheduler_mod.start(hh_client)
    telegram_task = None
    if settings.TELEGRAM_BOT_TOKEN:
        from app import notify

        telegram_task = asyncio.create_task(notify.poll_updates_loop())
    try:
        yield
    finally:
        if telegram_task:
            telegram_task.cancel()
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
    "new": "Новое",
    "viewed": "Просмотрел",
    "applied": "Откликнулся",
    "interview": "Собес",
    "rejected": "Отказ",
    "offer": "Оффер",
    "skipped": "Скип",
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
    None: "—",
    "RESPONSE": "ждёт",
    "INVITATION": "приглашение",
    "INTERVIEW": "собес",
    "DISCARD": "отказ",
    "DISCARD_NO_INTERACTION": "отказ (без интер.)",
    "DISCARD_BY_APPLICANT": "отозвал",
    "HIRED": "оффер",
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


def _filters_from_query(
    statuses,
    only_remote,
    text,
    stack,
    level,
    salary_rub_min,
    sort_by=None,
    sort_dir="desc",
    statuses_exclude=None,
    neg_states=None,
    neg_states_exclude=None,
    show_disappeared="hide",
    show_archived="hide",
    only_office=False,
    name_contains=None,
    company_contains=None,
):
    return {
        "statuses": statuses or None,
        "statuses_exclude": statuses_exclude or None,
        "neg_states": neg_states or None,
        "neg_states_exclude": neg_states_exclude or None,
        "only_remote": only_remote,
        "only_office": only_office,
        "text": text or None,
        "name_contains": name_contains or None,
        "company_contains": company_contains or None,
        "stack_any": stack or None,
        "level": level or None,
        "salary_rub_min": salary_rub_min,
        "sort_by": sort_by or None,
        "sort_dir": sort_dir or "desc",
        "show_disappeared": show_disappeared,
        "show_archived": show_archived,
    }


PY_SORT_KEYS = {"score", "predict", "status", "format", "stack", "source", "neg"}


# Маппинг виртуальных полей сортировки на функции-extractor.
# Для каждого ключа функция получает row (обогащённый _enrich_with_scoring)
# и возвращает значение для сортировки. None означает «пусто в конец» (через _PY_SORT_KEY).
_PY_SORT_EXTRACTORS = {
    "score": lambda r: r.get("score"),
    "predict": lambda r: r.get("predict"),
    "status": lambda r: r.get("status") or "new",
    # удалёнка > гибрид > офис: 3/2/1, остальные 0
    "format": lambda r: (
        3
        if (r.get("is_remote") or r.get("is_remote_text"))
        else (
            2
            if "HYBRID" in [str(x).upper() for x in (r.get("work_formats") or [])]
            else (
                1
                if any(str(x).upper() in ("ON_SITE", "OFFICE") for x in (r.get("work_formats") or []))
                else 0
            )
        )
    ),
    "stack": lambda r: len(r.get("parsed_stack") or []),
    # ✨ рекомендации сверху, потом backfill/обычные поиски
    "source": lambda r: (
        2
        if any(str(s).startswith("✨") for s in (r.get("source_list") or []))
        else (1 if r.get("source_list") else 0)
    ),
    "neg": lambda r: r.get("neg_state") or "",
}


def _parse_sort(sort: str | None, dir_legacy: str = "desc") -> list[tuple[str, str]]:
    """Парсит sort-параметр: 'score,-name,+salary_rub' → [('score','desc'), ('name','desc'), ('salary_rub','asc')].
    Префикс '-' = DESC, '+' = ASC, без префикса — берём dir_legacy (по умолчанию desc).
    Возвращает [] если sort пустой."""
    if not sort:
        return []
    out: list[tuple[str, str]] = []
    for raw in str(sort).split(","):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("-"):
            out.append((raw[1:], "desc"))
        elif raw.startswith("+"):
            out.append((raw[1:], "asc"))
        else:
            out.append((raw, dir_legacy or "desc"))
    return out


def _apply_py_sort(rows: list[dict], parsed: list[tuple[str, str]]) -> None:
    """Multi-sort in-place. Применяем поля reversed (stable sort) — последнее становится primary key.
    Для виртуальных PY-полей используем _PY_SORT_EXTRACTORS, иначе берём r.get(field) напрямую.
    Ключ (is_none, value) — None всегда «больше» (в конец при ASC), типы внутри поля консистентны."""
    for field, d in reversed(parsed):
        reverse = d.lower() == "desc"
        extractor = _PY_SORT_EXTRACTORS.get(field, lambda r, f=field: r.get(f))

        def _key(r, ex=extractor):
            v = ex(r)
            return (v is None, v)

        rows.sort(key=_key, reverse=reverse)


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    status: list[str] | None = Query(None),
    hide_status: list[str] | None = Query(None),
    neg: list[str] | None = Query(None),
    hide_neg: list[str] | None = Query(None),
    only_remote: bool = Query(False),  # legacy — поддерживаем старые ссылки
    format: str | None = Query(None),  # remote | office | all (новый, перекрывает only_remote)
    q: str | None = Query(None),
    name_q: str | None = Query(None),
    company_q: str | None = Query(None),
    stack: list[str] | None = Query(None),
    level: str | None = Query(None),
    salary_rub_min: int | None = Query(None),
    sort: str | None = Query(None),
    dir: str = Query("desc"),
    disappeared: str = Query("hide"),  # hide | only | all
    archived: str = Query("hide"),  # hide | only | all
):
    # «Скип» всегда скрыт автоматически — кроме режима архива (когда пользователь явно показывает только skipped).
    archive_mode = bool(status and "skipped" in status)
    if not archive_mode:
        hide_status = list(hide_status or [])
        if "skipped" not in hide_status:
            hide_status.append("skipped")

    # format перекрывает legacy only_remote, иначе fallback
    fmt = (format or "").lower()
    if fmt == "remote":
        only_remote, only_office = True, False
    elif fmt == "office":
        only_remote, only_office = False, True
    else:
        only_office = False
        # only_remote приходит из legacy — оставляем как есть

    db = await get_db()
    try:
        counts = await vacancies_repo.count_vacancies(db)
        # Multi-sort: если хоть одно поле — PY (score/predict) → весь sort в Python.
        # Иначе передаём raw-строку (CSV) в SQL.
        parsed_sort = _parse_sort(sort, dir)
        has_py = any(f in PY_SORT_KEYS for f, _ in parsed_sort)
        sql_sort = None if has_py else sort
        filters = _filters_from_query(
            status,
            only_remote,
            q,
            stack,
            level,
            salary_rub_min,
            sql_sort,
            dir,
            hide_status,
            neg,
            hide_neg,
            disappeared,
            archived,
            only_office,
            name_q,
            company_q,
        )
        rows = await vacancies_repo.list_vacancies(db, **filters, limit=400)
        rows = await _enrich_with_scoring(db, rows)
        if has_py and parsed_sort:
            _apply_py_sort(rows, parsed_sort)
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
        "index.html",
        request=request,
        status=hh_client.status,
        counts=counts,
        funnel=funnel,
        profile=profile,
        rows=rows,
        searches=searches,
        disappeared_count=disappeared_count,
        archived_count=archived_count,
        statuses=STATUSES,
        status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS,
        applied={
            "status": status or [],
            "hide_status": hide_status or [],
            "neg": neg or [],
            "hide_neg": hide_neg or [],
            "only_remote": only_remote,
            "format": "remote" if only_remote else ("office" if only_office else "all"),
            "q": q or "",
            "name_q": name_q or "",
            "company_q": company_q or "",
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
                hh_client,
                db,
                limit=limit,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(
                    current, total, message
                ),
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


@app.post("/api/backfill-descriptions")
async def backfill_descriptions(limit: int = Form(200)):
    """Массово дотягивает описания вакансий без полного текста (для RAG-индексации)."""

    async def job(ctx):
        db = await get_db()
        try:
            res = await collector.backfill_descriptions(
                hh_client,
                db,
                limit=limit,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(
                    current, total, message
                ),
            )
            await save_jar(db, hh_client.client)
            return res
        finally:
            await db.close()

    try:
        t = await task_mod.run("backfill_descriptions", "Дотянуть описания", job)
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
        "id",
        "name",
        "company_name",
        "area_name",
        "url",
        "salary_rub",
        "salary_currency",
        "is_remote",
        "level",
        "score",
        "predict",
        "status",
        "neg_label",
        "responses_count",
        "total_responses_count",
        "online_users_count",
        "parsed_stack",
        "updated_at",
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
        "ok": True,
        "hhid": state.get("hhid"),
        "name": f"{account.get('firstName', '')} {account.get('lastName', '')}".strip(),
        "email": account.get("email"),
        "pro": state.get("stateHhPro"),
        "resumes_total": info.get("total"),
        "resumes_finished": info.get("finished"),
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
                hh_client,
                db,
                params,
                max_pages=max_pages,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(
                    current, total, message
                ),
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
                hh_client,
                db,
                max_pages=max_pages,
                full=full,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(
                    current, total, message
                ),
            )
            resume_res = None
            if import_resume:
                ctx.update(message="импорт резюме…")
                try:
                    resume_res = await personal_collector.collect_resume(
                        hh_client, db, neg_res.get("resume_id")
                    )
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
    """SSE-стрим задач. Сам завершается через 60 сек — EventSource переподключится автоматом.
    Это убирает зависания uvicorn --reload (без таймаута SSE мешали graceful shutdown)."""

    async def gen():
        import time as _time

        deadline = _time.monotonic() + 60.0
        try:
            sub = task_mod.subscribe()
            async for chunk in sub:
                yield chunk
                if _time.monotonic() >= deadline:
                    break
        except asyncio.CancelledError:
            return

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


@app.post("/api/searches/recommendations")
async def searches_create_recommendations():
    """Подключает saved_search «✨ Рекомендации» по resume_id из профиля.
    Идемпотентно: если такой уже есть — возвращает его id."""
    db = await get_db()
    try:
        prof = await profile_repo.get_profile(db)
        resume_id = (prof or {}).get("resume_id")
        if not resume_id:
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "no_resume_id",
                    "message": "В профиле нет resume_id. Запусти «Полный sync откликов».",
                },
                status_code=400,
            )
        existing = await searches_repo.list_searches(db)
        for s in existing:
            if (s.get("params") or {}).get("resume") == resume_id:
                return {"ok": True, "id": s["id"], "existed": True}
        # max_pages=200 ≈ «до конца» — HH режет ~100 страниц.
        # early-stop K=5 сэкономит трафик при повторных синках.
        params = {
            "resume": resume_id,
            "items_on_page": 20,
            "order_by": "relevance",
            "max_pages": 200,
            "early_stop_seen": 5,
        }
        sid = await searches_repo.create_search(db, "✨ Рекомендации", params)
        return {"ok": True, "id": sid, "existed": False}
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


async def _bulk_set_param(
    field: str,
    value: int,
    *,
    only_active: bool,
    only_with_resume: bool,
) -> dict:
    """Общая логика bulk-апдейта одного поля params для saved_searches."""
    import json as _json

    db = await get_db()
    try:
        searches = await searches_repo.list_searches(db)
        updated = 0
        for s in searches:
            if only_active and not s.get("is_active"):
                continue
            params = dict(s.get("params") or {})
            if only_with_resume and not params.get("resume"):
                continue
            if params.get(field) == value:
                continue
            params[field] = value
            await searches_repo.update_search(
                db,
                s["id"],
                params=_json.dumps(params, ensure_ascii=False),
            )
            updated += 1
        return {"ok": True, "updated": updated, field: value}
    finally:
        await db.close()


@app.post("/api/searches/bulk-max-pages")
async def searches_bulk_set_max_pages(
    max_pages: int = Form(...),
    only_active: bool = Form(True),
    only_with_resume: bool = Form(False),
):
    """Массовое обновление глубины. only_with_resume=True → только рекомендации."""
    if max_pages < 1 or max_pages > 1000:
        return JSONResponse({"ok": False, "reason": "out_of_range"}, status_code=400)
    return await _bulk_set_param(
        "max_pages",
        max_pages,
        only_active=only_active,
        only_with_resume=only_with_resume,
    )


@app.post("/api/searches/bulk-early-stop")
async def searches_bulk_set_early_stop(
    early_stop_seen: int = Form(...),
    only_active: bool = Form(True),
    only_with_resume: bool = Form(False),
):
    """Массовое обновление early_stop_seen (K подряд seen → stop).
    0 = отключить early-stop у выбранных. Типичные: 3 (обычные) / 5 (рекомендации)."""
    if early_stop_seen < 0 or early_stop_seen > 100:
        return JSONResponse({"ok": False, "reason": "out_of_range"}, status_code=400)
    return await _bulk_set_param(
        "early_stop_seen",
        early_stop_seen,
        only_active=only_active,
        only_with_resume=only_with_resume,
    )


@app.post("/api/searches/{sid}/max-pages")
async def searches_set_max_pages(sid: int, max_pages: int = Form(...)):
    """Меняет глубину сохранённого поиска (params.max_pages)."""
    if max_pages < 1 or max_pages > 1000:
        return JSONResponse({"ok": False, "reason": "out_of_range"}, status_code=400)
    db = await get_db()
    try:
        s = await searches_repo.get(db, sid)
        if not s:
            return JSONResponse({"ok": False, "reason": "not_found"}, status_code=404)
        params = dict(s["params"])
        params["max_pages"] = max_pages
        import json as _json

        await searches_repo.update_search(db, sid, params=_json.dumps(params, ensure_ascii=False))
        return {"ok": True, "max_pages": max_pages}
    finally:
        await db.close()


@app.post("/api/searches/{sid}/update")
async def searches_update(
    sid: int,
    name: str | None = Form(None),
    text: str | None = Form(None),
    area: str | None = Form(None),
    remote: bool | None = Form(None),
    max_pages: int | None = Form(None),
    is_active: bool | None = Form(None),
):
    """Inline-редактирование сохранённого поиска. Params мержатся с существующими
    (не теряем resume/early_stop_seen и прочие ключи)."""
    db = await get_db()
    try:
        s = await searches_repo.get(db, sid)
        if not s:
            return JSONResponse({"ok": False, "reason": "not_found"}, status_code=404)
        params = dict(s["params"])
        if text is not None:
            params["text"] = text
        if area is not None:
            if area.strip():
                params["area"] = area.strip()
            else:
                params.pop("area", None)
        if remote is not None:
            if remote:
                params["schedule"] = "remote"
            else:
                params.pop("schedule", None)
        if max_pages is not None:
            params["max_pages"] = max(1, min(1000, max_pages))
        fields: dict = {"params": params}
        if name is not None and name.strip():
            fields["name"] = name.strip()
        if is_active is not None:
            fields["is_active"] = int(is_active)
        await searches_repo.update_search(db, sid, **fields)
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
async def searches_run_one(sid: int, full: bool = Form(False)):
    """full=True → отключает early-stop, проходим до конца. Полезно после долгой паузы
    или если порядок выдачи HH сильно сдвинулся."""

    async def job(ctx):
        db = await get_db()
        try:
            s = await searches_repo.get(db, sid)
            if not s:
                raise RuntimeError(f"search {sid} not found")
            params = dict(s["params"])
            # рекомендации: перед запросом обновим resume-хеш (он у HH меняется)
            if params.get("resume"):
                try:
                    ctx.update(message="обновляю resume-токен…")
                    from app.collector import personal as personal_col

                    new_token = await personal_col.refresh_resume_search_token(db=db, client=hh_client)
                    if new_token and new_token != params["resume"]:
                        import json as _json

                        params["resume"] = new_token
                        sp = dict(s["params"])
                        sp["resume"] = new_token
                        await searches_repo.update_search(db, sid, params=_json.dumps(sp, ensure_ascii=False))
                except Exception as e:
                    log.warning("searches_run_one: refresh resume token failed: %s", e)
            max_pages = int(params.pop("max_pages", 5))
            es_k = 0 if full else int(params.pop("early_stop_seen", 5 if params.get("resume") else 3))
            params.pop("early_stop_seen", None)  # в любом случае не передавать в HH
            mode = " [full]" if full else ""
            ctx.update(message=f"«{s['name']}»{mode}…")
            res = await collector.collect_search(
                hh_client,
                db,
                params,
                max_pages=max_pages,
                search_id=sid,
                progress_cb=lambda current=None, total=None, message=None: ctx.update(
                    current, total, message
                ),
                early_stop_consecutive_seen=es_k,
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
async def searches_sync_all(full: bool = Form(False)):
    """full=True → отключает early-stop у всех поисков (тяжёлый прогон по всему корпусу)."""

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
                es_k = 0 if full else int(params.pop("early_stop_seen", 5 if params.get("resume") else 3))
                params.pop("early_stop_seen", None)
                mode = " [full]" if full else ""
                ctx.update(
                    current=i, total=len(searches), message=f"{i}/{len(searches)}: «{s['name']}»{mode}"
                )
                try:
                    r = await collector.collect_search(
                        hh_client,
                        db,
                        params,
                        max_pages=max_pages,
                        search_id=s["id"],
                        early_stop_consecutive_seen=es_k,
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
                        ctx.update(
                            current=i, total=len(searches), message=f"{i}/{len(searches)}: «{s['name']}»"
                        )
                        try:
                            r = await collector.collect_search(
                                hh_client, inner_db, params, max_pages=max_pages, search_id=s["id"]
                            )
                            results.append({"id": s["id"], "name": s["name"], **r})
                        except (SessionExpiredError, AntibotChallengeError) as e:
                            results.append({"id": s["id"], "error": str(e)})
                            break
                    await save_jar(inner_db, hh_client.client)
                    return {"ran": len(results), "results": results}
                finally:
                    await inner_db.close()

            await task_mod.run(
                "sync_searches", "Пересборка сохранённых поисков", job, if_running="cancel_previous"
            )
        except Exception as e:
            return {"ok": True, "deleted": len(ids), "resync_started": False, "error": str(e)}

    return {"ok": True, "deleted": len(ids), "resync_started": bool(also_resync)}


@app.post("/api/scheduler/{job_id}/run-now")
async def scheduler_run_now(job_id: str):
    return await scheduler_mod.run_now(job_id)


@app.post("/api/jobs/{run_id}/stop")
async def stop_job_run(run_id: int):
    """Останавливает выполняющийся прогон джоба по job_runs.id."""
    return await scheduler_mod.cancel_run(run_id)


# ---------- RAG (опционально, extra `rag`) ----------


@app.get("/api/rag/coverage")
async def rag_coverage():
    """Покрытие индексации для live-обновления на /search."""
    db = await get_db()
    try:
        embedded = total = 0
        from app.llm import rag as rag_mod

        if rag_mod.is_available():
            from app.db import embeddings_repo

            embedded, total = await embeddings_repo.coverage(db)
        cur = await db.execute(
            "SELECT COUNT(*) FROM vacancies "
            "WHERE (description IS NULL OR length(description) <= 100) "
            "AND disappeared_at IS NULL AND archived_at IS NULL"
        )
        desc_missing = (await cur.fetchone())[0]
        return {"embedded": embedded, "total": total, "desc_missing": desc_missing}
    finally:
        await db.close()


@app.post("/api/rag/search")
async def rag_search(q: str = Form(...), k: int = Form(5)):
    """Семантический поиск по корпусу вакансий (retrieval)."""
    from app.llm import rag as rag_mod

    if not rag_mod.is_available():
        return {"ok": False, "reason": "rag_disabled"}
    if not q.strip():
        return {"ok": True, "results": []}
    db = await get_db()
    try:
        results = []
        for vid, score in await rag_mod.semantic_search(db, q, k):
            v = await vacancies_repo.get_vacancy(db, vid)
            if v:
                results.append(
                    {
                        "id": vid,
                        "name": v.get("name"),
                        "company": v.get("company_name"),
                        "score": round(score, 3),
                        "url": f"/vacancy/{vid}",
                    }
                )
        return {"ok": True, "results": results}
    finally:
        await db.close()


@app.post("/api/rag/ask")
async def rag_ask(q: str = Form(...), k: int = Form(5)):
    """Полный RAG: находит релевантные вакансии и отвечает на вопрос с ссылками на них."""
    from app.llm import rag as rag_mod

    if not rag_mod.is_available():
        return {"ok": False, "reason": "rag_disabled"}
    if not q.strip():
        return {"ok": False, "reason": "empty_query"}
    db = await get_db()
    try:
        return await rag_mod.ask(db, q, k)
    finally:
        await db.close()


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


@app.get("/api/status/stream")
async def status_stream():
    """SSE-стрим client+scheduler. Шлёт снапшот каждые 10 сек.
    Сам завершается через 60 сек — EventSource в браузере переподключается автоматом.
    Это убирает зависания при uvicorn --reload (долгоживущие SSE мешают graceful shutdown)."""

    async def gen():
        import time as _time

        deadline = _time.monotonic() + 60.0
        try:
            while _time.monotonic() < deadline:
                payload = {
                    "client": hh_client.status,
                    "scheduler": scheduler_mod.status(),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            return

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/vacancies", response_class=HTMLResponse)
async def vacancies_fragment(
    request: Request,
    status: list[str] | None = Query(None),
    hide_status: list[str] | None = Query(None),
    neg: list[str] | None = Query(None),
    hide_neg: list[str] | None = Query(None),
    only_remote: bool = Query(False),
    format: str | None = Query(None),
    q: str | None = Query(None),
    name_q: str | None = Query(None),
    company_q: str | None = Query(None),
    stack: list[str] | None = Query(None),
    level: str | None = Query(None),
    salary_rub_min: int | None = Query(None),
    sort: str | None = Query(None),
    dir: str = Query("desc"),
    disappeared: str = Query("hide"),
    archived: str = Query("hide"),
):
    fmt = (format or "").lower()
    if fmt == "remote":
        only_remote, only_office = True, False
    elif fmt == "office":
        only_remote, only_office = False, True
    else:
        only_office = False

    db = await get_db()
    try:
        parsed_sort = _parse_sort(sort, dir)
        has_py = any(f in PY_SORT_KEYS for f, _ in parsed_sort)
        sql_sort = None if has_py else sort
        filters = _filters_from_query(
            status,
            only_remote,
            q,
            stack,
            level,
            salary_rub_min,
            sql_sort,
            dir,
            hide_status,
            neg,
            hide_neg,
            disappeared,
            archived,
            only_office,
            name_q,
            company_q,
        )
        rows = await vacancies_repo.list_vacancies(db, **filters, limit=500)
        rows = await _enrich_with_scoring(db, rows)
        if has_py and parsed_sort:
            _apply_py_sort(rows, parsed_sort)
    finally:
        await db.close()
    return render(
        "_table.html",
        request=request,
        rows=rows,
        status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS,
        statuses=STATUSES,
    )


@app.post("/api/vacancies/bulk-status")
async def vacancies_bulk_status(
    ids: list[int] = Form(...),
    status: str = Form(...),
):
    """Массовая смена локального статуса выбранных вакансий (галочки в таблице → toolbar)."""
    if status not in STATUSES:
        return JSONResponse({"ok": False, "reason": f"unknown status: {status}"}, status_code=400)
    if not ids:
        return JSONResponse({"ok": False, "reason": "empty_ids"}, status_code=400)
    db = await get_db()
    try:
        updated = 0
        for vid in ids:
            try:
                await vacancies_repo.set_status(db, vid, status)
                updated += 1
            except Exception as e:
                log.warning("bulk_status vid=%s failed: %s", vid, e)
        return {"ok": True, "updated": updated, "status": status}
    finally:
        await db.close()


@app.post("/api/vacancy/{vid}/status", response_class=HTMLResponse)
async def set_vacancy_status(
    request: Request, vid: int, status: str = Form(...), note: str | None = Form(None)
):
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
        "_row.html",
        request=request,
        v=rows[0],
        status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS,
        statuses=STATUSES,
    )


@app.get("/vacancy/{vid}", response_class=HTMLResponse)
async def vacancy_detail(request: Request, vid: int):
    db = await get_db()
    try:
        v = await vacancies_repo.get_vacancy(db, vid)
        v_enriched = (await _enrich_with_scoring(db, [v]))[0] if v else None
        reqs = []
        last_run = None
        analyses: dict = {}
        analyzers_list: list = []
        enabled_kinds: list[str] = []
        similar_vacancies: list = []
        soft_score: int | None = None
        if v_enriched:
            from app.db import llm_repo
            from app.llm import rag as rag_mod
            from app.llm.registry import ANALYZERS, get_enabled_analyzers

            if rag_mod.is_available():
                try:
                    for svid, score in await rag_mod.similar(db, vid):
                        sv = await vacancies_repo.get_vacancy(db, svid)
                        if sv:
                            similar_vacancies.append(
                                {
                                    "id": svid,
                                    "name": sv.get("name"),
                                    "company": sv.get("company_name"),
                                    "score": score,
                                }
                            )
                except Exception as e:
                    log.warning("similar vacancies failed vid=%s: %s", vid, e)

            reqs = await llm_repo.get_requirements(db, vid)
            runs = await llm_repo.list_runs(
                db, target_kind="vacancy", target_id=str(vid), task_kind="requirements", limit=1
            )
            last_run = runs[0] if runs else None
            analyses = await llm_repo.get_all_analysis(db, vid)
            if analyses.get("soft_skills_employer"):
                from app.scoring.match import employer_soft_score

                soft_score = employer_soft_score(analyses["soft_skills_employer"].get("data"))
            enabled_kinds = await get_enabled_analyzers(db)
            analyzers_list = [
                {
                    "kind": a.kind,
                    "label": a.label,
                    "description": a.description,
                    "enabled": a.kind in enabled_kinds,
                }
                for a in ANALYZERS.values()
            ]
    finally:
        await db.close()
    if not v_enriched:
        raise HTTPException(404)
    return render(
        "vacancy.html",
        request=request,
        v=v_enriched,
        statuses=STATUSES,
        status_labels=STATUS_LABELS,
        status_colors=STATUS_COLORS,
        requirements=reqs,
        last_llm_run=last_run,
        analyzers=analyzers_list,
        analyses=analyses,
        similar_vacancies=similar_vacancies,
        soft_score=soft_score,
    )


@app.post("/api/vacancy/{vid}/llm-parse")
async def vacancy_llm_parse(vid: int, model: str | None = Form(None)):
    """Прогнать LLM-разбор требований для одной вакансии (синхронно)."""
    from app.llm.tasks.requirements import parse_one

    db = await get_db()
    try:
        res = await parse_one(db, vid, model=model)
    finally:
        await db.close()
    return res


@app.get("/api/llm/analyzers")
async def llm_analyzers_list():
    """Список всех зарегистрированных анализаторов + какие включены сейчас."""
    from app.llm.registry import ANALYZERS, get_enabled_analyzers

    db = await get_db()
    try:
        enabled = await get_enabled_analyzers(db)
    finally:
        await db.close()
    return {
        "analyzers": [
            {
                "kind": a.kind,
                "label": a.label,
                "description": a.description,
                "default_enabled": a.default_enabled,
                "enabled": a.kind in enabled,
            }
            for a in ANALYZERS.values()
        ],
        "enabled": enabled,
    }


@app.post("/api/llm/analyzers/enabled")
async def llm_analyzers_set_enabled(kinds: list[str] = Form(default=[])):
    """Сохраняет глобальный набор включённых анализаторов (для cron и UI-дефолтов)."""
    from app.llm.registry import get_enabled_analyzers, set_enabled_analyzers

    db = await get_db()
    try:
        await set_enabled_analyzers(db, kinds)
        return {"ok": True, "enabled": await get_enabled_analyzers(db)}
    finally:
        await db.close()


@app.post("/api/vacancy/{vid}/analyze")
async def vacancy_analyze(
    vid: int,
    kinds: list[str] = Form(default=[]),
    model: str | None = Form(None),
):
    """Запустить выбранные анализаторы по одной вакансии. Если kinds пустой — берём enabled."""
    from app.llm.registry import analyze_one, get_enabled_analyzers

    db = await get_db()
    try:
        if not kinds:
            kinds = await get_enabled_analyzers(db)
        results = await analyze_one(db, vid, kinds, model=model)
    finally:
        await db.close()
    return {
        "vacancy_id": vid,
        "results": [
            {
                "kind": r.kind,
                "ok": r.ok,
                "model": r.model,
                "latency_ms": r.latency_ms,
                "llm_run_id": r.llm_run_id,
                "error": r.error,
                "data": r.data,
            }
            for r in results
        ],
    }


@app.post("/api/vacancy/{vid}/llm-parse-multi")
async def vacancy_llm_parse_multi(vid: int, models: list[str] = Form(...)):
    """Прогнать на нескольких моделях подряд для сравнения. Сохраняет requirements
    из последнего успешного прогона."""
    from app.llm.tasks.requirements import parse_one_multi_model

    db = await get_db()
    try:
        res = await parse_one_multi_model(db, vid, models)
    finally:
        await db.close()
    return {"runs": res}


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    kind: str | None = Query(None),
    category: str | None = Query(None),
    top: int = Query(50),
):
    """Топ-N требований/категорий по корпусу. SQL по vacancy_requirements + vacancy_analysis."""
    db = await get_db()
    try:
        req_where = ["1=1"]
        req_args: list = []
        if kind:
            req_where.append("kind = ?")
            req_args.append(kind)
        if category:
            req_where.append("category = ?")
            req_args.append(category)
        req_args.append(top)
        cur = await db.execute(
            f"""
            SELECT text, kind, category, COUNT(DISTINCT vacancy_id) AS cnt
              FROM vacancy_requirements
             WHERE {" AND ".join(req_where)}
          GROUP BY LOWER(text), kind, category
          ORDER BY cnt DESC, text
             LIMIT ?
            """,
            req_args,
        )
        top_requirements = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT kind, COUNT(*) AS cnt FROM vacancy_requirements GROUP BY kind ORDER BY cnt DESC"
        )
        by_kind = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            "SELECT category, COUNT(*) AS cnt FROM vacancy_requirements "
            "WHERE category IS NOT NULL GROUP BY category ORDER BY cnt DESC"
        )
        by_category = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """
            SELECT json_extract(data_json, '$.kind') AS k, COUNT(*) AS cnt
              FROM vacancy_analysis
             WHERE kind = 'company_kind'
          GROUP BY json_extract(data_json, '$.kind')
          ORDER BY cnt DESC
            """,
        )
        company_kinds = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """
            SELECT text AS t, COUNT(DISTINCT vacancy_id) AS cnt
              FROM vacancy_requirements
             WHERE category = 'stack'
          GROUP BY text
          ORDER BY cnt DESC
             LIMIT 30
            """
        )
        top_stack = [dict(r) for r in await cur.fetchall()]

        # Топ-вопросов из interview_prep:
        # data_json.likely_questions — массив объектов {q, why}.
        # Группируем как есть (SQLite LOWER не работает с кириллицей).
        cur = await db.execute(
            """
            SELECT json_extract(value, '$.q') AS q,
                   COUNT(DISTINCT vacancy_id) AS cnt
              FROM vacancy_analysis,
                   json_each(json_extract(data_json, '$.likely_questions'))
             WHERE kind = 'interview_prep'
               AND json_extract(value, '$.q') IS NOT NULL
          GROUP BY json_extract(value, '$.q')
          ORDER BY cnt DESC, q
             LIMIT 200
            """
        )
        top_questions = [dict(r) for r in await cur.fetchall()]

        # Топ-тем (topics — плоский массив строк)
        cur = await db.execute(
            """
            SELECT value AS t, COUNT(DISTINCT vacancy_id) AS cnt
              FROM vacancy_analysis,
                   json_each(json_extract(data_json, '$.topics'))
             WHERE kind = 'interview_prep' AND value IS NOT NULL AND value != ''
          GROUP BY value
          ORDER BY cnt DESC
             LIMIT 200
            """
        )
        top_topics = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute("SELECT COUNT(*) FROM vacancy_analysis WHERE kind = 'interview_prep'")
        interview_prep_count = (await cur.fetchone())[0]

        cur = await db.execute("SELECT COUNT(DISTINCT vacancy_id) FROM vacancy_requirements")
        parsed_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM vacancies")
        total_vacancies = (await cur.fetchone())[0]

        # LLM-затраты: общая сводка, разбивка по моделям и task_kind
        cur = await db.execute(
            """
            SELECT COUNT(*)                       AS cnt,
                   COALESCE(SUM(prompt_tokens), 0)   AS prompt_tokens,
                   COALESCE(SUM(response_tokens), 0) AS response_tokens,
                   COALESCE(AVG(latency_ms), 0)      AS avg_latency_ms
              FROM llm_runs
            """
        )
        row = await cur.fetchone()
        llm_total = (
            dict(row)
            if row
            else {
                "cnt": 0,
                "prompt_tokens": 0,
                "response_tokens": 0,
                "avg_latency_ms": 0,
            }
        )

        cur = await db.execute(
            """
            SELECT model,
                   COUNT(*)                          AS cnt,
                   COALESCE(SUM(prompt_tokens), 0)   AS prompt_tokens,
                   COALESCE(SUM(response_tokens), 0) AS response_tokens,
                   COALESCE(AVG(latency_ms), 0)      AS avg_latency_ms
              FROM llm_runs
          GROUP BY model
          ORDER BY cnt DESC, model
            """
        )
        llm_by_model = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """
            SELECT task_kind,
                   COUNT(*)                                       AS cnt,
                   SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END)        AS ok_count,
                   SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END)        AS fail_count
              FROM llm_runs
          GROUP BY task_kind
          ORDER BY cnt DESC, task_kind
            """
        )
        llm_by_task = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    return render(
        "analytics.html",
        request=request,
        top_requirements=top_requirements,
        by_kind=by_kind,
        by_category=by_category,
        company_kinds=company_kinds,
        top_stack=top_stack,
        top_questions=top_questions,
        top_topics=top_topics,
        interview_prep_count=interview_prep_count,
        parsed_count=parsed_count,
        total_vacancies=total_vacancies,
        llm_total=llm_total,
        llm_by_model=llm_by_model,
        llm_by_task=llm_by_task,
        filters={"kind": kind or "", "category": category or "", "top": top},
    )


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    """Семантический поиск + RAG Q&A по корпусу вакансий."""
    from app.llm import rag as rag_mod

    available = rag_mod.is_available()
    embedded = total = 0
    db = await get_db()
    try:
        if available:
            from app.db import embeddings_repo

            embedded, total = await embeddings_repo.coverage(db)
        cur = await db.execute(
            "SELECT COUNT(*) FROM vacancies "
            "WHERE (description IS NULL OR length(description) <= 100) "
            "AND disappeared_at IS NULL AND archived_at IS NULL"
        )
        desc_missing = (await cur.fetchone())[0]
    finally:
        await db.close()
    return render(
        "search.html",
        request=request,
        rag_available=available,
        embedded=embedded,
        total=total,
        desc_missing=desc_missing,
    )


@app.get("/searches", response_class=HTMLResponse)
async def searches_page(request: Request):
    """Редактируемая таблица сохранённых поисков (inline-правка всех полей)."""
    db = await get_db()
    try:
        searches = await searches_repo.list_searches(db)
    finally:
        await db.close()
    return render("searches.html", request=request, searches=searches)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    job_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100),
):
    """История прогонов фоновых джобов (job_runs)."""
    from app.db import job_runs_repo

    runs = await job_runs_repo.list_runs(job_id=job_id, status=status_filter or None, limit=limit)
    # для красивого ярлыка джоба — реальные label'ы из scheduler
    labels = scheduler_mod._JOB_LABELS if hasattr(scheduler_mod, "_JOB_LABELS") else {}
    # уникальные job_id для селектора фильтра
    all_job_ids = sorted({r["job_id"] for r in await job_runs_repo.list_runs(limit=500)})
    return render(
        "jobs.html",
        request=request,
        runs=runs,
        labels=labels,
        all_job_ids=all_job_ids,
        filters={"job_id": job_id or "", "status": status_filter or "", "limit": limit},
    )


@app.get("/llm-logs", response_class=HTMLResponse)
async def llm_logs_page(
    request: Request,
    task_kind: str | None = Query(None),
    target_id: str | None = Query(None),
    run: int | None = Query(None),
    limit: int = Query(100),
):
    from app.db import llm_repo

    db = await get_db()
    try:
        runs = await llm_repo.list_runs(
            db,
            task_kind=task_kind,
            target_id=target_id,
            target_kind="vacancy" if target_id else None,
            limit=limit,
        )
        focused = None
        if run:
            focused = await llm_repo.get_run(db, run)
    finally:
        await db.close()
    return render(
        "llm_logs.html",
        request=request,
        runs=runs,
        focused=focused,
        filters={"task_kind": task_kind or "", "target_id": target_id or "", "run": run or ""},
    )


@app.get("/api/llm/runs")
async def llm_runs_list(
    task_kind: str | None = Query(None),
    target_id: str | None = Query(None),
    limit: int = Query(50),
):
    from app.db import llm_repo

    db = await get_db()
    try:
        runs = await llm_repo.list_runs(
            db,
            task_kind=task_kind,
            target_id=target_id,
            target_kind="vacancy" if target_id else None,
            limit=limit,
        )
        return {"runs": runs}
    finally:
        await db.close()


@app.post("/api/llm/parse-corpus")
async def llm_parse_corpus(
    limit: int = Form(20),
    model: str | None = Form(None),
    only_unparsed: bool = Form(True),
):
    """Прогнать LLM-разбор по корпусу. Запускается как task (видна в панели задач)."""

    async def job(ctx):
        from app.llm.tasks.requirements import parse_one

        db = await get_db()
        try:
            sql = """
              SELECT v.id FROM vacancies v
              {join}
              WHERE v.description IS NOT NULL AND length(v.description) > 100
              {where}
              ORDER BY v.id DESC
              LIMIT ?
            """
            if only_unparsed:
                sql = sql.format(
                    join="LEFT JOIN vacancy_requirements r ON r.vacancy_id = v.id",
                    where="AND r.id IS NULL",
                )
            else:
                sql = sql.format(join="", where="")
            cur = await db.execute(sql, (limit,))
            ids = [r[0] for r in await cur.fetchall()]
            total = len(ids)
            ctx.update(current=0, total=total, message=f"найдено {total}")
            ok = 0
            for i, vid in enumerate(ids, 1):
                try:
                    res = await parse_one(db, vid, model=model)
                    if res.get("ok"):
                        ok += 1
                except Exception as e:
                    log.warning("llm parse corpus: vid=%s failed: %s", vid, e)
                ctx.update(current=i, message=f"{i}/{total} (успешных: {ok})")
            return {"processed": total, "ok": ok, "model": model or settings.LLM_MODEL_REQUIREMENTS}
        finally:
            await db.close()

    try:
        t = await task_mod.run("llm_parse_corpus", "LLM: разбор корпуса", job)
        return _task_response(t)
    except task_mod.TaskAlreadyRunning as e:
        return JSONResponse({"ok": False, "reason": "already_running", "kind": e.kind}, status_code=409)


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
        {
            "label": "Должность",
            "html": True,
            "vals": [
                f'<a class="text-blue-600 hover:underline" href="/vacancy/{v["id"]}">{v["name"]}</a>'
                for v in vacs
            ],
        },
        {"label": "Компания", "vals": [v.get("company_name") for v in vacs]},
        {"label": "Город", "vals": [v.get("area_name") for v in vacs]},
        {"label": "Match score", "vals": [v.get("score") for v in vacs]},
        {"label": "Предсказание приглашения, %", "vals": [v.get("predict") for v in vacs]},
        {"label": "ЗП, ₽", "vals": [fmt_money(v.get("salary_rub")) for v in vacs]},
        {
            "label": "Формат",
            "vals": ["удалёнка" if (v.get("is_remote") or v.get("is_remote_text")) else "офис" for v in vacs],
        },
        {"label": "Уровень", "vals": [v.get("level") or "—" for v in vacs]},
        {"label": "Опыт (HH)", "vals": [v.get("work_experience") or "—" for v in vacs]},
        {
            "label": "Стек (распознанный)",
            "html": True,
            "vals": [", ".join(v.get("parsed_stack") or []) or "—" for v in vacs],
        },
        {
            "label": "Откликов (мои / всего)",
            "vals": [
                f"{v.get('responses_count') or '—'} / {v.get('total_responses_count') or '—'}" for v in vacs
            ],
        },
        {"label": "Сейчас смотрят", "vals": [v.get("online_users_count") or "—" for v in vacs]},
        {
            "label": "Вежливость работодателя",
            "vals": [
                (
                    f"{v['politeness']['read_topic_percent']}% за {v['politeness']['reply_working_days']} раб.дн."
                    if v.get("politeness")
                    else "—"
                )
                for v in vacs
            ],
        },
        {"label": "Мой статус", "vals": [STATUS_LABELS.get(v.get("status"), v.get("status")) for v in vacs]},
        {
            "label": "Состояние отклика",
            "vals": [v.get("neg_label") if v.get("neg_state") else "—" for v in vacs],
        },
    ]
    return render("compare.html", request=request, rows=vacs, fields=fields)


@app.get("/funnel", response_class=HTMLResponse)
async def funnel_page(
    request: Request,
    only: str | None = Query(None),  # discard | interview | waiting | None
    top: int = Query(50),
):
    db = await get_db()
    try:
        await funnel_repo.backfill_employer_names(db)
        c = await negotiations_repo.counters(db)
        top_list = await funnel_repo.top_employers(db, limit=max(1, min(top, 500)), only=only)
        soft_scores = await funnel_repo.soft_scores_by_employer(db)
        for e in top_list:
            e["soft_score"] = soft_scores.get(e.get("employer_id"))
        weeks = await funnel_repo.by_week(db)
        avg_h = await funnel_repo.avg_hr_response_hours(db)
    finally:
        await db.close()
    total = c["total"] or 1
    cards = [
        {"label": "Всего", "value": c["total"], "color": "", "pct": None},
        {
            "label": "Ждут",
            "value": c["waiting"],
            "color": "text-amber-700",
            "pct": round(c["waiting"] / total * 100),
        },
        {
            "label": "HR смотрел",
            "value": c["viewed"],
            "color": "text-blue-700",
            "pct": round(c["viewed"] / total * 100),
        },
        {
            "label": "Собес/Приглашения",
            "value": c["invited"],
            "color": "text-violet-700",
            "pct": round(c["invited"] / total * 100),
        },
        {
            "label": "Отказов",
            "value": c["rejected"],
            "color": "text-red-700",
            "pct": round(c["rejected"] / total * 100),
        },
        {
            "label": "Архив",
            "value": c["archived"],
            "color": "text-neutral-500",
            "pct": round(c["archived"] / total * 100),
        },
    ]
    return render(
        "funnel.html",
        request=request,
        cards=cards,
        top_employers=top_list,
        weeks=weeks,
        avg_hr_response_hours=avg_h,
        only_filter=only or "",
        top_limit=top,
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    status: str | None = Query(None),
    path: str | None = Query(None),
    only_errors: bool = Query(False),
    limit: int = Query(200),
):
    rows = await logs_repo.list_logs(
        limit=limit, status_filter=status, path_filter=path, only_errors=only_errors
    )
    st = await logs_repo.stats()
    return render(
        "logs.html",
        request=request,
        rows=rows,
        stats=st,
        cur_status=status,
        cur_path=path,
        cur_only_errors=only_errors,
        cur_limit=limit,
    )


@app.get("/api/logs")
async def logs_api(
    status: str | None = Query(None),
    path: str | None = Query(None),
    only_errors: bool = Query(False),
    limit: int = Query(200),
):
    return {
        "rows": await logs_repo.list_logs(
            limit=limit, status_filter=status, path_filter=path, only_errors=only_errors
        ),
        "stats": await logs_repo.stats(),
    }


@app.post("/api/logs/cleanup")
async def logs_cleanup(keep: int = Form(5000)):
    deleted = await logs_repo.cleanup(keep=keep)
    return {"ok": True, "deleted": deleted, "kept": keep}


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    from app import notify
    from app.llm import settings as llm_settings
    from app.llm.registry import ANALYZERS, get_enabled_analyzers

    db = await get_db()
    try:
        p = await profile_repo.get_profile(db) or {"skills": [], "formats": []}
        llm_model = await llm_settings.get_requirements_model(db)
        llm_model_fast = await llm_settings.get_fast_model(db)
        notifications_enabled = await notify.is_enabled(db)
        notifications_telegram = await notify.is_telegram_enabled(db)
        telegram_configured = notify.telegram_configured()
        match_threshold = await notify.get_match_threshold(db)
        digest_hour = await notify.get_digest_hour(db)
        notif_events = await notify.get_events(db)
        enabled = await get_enabled_analyzers(db)
        analyzers_list = [
            {
                "kind": a.kind,
                "label": a.label,
                "description": a.description,
                "enabled": a.kind in enabled,
                "fast": a.fast,
            }
            for a in ANALYZERS.values()
        ]
    finally:
        await db.close()
    return render(
        "profile.html",
        request=request,
        p=p,
        llm_model=llm_model,
        llm_model_fast=llm_model_fast,
        llm_default=settings.LLM_MODEL_REQUIREMENTS,
        llm_default_fast=settings.LLM_MODEL_FAST,
        notifications_enabled=notifications_enabled,
        notifications_telegram=notifications_telegram,
        telegram_configured=telegram_configured,
        match_threshold=match_threshold,
        digest_hour=digest_hour,
        notif_events=notif_events,
        notif_event_labels=notify.EVENT_LABELS,
        analyzers=analyzers_list,
    )


@app.post("/api/settings/llm-model")
async def settings_set_llm_model(model: str = Form(...)):
    from app.llm import settings as llm_settings

    db = await get_db()
    try:
        await llm_settings.set_requirements_model(db, model)
        return {"ok": True, "model": model}
    finally:
        await db.close()


@app.post("/api/settings/llm-model-fast")
async def settings_set_llm_model_fast(model: str = Form(...)):
    """Быстрая модель для лёгких задач (summary/salary/company_kind/soft_skills)."""
    from app.llm import settings as llm_settings

    db = await get_db()
    try:
        await llm_settings.set_fast_model(db, model)
        return {"ok": True, "model": model}
    finally:
        await db.close()


@app.post("/api/settings/notifications")
async def settings_set_notifications(
    enabled: bool | None = Form(None),
    telegram: bool | None = Form(None),
    threshold: int | None = Form(None),
    digest_hour: int | None = Form(None),
    events: list[str] = Form(default=[]),
    events_present: bool = Form(False),
):
    """Настройки уведомлений: каналы (macOS/Telegram), порог match-score и категории
    событий (вакансии/собесы/ошибки/завершение джоб). Передавать можно подмножество полей.
    events применяются только если events_present=true (чтобы пустой список = «выключить всё»)."""
    from app import notify

    db = await get_db()
    try:
        if enabled is not None:
            await notify.set_enabled(db, enabled)
        if telegram is not None:
            await notify.set_telegram_enabled(db, telegram)
        if threshold is not None:
            await notify.set_match_threshold(db, threshold)
        if digest_hour is not None:
            await notify.set_digest_hour(db, digest_hour)
        if events_present:
            await notify.set_events(db, events)
        return {
            "ok": True,
            "enabled": await notify.is_enabled(db),
            "telegram": await notify.is_telegram_enabled(db),
            "threshold": await notify.get_match_threshold(db),
            "digest_hour": await notify.get_digest_hour(db),
            "events": sorted(await notify.get_events(db)),
            "telegram_configured": notify.telegram_configured(),
        }
    finally:
        await db.close()


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
        await profile_repo.update_manual(
            db,
            {
                "title": title or None,
                "years_experience": years_experience,
                "salary_expected_from": salary_expected_from,
                "salary_currency": salary_currency,
                "skills": skills,
                "formats": formats,
            },
        )
    finally:
        await db.close()
    return HTMLResponse("сохранено")
