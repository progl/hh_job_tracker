"""Реестр фоновых задач для UI.

- Только одна задача каждого `kind` одновременно (повторный запрос → 409 или auto-cancel).
- Прогресс публикуется в SSE-стрим (`/api/tasks/stream`).
- Жизненный цикл: queued → running → done | error | cancelled.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Task:
    id: str
    kind: str
    label: str
    status: str = "queued"
    progress: int = 0
    current: int = 0
    total: int = 0
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    _async_task: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "progress": self.progress,
            "current": self.current,
            "total": self.total,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class TaskAlreadyRunning(Exception):
    def __init__(self, kind: str, task_id: str):
        super().__init__(f"task '{kind}' already running")
        self.kind = kind
        self.task_id = task_id


_tasks: dict[str, Task] = {}
_subs: set[asyncio.Queue] = set()
_MAX_HISTORY = 50


def _broadcast(task: Task) -> None:
    payload = task.to_dict()
    for q in list(_subs):
        try:
            q.put_nowait(payload)
        except Exception:
            pass


def list_tasks(include_finished: bool = True, limit: int = 30) -> list[dict]:
    items = sorted(_tasks.values(), key=lambda t: t.started_at or 0, reverse=True)
    if not include_finished:
        items = [t for t in items if t.status in ("queued", "running")]
    return [t.to_dict() for t in items[:limit]]


def _prune() -> None:
    if len(_tasks) <= _MAX_HISTORY:
        return
    finished = sorted(
        (t for t in _tasks.values() if t.status in ("done", "error", "cancelled")),
        key=lambda t: t.finished_at or 0,
    )
    while len(_tasks) > _MAX_HISTORY and finished:
        t = finished.pop(0)
        _tasks.pop(t.id, None)


def find_running(kind: str) -> Task | None:
    for t in _tasks.values():
        if t.kind == kind and t.status in ("queued", "running"):
            return t
    return None


async def cancel(kind: str) -> bool:
    t = find_running(kind)
    if not t:
        return False
    if t._async_task and not t._async_task.done():
        t._async_task.cancel()
    t.status = "cancelled"
    t.finished_at = time.time()
    t.message = "отменено"
    _broadcast(t)
    return True


async def run(
    kind: str,
    label: str,
    coro_factory: Callable[["ProgressCtx"], Awaitable[Any]],
    *,
    if_running: str = "reject",  # reject | cancel_previous
) -> Task:
    existing = find_running(kind)
    if existing:
        if if_running == "reject":
            raise TaskAlreadyRunning(kind, existing.id)
        elif if_running == "cancel_previous":
            await cancel(kind)

    t = Task(id=str(uuid.uuid4())[:8], kind=kind, label=label)
    _tasks[t.id] = t
    _prune()
    t.status = "running"
    t.started_at = time.time()
    _broadcast(t)

    ctx = ProgressCtx(t)

    async def _runner():
        try:
            res = await coro_factory(ctx)
            t.result = res
            t.status = "done"
            t.progress = 100
            # синхронизируем current с total чтобы UI не показывал «10/20» при done
            if t.total > 0:
                t.current = t.total
            if not t.message or t.message in ("запущено по требованию…",):
                t.message = "готово"
        except asyncio.CancelledError:
            t.status = "cancelled"
            t.message = "отменено"
            raise
        except Exception as e:
            t.status = "error"
            t.error = str(e)
            t.message = f"ошибка: {e}"
        finally:
            t.finished_at = time.time()
            _broadcast(t)

    t._async_task = asyncio.create_task(_runner())
    return t


class ProgressCtx:
    """Передаётся в coro_factory; collector'ы используют для отчёта о прогрессе."""

    def __init__(self, task: Task):
        self.task = task

    def update(
        self, current: int | None = None, total: int | None = None, message: str | None = None
    ) -> None:
        if current is not None:
            self.task.current = current
        if total is not None:
            self.task.total = total
        if message is not None:
            self.task.message = message
        if self.task.total > 0:
            self.task.progress = min(100, int(self.task.current / self.task.total * 100))
        _broadcast(self.task)


async def subscribe():
    """SSE-генератор."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subs.add(q)
    try:
        # стартовый снапшот
        snap = {"snapshot": list_tasks(limit=20)}
        yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subs.discard(q)
