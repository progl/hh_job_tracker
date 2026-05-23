import re

_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    ("lead", [re.compile(p, re.IGNORECASE) for p in [
        r"\bteam[\s-]?lead\b", r"\btech[\s-]?lead\b", r"\btl\b",
        r"\bтимлид\w*", r"\bруководитель\s+(группы|команды|отдела|направлен\w+|разработ\w+)",
        r"\bруководитель\s+\w+\s+(разработ\w+|команды|группы)",
        r"\bлид[ея]р\s+команды",
        r"\bстарший\s+разработчик[-\s]лид",
        r"\bengineering[\s-]manager\b", r"\bem\b",
        r"\bhead\s+of\s+(engineering|development|разработ\w+)",
    ]]),
    ("senior", [re.compile(p, re.IGNORECASE) for p in [
        r"\bsenior\b", r"\bsr\.?\b", r"\bсеньор\w*", r"\bведущ\w+\s+(разработ\w+|программист\w*|инженер\w*)",
        r"\bстарший\s+разработ\w+",
    ]]),
    ("middle", [re.compile(p, re.IGNORECASE) for p in [
        r"\bmiddle\b", r"\bmid\b", r"\bмидл\w*", r"\bсредний\s+разработ\w+",
    ]]),
    ("junior", [re.compile(p, re.IGNORECASE) for p in [
        r"\bjunior\b", r"\bjr\.?\b", r"\bджуниор\w*", r"\bмладший\s+разработ\w+",
    ]]),
    ("intern", [re.compile(p, re.IGNORECASE) for p in [
        r"\bintern\b", r"\bстажёр\w*", r"\bстажер\w*",
    ]]),
]


def detect_level(text: str) -> str | None:
    if not text:
        return None
    text_low = text
    for level, regexes in _PATTERNS:
        if any(r.search(text_low) for r in regexes):
            return level
    return None
