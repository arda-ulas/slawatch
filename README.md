# slawatch

SLA-breach risk analytics for a **telecom enterprise service desk**, built on **synthetic**
trouble-ticket data shaped after the public TM Forum TMF621 Trouble Ticket API. An enterprise
account team wants to catch SLA breaches on its business customers' trouble tickets early;
this repo generates a realistic ticket book (seasonality, regional outages, technician-group
backlog, reopened tickets, deliberately dirty raw extracts), cleans and loads it into
PostgreSQL, and exposes the analytics as SQL views. Every row is generated from a seed — there
is no real customer, site or ticket anywhere in this project.

Later steps (not yet here): a scikit-learn breach classifier, a FastAPI scoring service on AWS
Lambda, and a Tableau Public dashboard fed from `data/processed/`.

## Quickstart (steps 1–2)

Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), Docker with Compose.

```bash
uv sync --all-groups          # or: make install
cp .env.example .env
make data                     # synthetic raw extract -> data/raw/   (~10 s for 200k tickets)
make db-up                    # PostgreSQL 16 in docker compose, waits for healthy
make load                     # clean -> data/processed/ + load Postgres + apply sql/views/  (~40 s)
make test                     # pytest; the DB integration tests skip if Postgres is down
make lint                     # ruff
```

Then, for example:

```bash
make psql
slawatch=# SELECT * FROM v_top_customers_at_risk;
slawatch=# SELECT * FROM f_backlog_ageing('2025-11-01');
```

Smaller or different runs: `make data N_TICKETS=20000 SEED=7`, or call the CLIs directly
(`uv run slawatch-generate --help`, `uv run slawatch-pipeline --help`).

## Repo layout

```
src/slawatch/
  config.py      all distributions and parameters of the synthetic world
  generate.py    seeded generator: dimensions, arrival process, resolution model, dirtiness
  cleaning.py    pandas cleaning functions (categoricals, duplicates, timestamps, invariants)
  features.py    creation-time-safe features vs post-resolution outcomes
  pipeline.py    raw CSV -> clean -> derive -> Postgres (COPY) + Tableau CSV extract
  db.py          engine from $DATABASE_URL, DDL runner, COPY loader
sql/
  schema.sql     dim_customer / dim_site / dim_service / sla_target / outage_incident,
                 fact_ticket / fact_ticket_status_history, load_run
  views/         SLA compliance by customer x service x month; backlog ageing (function +
                 monthly/current views); MTTR p50/p90 with GROUPING SETS; outage impact;
                 customer breach-rate trend and top-at-risk
docs/
  data.md        every distribution, the TMF621 mapping, injected dirtiness, leakage rules
  findings.md    what the views show on the default run, with the numbers
tests/           generator determinism/schema/breach band, cleaning units, DB integration
data/raw/, data/processed/   generated, gitignored
docker-compose.yml, .env.example, Makefile
```
