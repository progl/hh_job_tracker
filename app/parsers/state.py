import html as html_mod
import json
import re

_TEMPLATE_RE = re.compile(
    r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>',
    re.DOTALL,
)


def extract_initial_state(html_text: str) -> dict | None:
    m = _TEMPLATE_RE.search(html_text)
    if not m:
        return None
    return json.loads(html_mod.unescape(m.group(1)))
