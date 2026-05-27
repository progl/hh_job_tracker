import json

import httpx
import pytest

from app.clients import cookies as ck


def test_parse_cookie_header_basic():
    items = ck.parse_cookie_header("a=1; b=2; c=hello")
    names = {i["name"]: i["value"] for i in items}
    assert names == {"a": "1", "b": "2", "c": "hello"}


def test_parse_cookie_header_empty():
    assert ck.parse_cookie_header("") == []


def test_apply_cookies_to_client_and_dump_jar():
    client = httpx.AsyncClient()
    ck.apply_cookies_to_client(
        client,
        [
            {"name": "k1", "value": "v1"},
            {"name": "k2", "value": "v2", "domain": ".example.com", "path": "/a"},
        ],
    )
    assert ck.jar_size(client) >= 2
    dumped = ck.dump_jar(client)
    names = {d["name"]: d for d in dumped}
    assert "k1" in names
    assert "k2" in names


@pytest.mark.asyncio
async def test_save_jar_and_load_jar(tmp_db):
    client = httpx.AsyncClient()
    ck.apply_cookies_to_client(client, [{"name": "a", "value": "1"}])
    await ck.save_jar(tmp_db, client)
    # без env_cookie_header — берём из БД
    loaded = await ck.load_jar(tmp_db)
    assert loaded is not None
    names = {c["name"] for c in loaded}
    assert "a" in names


@pytest.mark.asyncio
async def test_load_jar_returns_env_when_db_empty(tmp_db):
    res = await ck.load_jar(tmp_db, env_cookie_header="hhtoken=ENV; hhuid=u1")
    assert res is not None
    names = {c["name"]: c["value"] for c in res}
    assert names["hhtoken"] == "ENV"


@pytest.mark.asyncio
async def test_load_jar_env_overrides_db_on_anchor_diff(tmp_db):
    # положим в БД hhtoken=OLD
    payload = json.dumps([{"name": "hhtoken", "value": "OLD", "domain": ".hh.ru", "path": "/"}])
    await tmp_db.execute("INSERT INTO cookie_store(key, value) VALUES ('jar', ?)", (payload,))
    await tmp_db.commit()
    res = await ck.load_jar(tmp_db, env_cookie_header="hhtoken=NEW")
    assert res is not None
    names = {c["name"]: c["value"] for c in res}
    assert names["hhtoken"] == "NEW"
    # БД-jar должен быть удалён
    cur = await tmp_db.execute("SELECT COUNT(*) FROM cookie_store WHERE key='jar'")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_load_jar_keeps_db_when_anchors_match(tmp_db):
    payload = json.dumps([{"name": "hhtoken", "value": "SAME", "domain": ".hh.ru", "path": "/"}])
    await tmp_db.execute("INSERT INTO cookie_store(key, value) VALUES ('jar', ?)", (payload,))
    await tmp_db.commit()
    res = await ck.load_jar(tmp_db, env_cookie_header="hhtoken=SAME")
    # вернёт БД-jar, не env
    assert res is not None
    names = {c["name"] for c in res}
    assert "hhtoken" in names
    # БД сохранена
    cur = await tmp_db.execute("SELECT COUNT(*) FROM cookie_store WHERE key='jar'")
    assert (await cur.fetchone())[0] == 1


def test_cookie_value_helper():
    items = [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
    assert ck._cookie_value(items, "a") == "1"
    assert ck._cookie_value(items, "missing") is None
