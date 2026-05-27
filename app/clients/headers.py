from app.config import settings


def _base() -> dict[str, str]:
    return {
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "user-agent": settings.HH_USER_AGENT,
        "sec-ch-ua": settings.HH_SEC_CH_UA,
        "sec-ch-ua-mobile": settings.HH_SEC_CH_UA_MOBILE,
        "sec-ch-ua-platform": settings.HH_SEC_CH_UA_PLATFORM,
    }


def headers_document(referer: str | None = None) -> dict[str, str]:
    h = _base()
    h.update(
        {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            ),
            "priority": "u=0, i",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin" if referer else "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        }
    )
    if referer:
        h["referer"] = referer
    return h


def headers_xhr(referer: str, origin: str) -> dict[str, str]:
    h = _base()
    h.update(
        {
            "accept": "*/*",
            "priority": "u=4, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": referer,
            "origin": origin,
        }
    )
    return h
