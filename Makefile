# RQIS — Developer convenience targets
# Usage: make <target>

.PHONY: up down clean logs shell-db migrate backfill test lint typecheck fmt

# ─── Infrastructure ───────────────────────────────────────────────────────────

up:
	docker compose up -d
	@echo "Services starting. Run 'make logs' to follow output."
	@echo "  Airflow UI:  http://localhost:8080"
	@echo "  MLflow UI:   http://localhost:5000"
	@echo "  MinIO UI:    http://localhost:9001"
	@echo "  Grafana:     http://localhost:3000"
	@echo "  Prometheus:  http://localhost:9090"

down:
	docker compose down

# WARNING: Destroys all data volumes. Requires interactive confirmation.
clean:
	@echo "⚠️  This will destroy all local data (TimescaleDB, MinIO, Redis)."
	@read -p "Type YES to confirm: " confirm && [ "$$confirm" = "YES" ] || (echo "Aborted." && exit 1)
	docker compose down -v
	@echo "All volumes removed."

logs:
	docker compose logs -f --tail=50

logs-db:
	docker compose logs -f timescaledb

shell-db:
	docker compose exec timescaledb psql -U rqis -d rqis

shell-redis:
	docker compose exec redis redis-cli

# ─── Database ─────────────────────────────────────────────────────────────────

migrate:
	alembic upgrade head

migrate-down:
	alembic downgrade -1

migrate-status:
	alembic current

# ─── Data ─────────────────────────────────────────────────────────────────────

# Backfill 5 years of daily OHLCV for S&P 500.
# Run once after first 'make up' and 'make migrate'.
backfill:
	python -m data.ingestion.market.yfinance_client backfill

# ─── Testing ──────────────────────────────────────────────────────────────────

test:
	pytest --cov=data --cov=signals --cov=portfolio --cov=execution --cov=risk --cov=backtesting \
	       --cov-report=term-missing --cov-report=html:htmlcov \
	       -m "not integration"

test-integration:
	pytest -m integration -v

# ─── Code quality ─────────────────────────────────────────────────────────────

lint:
	ruff check .

fmt:
	ruff format .

typecheck:
	mypy data signals portfolio execution risk backtesting reporting

check: fmt lint typecheck test
	@echo "All checks passed."
