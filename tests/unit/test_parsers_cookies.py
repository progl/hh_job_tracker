import httpx

from app.clients.cookies import (
    apply_cookies_to_client,
    dump_jar,
    jar_size,
    parse_cookie_header,
)


def test_parse_cookie_header_empty():
    assert parse_cookie_header("") == []


def test_parse_cookie_header_single():
    out = parse_cookie_header("foo=bar")
    assert out == [{"name": "foo", "value": "bar"}]


def test_parse_cookie_header_multiple():
    out = parse_cookie_header("foo=bar; baz=qux; x=1")
    names = [c["name"] for c in out]
    assert "foo" in names and "baz" in names and "x" in names
    assert next(c["value"] for c in out if c["name"] == "baz") == "qux"


def test_apply_and_dump_jar():
    client = httpx.AsyncClient()
    apply_cookies_to_client(client, [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}])
    out = dump_jar(client)
    names = {c["name"] for c in out}
    assert {"a", "b"} <= names


def test_jar_size():
    client = httpx.AsyncClient()
    assert jar_size(client) == 0
    apply_cookies_to_client(client, [{"name": "a", "value": "1"}])
    assert jar_size(client) == 1
