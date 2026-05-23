import asyncio
import json

import pytest

from app import events as ev_mod


@pytest.fixture(autouse=True)
def _reset_events():
    ev_mod._history.clear()
    ev_mod._subscribers.clear()
    yield
    ev_mod._history.clear()
    ev_mod._subscribers.clear()


def test_emit_appends_history_with_sequence():
    ev_mod.emit("info", "hello", {"x": 1})
    ev_mod.emit("warn", "again")
    t = ev_mod.tail(10)
    assert len(t) == 2
    assert t[0]["kind"] == "info"
    assert t[0]["message"] == "hello"
    assert t[0]["data"] == {"x": 1}
    assert t[1]["kind"] == "warn"
    # id монотонно возрастает
    assert t[1]["id"] > t[0]["id"]
    # ts заполнен
    assert isinstance(t[0]["ts"], float)


def test_emit_without_data_uses_empty_dict():
    ev_mod.emit("info", "no-data")
    e = ev_mod.tail(1)[0]
    assert e["data"] == {}


def test_history_capped_at_max():
    for i in range(ev_mod.MAX_HISTORY + 10):
        ev_mod.emit("info", f"m{i}")
    assert len(ev_mod._history) == ev_mod.MAX_HISTORY
    # последний — самый новый
    last = ev_mod.tail(1)[0]
    assert last["message"] == f"m{ev_mod.MAX_HISTORY + 9}"


def test_tail_returns_last_n():
    for i in range(20):
        ev_mod.emit("info", f"m{i}")
    t = ev_mod.tail(5)
    assert len(t) == 5
    assert [x["message"] for x in t] == [f"m{i}" for i in range(15, 20)]


def test_emit_pushes_to_subscribers():
    q: asyncio.Queue = asyncio.Queue()
    ev_mod._subscribers.add(q)
    ev_mod.emit("info", "broadcast")
    assert q.qsize() == 1
    received = q.get_nowait()
    assert received["message"] == "broadcast"


def test_emit_tolerates_full_subscriber_queue():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait({"filler": True})
    ev_mod._subscribers.add(q)
    # put_nowait бросит — но emit не должен упасть
    ev_mod.emit("info", "x")


@pytest.mark.asyncio
async def test_subscribe_yields_history_and_new_events():
    # подготовим историю
    ev_mod.emit("info", "old1")
    ev_mod.emit("info", "old2")
    gen = ev_mod.subscribe()
    out1 = await gen.__anext__()
    out2 = await gen.__anext__()
    assert "old1" in out1
    assert "old2" in out2
    # теперь эмитим новый
    ev_mod.emit("warn", "new1")
    out3 = await gen.__anext__()
    assert "new1" in out3
    # формат SSE
    assert out3.startswith("data: ")
    payload = json.loads(out3.removeprefix("data: ").strip())
    assert payload["kind"] == "warn"
    await gen.aclose()
