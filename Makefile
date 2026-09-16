.PHONY: help install data db-up db-down db-reset load views all test test-unit lint fmt psql clean

SEED ?= 20240701
N_TICKETS ?= 200000
RAW_DIR ?= data/raw
PROCESSED_DIR ?= data/processed

help:
	@echo "make install   - create the uv environment"
	@echo "make data      - generate the synthetic raw extract into $(RAW_DIR)"
	@echo "make db-up     - start PostgreSQL 16 via docker compose and wait for health"
	@echo "make load      - clean raw CSVs, write $(PROCESSED_DIR), load Postgres, apply views"
	@echo "make views     - (re)apply sql/views/*.sql only"
	@echo "make all       - data + db-up + load"
	@echo "make test      - pytest (integration tests skip if Postgres is unreachable)"
	@echo "make lint      - ruff check"
	@echo "make psql      - open psql inside the container"
	@echo "make db-down   - stop the container (data volume kept); db-reset also drops it"

install:
	uv sync --all-groups

data:
	uv run slawatch-generate --seed $(SEED) --n-tickets $(N_TICKETS) --out $(RAW_DIR)

db-up:
	docker compose up -d --wait

db-down:
	docker compose down

db-reset:
	docker compose down -v

load:
	uv run slawatch-pipeline --raw $(RAW_DIR) --processed $(PROCESSED_DIR)

views:
	uv run slawatch-pipeline --views-only

all: data db-up load

test:
	uv run pytest

test-unit:
	uv run pytest -m "not integration"

lint:
	uv run ruff check
	uv run ruff format --check

fmt:
	uv run ruff format

psql:
	docker compose exec db psql -U slawatch -d slawatch

clean:
	rm -f $(RAW_DIR)/synthetic_* $(PROCESSED_DIR)/synthetic_* $(PROCESSED_DIR)/pipeline_report.json
