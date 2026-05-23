from typing import Any

FX_TO_RUB: dict[str, float] = {
    "RUR": 1.0,
    "RUB": 1.0,
    "USD": 90.0,
    "EUR": 100.0,
    "KZT": 0.20,
    "BYR": 30.0,
    "BYN": 30.0,
    "UAH": 2.2,
    "GEL": 33.0,
    "UZS": 0.007,
    "AZN": 53.0,
}

TAX_RATE = 0.13


def to_rub(amount: int | float | None, currency: str | None) -> int | None:
    if amount is None or not currency:
        return None
    rate = FX_TO_RUB.get(currency.upper())
    if rate is None:
        return None
    return int(amount * rate)


def normalize_compensation(comp: dict[str, Any] | None) -> dict[str, Any]:
    """Возвращает {'from','to','currency','gross','from_rub','to_rub','mid_rub'}.
    HH в compensation хранит: currencyCode, from, to, gross."""
    if not comp:
        return {}
    cur = comp.get("currencyCode") or comp.get("currency")
    gross = comp.get("gross")
    a_from = comp.get("from")
    a_to = comp.get("to")
    out = {
        "from": a_from,
        "to": a_to,
        "currency": cur,
        "gross": gross,
    }
    f_rub = to_rub(a_from, cur)
    t_rub = to_rub(a_to, cur)
    if gross is True:
        if f_rub is not None:
            f_rub = int(f_rub * (1 - TAX_RATE))
        if t_rub is not None:
            t_rub = int(t_rub * (1 - TAX_RATE))
    out["from_rub"] = f_rub
    out["to_rub"] = t_rub
    if f_rub and t_rub:
        out["mid_rub"] = (f_rub + t_rub) // 2
    elif f_rub:
        out["mid_rub"] = f_rub
    elif t_rub:
        out["mid_rub"] = t_rub
    else:
        out["mid_rub"] = None
    return out
