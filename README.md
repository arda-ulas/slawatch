# slawatch

[![ci](https://github.com/arda-ulas/slawatch/actions/workflows/ci.yml/badge.svg)](https://github.com/arda-ulas/slawatch/actions/workflows/ci.yml)

SLA-breach risk analytics for a **telecom enterprise service desk**, built on **synthetic**
trouble-ticket data shaped after the public TM Forum TMF621 Trouble Ticket API. An enterprise
account team wants to catch SLA breaches on its business customers' trouble tickets early;
this repo generates a realistic ticket book (seasonality, regional outages, technician-group
backlog, reopened tickets, deliberately dirty raw extracts), cleans and loads it into
PostgreSQL, and exposes the analytics as SQL views. Every row is generated from a seed — there
is no real customer, site or ticket anywhere in this project.

Step 3 adds a scikit-learn breach classifier trained on creation-time features only, with a
time-based evaluation written up in [`docs/model.md`](docs/model.md). Step 4 serves it from a
FastAPI service packaged as an AWS Lambda container image, with CI that runs the full test
suite against Postgres and smoke-tests the image under the Lambda runtime emulator. Later
steps (not yet here): the actual AWS deployment ([`docs/deploy.md`](docs/deploy.md) is the
draft runbook), and a Tableau Public dashboard fed from `data/processed/`.

## Quickstart (steps 1–3)

Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), Docker with Compose.

```bash
uv sync --all-extras --all-groups   # or: make install
cp .env.example .env
make data                     # synthetic raw extract -> data/raw/   (~10 s for 200k tickets)
make db-up                    # PostgreSQL 16 in docker compose, waits for healthy
make load                     # clean -> data/processed/ + load Postgres + apply sql/views/  (~40 s)
make train                    # SLA-breach model -> models/, docs/img/, risk scores CSV + table (~90 s)
make test                     # pytest; the DB integration tests skip if Postgres is down
make lint                     # ruff
```

Then, for example:

```bash
make psql
slawatch=# SELECT * FROM v_top_customers_at_risk;
slawatch=# SELECT * FROM f_backlog_ageing('2025-11-01');
slawatch=# SELECT * FROM v_risk_band_summary;
```

Scoring from Python (the API imports the same module):

```python
from slawatch.model import load_model, score
score([{...creation-time features...}], model=load_model("models/sla_breach.joblib"))
# -> [{'probability': 0.756159, 'risk_band': 'high'}]
```

Smaller or different runs: `make data N_TICKETS=20000 SEED=7`, or call the CLIs directly
(`uv run slawatch-generate --help`, `uv run slawatch-pipeline --help`).

## Run the API (step 4)

The committed artifact `models/sla_breach.joblib` (~5 KB) and its `models/model_card.json`
are what the service loads, so no data or training run is needed:

```bash
make serve                    # uvicorn on http://127.0.0.1:8000, interactive docs at /docs
curl -s localhost:8000/health | jq .
curl -s -X POST localhost:8000/v1/score -H 'content-type: application/json' \
  -d "$(jq -r .body deploy/lambda/events/score.json)" | jq .
# -> {"probability": 0.756159, "risk_band": "high", "flag_for_review": true, "threshold": 0.2634..., "model_version": "0.1.0"}
```

| Endpoint | |
|---|---|
| `GET /health` | status, model version, training window (synthetic months) |
| `POST /v1/score` | one ticket's creation-time features -> probability, risk band, review flag, threshold, model version |
| `POST /v1/score/batch` | up to 500 tickets, results in input order |
| `GET /v1/model` | the model card, verified at startup to match the loaded artifact |

Validation is the same pydantic contract as `slawatch.model.TicketFeatures`: unknown levels,
out-of-range numbers and any outcome field (`sla_breached`...) are a 422. The model was trained on
**synthetic** data and the OpenAPI description says so. `SLAWATCH_MODEL_PATH` /
`SLAWATCH_MODEL_CARD_PATH` point the service at another artifact + card pair.

Lambda container image (arm64 by default; `LAMBDA_PLATFORM=linux/amd64` for Intel runners):

```bash
make lambda-build             # docker buildx -> slawatch-lambda:local, ~1.1 GB uncompressed
make lambda-smoke             # runs it under the Lambda Runtime Interface Emulator, POSTs
                              # API Gateway v2.0 events for /health and /v1/score, checks the golden value
```

Deployment to AWS (ECR + Lambda + Function URL, `ca-central-1`) is written up but **not yet
executed** in [`docs/deploy.md`](docs/deploy.md), with draft `deploy/lambda/deploy.sh` and
`teardown.sh`.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): `uv sync --locked`, ruff, the whole
pytest suite with a `postgres:16` service container (the integration tests fail rather than skip
when it is unreachable), and a build + emulator smoke test of the Lambda image.

## Repo layout

```
src/slawatch/
  config.py      all distributions and parameters of the synthetic world
  generate.py    seeded generator: dimensions, arrival process, resolution model, dirtiness
  cleaning.py    pandas cleaning functions (categoricals, duplicates, timestamps, invariants)
  features.py    creation-time-safe features vs post-resolution outcomes
  pipeline.py    raw CSV -> clean -> derive -> Postgres (COPY) + Tableau CSV extract
  db.py          engine from $DATABASE_URL, DDL runner, COPY loader
  model.py       feature contract (pydantic schema), load_model / score, model-card check
  train.py       time-split training, validation-only tuning, test evaluation, artifact + plots
  api.py         FastAPI service: /health, /v1/score, /v1/score/batch, /v1/model
  lambda_handler.py  Mangum wrapper for AWS Lambda
sql/
  schema.sql     dim_customer / dim_site / dim_service / sla_target / outage_incident,
                 fact_ticket / fact_ticket_status_history, ticket_risk_score, load_run
  views/         SLA compliance by customer x service x month; backlog ageing (function +
                 monthly/current views); MTTR p50/p90 with GROUPING SETS; outage impact;
                 customer breach-rate trend and top-at-risk; model risk scores per ticket/band
deploy/lambda/
  Dockerfile     public.ecr.aws/lambda/python:3.12 + runtime deps only + the committed model
  smoke_local.sh runs the image under the Lambda runtime emulator and checks the responses
  events/        API Gateway HTTP API v2.0 events used by the smoke test
  deploy.sh, teardown.sh   draft, parameterised, idempotent; not yet executed
docs/
  data.md        every distribution, the TMF621 mapping, injected dirtiness, leakage rules
  findings.md    what the views show on the default run, with the numbers
  model.md       the classifier: split, feature decisions, test metrics, calibration, leakage checks
  deploy.md      draft runbook for ECR / Lambda / Function URL in ca-central-1, with teardown
  img/           evaluation plots written by `make train`
models/          sla_breach.joblib (committed, ~5 KB), model_card.json, evaluation.json
tests/           generator determinism/schema/breach band, cleaning units, DB integration,
                 model module (schema, scoring, determinism), training smoke test, API
data/raw/, data/processed/   generated, gitignored
.github/workflows/ci.yml, docker-compose.yml, .env.example, Makefile
```
