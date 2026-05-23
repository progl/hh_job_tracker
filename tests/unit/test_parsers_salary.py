from app.parsers.salary import FX_TO_RUB, normalize_compensation, to_rub


def test_to_rub_rur_passthrough():
    assert to_rub(100000, "RUR") == 100000


def test_to_rub_usd():
    assert to_rub(1000, "USD") == int(1000 * FX_TO_RUB["USD"])


def test_to_rub_unknown_currency():
    assert to_rub(1000, "JPY") is None


def test_to_rub_none_amount():
    assert to_rub(None, "RUR") is None


def test_to_rub_lowercase():
    assert to_rub(100, "usd") == int(100 * FX_TO_RUB["USD"])


def test_normalize_empty():
    assert normalize_compensation(None) == {}
    assert normalize_compensation({}) == {}


def test_normalize_full_rur():
    out = normalize_compensation({"currencyCode": "RUR", "from": 100000, "to": 200000, "gross": False})
    assert out["from_rub"] == 100000
    assert out["to_rub"] == 200000
    assert out["mid_rub"] == 150000


def test_normalize_gross_subtracts_tax():
    out = normalize_compensation({"currencyCode": "RUR", "from": 100000, "to": 200000, "gross": True})
    assert out["from_rub"] == int(100000 * 0.87)
    assert out["to_rub"] == int(200000 * 0.87)


def test_normalize_only_from():
    out = normalize_compensation({"currencyCode": "RUR", "from": 100000, "gross": False})
    assert out["from_rub"] == 100000
    assert out["to_rub"] is None
    assert out["mid_rub"] == 100000


def test_normalize_only_to():
    out = normalize_compensation({"currencyCode": "RUR", "to": 200000, "gross": False})
    assert out["mid_rub"] == 200000


def test_normalize_usd():
    out = normalize_compensation({"currencyCode": "USD", "from": 5000, "to": 7000, "gross": False})
    assert out["from_rub"] == int(5000 * FX_TO_RUB["USD"])
    assert out["currency"] == "USD"


def test_normalize_currency_field_fallback():
    out = normalize_compensation({"currency": "RUR", "from": 100000, "gross": False})
    assert out["currency"] == "RUR"
    assert out["from_rub"] == 100000
