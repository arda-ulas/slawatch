.PHONY: help install data db-up db-down db-reset load views train report tableau all test test-unit lint fmt psql clean serve lambda-build lambda-smoke

SEED ?= 20240701
N_TICKETS ?= 200000
RAW_DIR ?= data/raw
PROCESSED_DIR ?= data/processed
MODELS_DIR ?= models
API_PORT ?= 8000
LAMBDA_IMAGE ?= slawatch-lambda:local
LAMBDA_PLATFORM ?= linux/arm64
REPORTS_DIR ?= reports
TABLEAU_DIR ?= tableau/data
WEEK_ENDING ?=

help:
	@echo "make install   - create the uv environment"
	@echo "make data      - generate the synthetic raw extract into $(RAW_DIR)"
	@echo "make db-up     - start PostgreSQL 16 via docker compose and wait for health"
	@echo "make load      - clean raw CSVs, write $(PROCESSED_DIR), load Postgres, apply views"
	@echo "make views     - (re)apply sql/views/*.sql only"
	@echo "make train     - train the SLA-breach model -> $(MODELS_DIR)/, docs/img/, risk-score CSV + table"
	@echo "make report    - weekly KPI workbook -> $(REPORTS_DIR)/ (WEEK_ENDING=YYYY-MM-DD, a Sunday; default: last full week)"
	@echo "make tableau   - Tableau Public extracts -> $(TABLEAU_DIR)/"
	@echo "make all       - data + db-up + load + train"
	@echo "make serve     - run the scoring API with uvicorn on :$(API_PORT) (docs at /docs)"
	@echo "make lambda-build - build the Lambda container image $(LAMBDA_IMAGE) for $(LAMBDA_PLATFORM)"
	@echo "make lambda-smoke - run the image with the Lambda runtime emulator and hit /health, /v1/score"
	@echo "make test      - pytest (integration tests skip if Postgres is unreachable)"
	@echo "make lint      - ruff check"
	@echo "make psql      - open psql inside the container"
	@echo "make db-down   - stop the container (data volume kept); db-reset also drops it"

install:
	uv sync --all-extras --all-groups

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

train:
	uv run slawatch-train --processed $(PROCESSED_DIR) --raw $(RAW_DIR) --models $(MODELS_DIR)

report:
	uv run slawatch-report $(if $(WEEK_ENDING),--week-ending $(WEEK_ENDING),) --out-dir $(REPORTS_DIR)

tableau:
	uv run slawatch-tableau --out $(TABLEAU_DIR)

all: data db-up load train

test:
	uv run pytest

serve:
	uv run uvicorn slawatch.api:app --host 127.0.0.1 --port $(API_PORT) --reload

lambda-build:
	docker buildx build --platform $(LAMBDA_PLATFORM) --load -t $(LAMBDA_IMAGE) -f deploy/lambda/Dockerfile .

lambda-smoke:
	IMAGE=$(LAMBDA_IMAGE) deploy/lambda/smoke_local.sh

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
	rm -f $(PROCESSED_DIR)/ticket_risk_scores.csv $(MODELS_DIR)/sla_breach.joblib
