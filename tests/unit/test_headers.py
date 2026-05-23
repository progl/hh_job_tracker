from app.clients import headers as h_mod


def test_headers_document_no_referer():
    h = h_mod.headers_document()
    assert h["sec-fetch-site"] == "none"
    assert "referer" not in h
    assert h["accept"].startswith("text/html")
    assert h["sec-fetch-dest"] == "document"
    assert h["sec-fetch-mode"] == "navigate"
    assert h["upgrade-insecure-requests"] == "1"
    # базовые заголовки
    assert "user-agent" in h
    assert "sec-ch-ua" in h
    assert "sec-ch-ua-platform" in h


def test_headers_document_with_referer():
    h = h_mod.headers_document(referer="https://hh.ru/foo")
    assert h["referer"] == "https://hh.ru/foo"
    assert h["sec-fetch-site"] == "same-origin"


def test_headers_xhr():
    h = h_mod.headers_xhr(referer="https://hh.ru/x", origin="https://hh.ru")
    assert h["accept"] == "*/*"
    assert h["sec-fetch-dest"] == "empty"
    assert h["sec-fetch-mode"] == "cors"
    assert h["sec-fetch-site"] == "same-origin"
    assert h["referer"] == "https://hh.ru/x"
    assert h["origin"] == "https://hh.ru"


def test_base_keys_present_in_both():
    d = h_mod.headers_document()
    x = h_mod.headers_xhr("r", "o")
    common = {"accept-language", "user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
    assert common.issubset(d.keys())
    assert common.issubset(x.keys())
