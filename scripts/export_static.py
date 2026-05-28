"""Экспортирует статический снапшот UI приложения для GitHub Pages.

Подключается к работающему demo-uvicorn на http://127.0.0.1:8099 (make demo-run),
выкачивает HTML страницы и переписывает ссылки/удаляет HTMX-интерактив.
Результат — в docs/site/. Порт 8099 (а не 8000) — чтобы не конфликтовать с
реальным `make run`.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL = "http://127.0.0.1:8099"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "docs" / "site"
DB_PATH = PROJECT_ROOT / "data" / "hh_demo.db"

# карта route -> относительный файл (от корня docs/site)
STATIC_ROUTES: dict[str, str] = {
    "/": "index.html",
    "/search": "search.html",
    "/searches": "searches.html",
    "/funnel": "funnel.html",
    "/profile": "profile.html",
    "/logs": "logs.html",
    "/compare": "compare.html",
    "/llm-logs": "llm_logs.html",
    "/analytics": "analytics.html",
    "/jobs": "jobs.html",
}

# HTMX-атрибуты, которые надо вычистить
HTMX_ATTR_PREFIXES = ("hx-", "sse-", "data-hx-")
HTMX_ATTR_EXACT = {"hx-ext"}

BANNER_HTML = (
    '<div style="background:#fef3c7;color:#78350f;padding:8px 16px;'
    "text-align:center;font-family:system-ui;font-size:14px;"
    'border-bottom:1px solid #fbbf24">'
    "Это статический демо-снапшот UI. Интерактив (фильтры, кнопки, формы) отключён. "
    '<a href="https://github.com/progl/hh_job_tracker" '
    'style="color:#1d4ed8;text-decoration:underline">Исходники</a>'
    "</div>"
)


def fetch_vacancy_ids() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id FROM vacancies ORDER BY id").fetchall()
    return [int(r[0]) for r in rows]


def rewrite_href(href: str, *, in_vacancy_dir: bool) -> str | None:
    """Возвращает новый href или None, если оставить как есть."""
    if not href:
        return None
    # ссылки на API/абсолютные/якоря не трогаем
    if href.startswith(("http://", "https://", "#", "mailto:", "tel:", "javascript:")):
        return None
    # статические маршруты
    if href in STATIC_ROUTES:
        target = STATIC_ROUTES[href]
        return ("../" + target) if in_vacancy_dir else target
    # /vacancy/{id}
    if href.startswith("/vacancy/"):
        rest = href[len("/vacancy/") :]
        # отрезаем query/fragment
        for sep in ("?", "#"):
            if sep in rest:
                rest = rest.split(sep, 1)[0]
        if not rest:
            return None
        return (rest + ".html") if in_vacancy_dir else f"vacancy/{rest}.html"
    return None


def strip_htmx_attrs(soup: BeautifulSoup) -> int:
    removed = 0
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            low = attr.lower()
            if (
                any(low.startswith(p) for p in HTMX_ATTR_PREFIXES)
                or low in HTMX_ATTR_EXACT
                or low.startswith("hx-on")
            ):
                del tag.attrs[attr]
                removed += 1
    return removed


def remove_htmx_and_sse_scripts(soup: BeautifulSoup) -> tuple[int, int]:
    htmx_removed = 0
    sse_removed = 0
    for script in list(soup.find_all("script")):
        src = script.get("src", "") or ""
        if "htmx" in src.lower():
            script.decompose()
            htmx_removed += 1
            continue
        body = script.string or script.text or ""
        if "/api/tasks/stream" in body or "tasks/stream" in body:
            script.decompose()
            sse_removed += 1
    return htmx_removed, sse_removed


def rewrite_links(soup: BeautifulSoup, *, in_vacancy_dir: bool) -> int:
    rewritten = 0
    for a in soup.find_all("a", href=True):
        new = rewrite_href(a["href"], in_vacancy_dir=in_vacancy_dir)
        if new is not None:
            a["href"] = new
            rewritten += 1
    # форма action тоже на всякий случай — но статика их не использует;
    # пометим disabled
    for form in soup.find_all("form"):
        form["onsubmit"] = "return false;"
    return rewritten


def add_banner(soup: BeautifulSoup) -> None:
    body = soup.find("body")
    if body is None:
        return
    banner = BeautifulSoup(BANNER_HTML, "html.parser")
    body.insert(0, banner)


def transform_html(html: str, *, in_vacancy_dir: bool) -> tuple[str, dict[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    stats = {
        "links": rewrite_links(soup, in_vacancy_dir=in_vacancy_dir),
        "htmx_attrs": strip_htmx_attrs(soup),
    }
    htmx_scripts, sse_scripts = remove_htmx_and_sse_scripts(soup)
    stats["htmx_scripts"] = htmx_scripts
    stats["sse_scripts"] = sse_scripts
    add_banner(soup)
    return str(soup), stats


def write_page(client: httpx.Client, route: str, out_rel: str) -> dict:
    resp = client.get(route)
    resp.raise_for_status()
    in_vacancy_dir = out_rel.startswith("vacancy/")
    html, stats = transform_html(resp.text, in_vacancy_dir=in_vacancy_dir)
    out_path = OUT_DIR / out_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return {"route": route, "out": str(out_path.relative_to(PROJECT_ROOT)), **stats, "bytes": len(html)}


@contextmanager
def _demo_server():
    """Поднимает demo-uvicorn на BASE_URL с DB_PATH=demo, гасит на выходе."""
    parsed = urlparse(BASE_URL)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8099
    env = {**os.environ, "DB_PATH": str(DB_PATH)}
    print(f"[serve] поднимаю demo-сервер на {BASE_URL} (DB={DB_PATH.name})…")
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "app.web.app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--timeout-graceful-shutdown",
            "3",
        ],
        env=env,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                with httpx.Client(base_url=BASE_URL, timeout=2) as probe:
                    probe.get("/").raise_for_status()
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("demo-сервер упал при старте") from None
                time.sleep(0.5)
        else:
            raise RuntimeError(f"demo-сервер не поднялся на {BASE_URL} за 40с")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("[serve] demo-сервер остановлен")


def _export() -> int:
    # проверяем uvicorn
    try:
        with httpx.Client(base_url=BASE_URL, timeout=10) as probe:
            r = probe.get("/")
            r.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] uvicorn недоступен на {BASE_URL}: {exc}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    vacancy_ids = fetch_vacancy_ids()
    print(f"[info] vacancies in demo DB: {len(vacancy_ids)}")

    results: list[dict] = []
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        for route, out_rel in STATIC_ROUTES.items():
            results.append(write_page(client, route, out_rel))
        for vid in vacancy_ids:
            results.append(write_page(client, f"/vacancy/{vid}", f"vacancy/{vid}.html"))

    total_bytes = sum(r["bytes"] for r in results)
    print(f"[done] pages={len(results)} total_bytes={total_bytes} ({total_bytes / 1024:.1f} KiB)")
    for r in results[:3]:
        print(f"  sample: {r}")
    return 0


def main(serve: bool = False) -> int:
    if serve:
        with _demo_server():
            return _export()
    return _export()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Экспорт статического снапшота UI в docs/site/.")
    ap.add_argument(
        "--serve",
        action="store_true",
        help="самому поднять demo-uvicorn на 8099 (иначе подключается к уже запущенному)",
    )
    args = ap.parse_args()
    raise SystemExit(main(serve=args.serve))
