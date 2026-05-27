"""In-process event bus с SSE-стримом для UI.

Используется для отображения логов запросов, прогресса коллекторов и тостов о результатах.

Подписчики: каждый SSE-клиент имеет свою asyncio.Queue, прошлые события доступны через `tail()`.
"""

import asyncio
import json
import time
from collections import deque
from typing import Any

MAX_HISTORY = 500
_history: deque = deque(maxlen=MAX_HISTORY)
_subscribers: set[asyncio.Queue] = set()
_seq = 0


def emit(kind: str, message: str, data: dict[str, Any] | None = None) -> None:
    """Послать событие. kind: request|response|paused|wait|progress|info|warn|error|done."""
    global _seq
    _seq += 1
    ev = {
        "id": _seq,
        "ts": time.time(),
        "kind": kind,
        "message": message,
        "data": data or {},
    }
    _history.append(ev)
    for q in list(_subscribers):
        try:
            q.put_nowait(ev)
        except Exception:
            pass


def tail(n: int = 50) -> list[dict]:
    return list(_history)[-n:]


async def subscribe():
    """SSE-генератор."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    try:
        # отправим последние 20 событий чтобы новый клиент сразу видел контекст
        for ev in list(_history)[-20:]:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(q)
