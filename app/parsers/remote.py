import re

_REMOTE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bremote\b",
        r"\bудал[её]н\w*",
        r"\bдистанцион\w+",
        r"\bна\s+дому\b",
        r"\bиз\s+любой\s+точки\b",
        r"\bwork[\s-]from[\s-]home\b",
        r"\bWFH\b",
    ]
]

_HYBRID_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bгибрид\w*",
        r"\bhybrid\b",
        r"\bсмешан\w+\s+(график|формат)",
    ]
]


def is_remote_by_text(text: str) -> bool:
    if not text:
        return False
    return any(r.search(text) for r in _REMOTE_PATTERNS)


def is_hybrid_by_text(text: str) -> bool:
    if not text:
        return False
    return any(r.search(text) for r in _HYBRID_PATTERNS)
