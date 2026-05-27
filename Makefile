.PHONY: sync run db-init clean demo-seed demo-run demo-clean test coverage lint lint-fix format check

sync:
	uv sync

run:
	uv run uvicorn app.web.app:app --reload --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 3

db-init:
	uv run python -c "import asyncio; from app.db.db import init_db; asyncio.run(init_db())"

clean:
	rm -rf data/hh.db data/hh.db-wal data/hh.db-shm

# Demo-режим: отдельная БД с вымышленными данными для скриншотов.
# Реальная data/hh.db не трогается.
demo-seed:
	uv run python -m scripts.seed_demo --force

demo-run:
	DB_PATH=data/hh_demo.db uv run uvicorn app.web.app:app --reload --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 3

demo-clean:
	rm -rf data/hh_demo.db data/hh_demo.db-wal data/hh_demo.db-shm

test:
	uv run pytest tests/ -v

coverage:
	uv run pytest tests/ --cov=app --cov-report=term-missing:skip-covered --cov-report=html:htmlcov
	@echo "HTML отчёт: htmlcov/index.html"

lint:
	uv run ruff check app tests scripts

lint-fix:
	uv run ruff check --fix app tests scripts

format:
	uv run ruff format app tests scripts

check: lint
	uv run ruff format --check app tests scripts
	uv run pytest tests/ -q
