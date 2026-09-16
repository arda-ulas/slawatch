-- slawatch analytical schema (PostgreSQL 16). All data loaded here is synthetic.
-- Dimension tables (customer, site, service, SLA target, outage) + fact tables
-- (ticket, ticket status history) + a load-run audit table.
-- Re-runnable: drops and recreates everything.

DROP VIEW IF EXISTS
    v_sla_compliance_monthly,
    v_backlog_ageing_monthly,
    v_backlog_ageing_current,
    v_resolution_stats,
    v_outage_impact,
    v_outage_vs_normal,
    v_customer_risk_trend,
    v_top_customers_at_risk,
    v_ticket_risk,
    v_risk_band_summary
    CASCADE;
DROP FUNCTION IF EXISTS f_backlog_ageing(timestamptz);
DROP TABLE IF EXISTS ticket_risk_score CASCADE;
DROP TABLE IF EXISTS fact_ticket_status_history CASCADE;
DROP TABLE IF EXISTS fact_ticket CASCADE;
DROP TABLE IF EXISTS outage_incident CASCADE;
DROP TABLE IF EXISTS sla_target CASCADE;
DROP TABLE IF EXISTS dim_service CASCADE;
DROP TABLE IF EXISTS dim_site CASCADE;
DROP TABLE IF EXISTS dim_customer CASCADE;
DROP TABLE IF EXISTS load_run CASCADE;

-- ---------------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------------
CREATE TABLE dim_customer (
    customer_id     text PRIMARY KEY,
    customer_name   text NOT NULL,
    industry        text NOT NULL,
    tier            text NOT NULL CHECK (tier IN ('platinum', 'gold', 'silver', 'bronze')),
    hq_province     char(2) NOT NULL
);

CREATE TABLE dim_site (
    site_id     text PRIMARY KEY,
    customer_id text NOT NULL REFERENCES dim_customer (customer_id),
    site_name   text NOT NULL,
    city        text NOT NULL,
    province    char(2) NOT NULL,
    region      text NOT NULL,
    timezone    text NOT NULL
);
CREATE INDEX ix_site_customer ON dim_site (customer_id);
CREATE INDEX ix_site_region ON dim_site (region);

CREATE TABLE dim_service (
    service_id       text PRIMARY KEY,
    site_id          text NOT NULL REFERENCES dim_site (site_id),
    customer_id      text NOT NULL REFERENCES dim_customer (customer_id),
    service_type     text NOT NULL CHECK (service_type IN (
                         'dedicated_internet', 'sd_wan', 'business_voice_sip',
                         'managed_cloud', 'managed_security', 'mpls_wan')),
    bandwidth_mbps   integer CHECK (bandwidth_mbps IS NULL OR bandwidth_mbps > 0),
    assignment_group text NOT NULL
);
CREATE INDEX ix_service_site ON dim_service (site_id);
CREATE INDEX ix_service_customer_type ON dim_service (customer_id, service_type);

CREATE TABLE sla_target (
    tier         text NOT NULL CHECK (tier IN ('platinum', 'gold', 'silver', 'bronze')),
    severity     text NOT NULL CHECK (severity IN ('critical', 'major', 'minor', 'low')),
    target_hours numeric(8, 2) NOT NULL CHECK (target_hours > 0),
    PRIMARY KEY (tier, severity)
);

CREATE TABLE outage_incident (
    outage_id    text PRIMARY KEY,
    region       text NOT NULL,
    service_type text NOT NULL,
    start_ts     timestamptz NOT NULL,
    end_ts       timestamptz NOT NULL,
    cause        text NOT NULL,
    CHECK (end_ts > start_ts)
);

-- ---------------------------------------------------------------------------
-- Facts
-- ---------------------------------------------------------------------------
CREATE TABLE fact_ticket (
    ticket_id                 text PRIMARY KEY,
    name                      text NOT NULL,
    description               text,
    ticket_type               text NOT NULL CHECK (ticket_type IN (
                                  'incident', 'service_request', 'query', 'complaint')),
    severity                  text NOT NULL CHECK (severity IN ('critical', 'major', 'minor', 'low')),
    priority                  smallint NOT NULL CHECK (priority BETWEEN 1 AND 4),
    status                    text NOT NULL CHECK (status IN (
                                  'acknowledged', 'in_progress', 'pending', 'held',
                                  'resolved', 'closed', 'cancelled')),
    channel                   text NOT NULL,
    -- timestamps (TMF621 creationDate / lastUpdate / expectedResolutionDate / ...)
    creation_ts               timestamptz NOT NULL,
    last_update_ts            timestamptz NOT NULL,
    expected_resolution_ts    timestamptz NOT NULL,
    requested_resolution_ts   timestamptz,
    resolution_ts             timestamptz,
    -- related parties / entities
    customer_id               text NOT NULL REFERENCES dim_customer (customer_id),
    site_id                   text NOT NULL REFERENCES dim_site (site_id),
    service_id                text NOT NULL REFERENCES dim_service (service_id),
    assignment_group          text NOT NULL,
    active_outage_id          text REFERENCES outage_incident (outage_id),
    -- creation-time context
    open_backlog_at_creation  integer NOT NULL CHECK (open_backlog_at_creation >= 0),
    sla_target_hours          numeric(8, 2) NOT NULL,
    creation_hour_local       smallint NOT NULL CHECK (creation_hour_local BETWEEN 0 AND 23),
    creation_dow_local        smallint NOT NULL CHECK (creation_dow_local BETWEEN 0 AND 6),
    is_weekend                boolean NOT NULL,
    is_after_hours            boolean NOT NULL,
    -- outcomes (post-creation; never model features)
    reopen_count              smallint NOT NULL DEFAULT 0,
    resolution_hours          numeric(10, 4),
    is_resolved               boolean NOT NULL,
    sla_breached              boolean,
    CONSTRAINT ck_resolution_after_creation
        CHECK (resolution_ts IS NULL OR resolution_ts >= creation_ts),
    CONSTRAINT ck_last_update_after_creation
        CHECK (last_update_ts >= creation_ts),
    CONSTRAINT ck_resolved_consistency
        CHECK (is_resolved = (resolution_ts IS NOT NULL))
);
CREATE INDEX ix_ticket_creation ON fact_ticket (creation_ts);
CREATE INDEX ix_ticket_customer_creation ON fact_ticket (customer_id, creation_ts);
CREATE INDEX ix_ticket_service ON fact_ticket (service_id);
CREATE INDEX ix_ticket_site ON fact_ticket (site_id);
CREATE INDEX ix_ticket_group_creation ON fact_ticket (assignment_group, creation_ts);
CREATE INDEX ix_ticket_status ON fact_ticket (status) WHERE status NOT IN ('closed', 'cancelled');
CREATE INDEX ix_ticket_resolution ON fact_ticket (resolution_ts);
CREATE INDEX ix_ticket_breached ON fact_ticket (sla_breached) WHERE sla_breached;

CREATE TABLE fact_ticket_status_history (
    history_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticket_id     text NOT NULL REFERENCES fact_ticket (ticket_id),
    sequence_no   smallint NOT NULL,
    status        text NOT NULL CHECK (status IN (
                      'acknowledged', 'in_progress', 'pending', 'held',
                      'resolved', 'closed', 'cancelled')),
    change_ts     timestamptz NOT NULL,
    change_reason text,
    UNIQUE (ticket_id, sequence_no)
);
CREATE INDEX ix_history_ticket_ts ON fact_ticket_status_history (ticket_id, change_ts);
CREATE INDEX ix_history_ts ON fact_ticket_status_history (change_ts);
CREATE INDEX ix_history_status_ts ON fact_ticket_status_history (status, change_ts);

-- ---------------------------------------------------------------------------
-- Model output: one risk score per ticket, written by `make train` (slawatch-train)
-- ---------------------------------------------------------------------------
CREATE TABLE ticket_risk_score (
    ticket_id     text PRIMARY KEY REFERENCES fact_ticket (ticket_id),
    model_version text NOT NULL,
    probability   numeric(8, 6) NOT NULL CHECK (probability BETWEEN 0 AND 1),
    risk_band     text NOT NULL CHECK (risk_band IN ('low', 'medium', 'high')),
    split         text NOT NULL CHECK (split IN ('train', 'validation', 'test', 'unlabelled')),
    scored_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_risk_band ON ticket_risk_score (risk_band);

-- ---------------------------------------------------------------------------
-- Load audit
-- ---------------------------------------------------------------------------
CREATE TABLE load_run (
    run_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    snapshot_ts     timestamptz NOT NULL,   -- "as of" moment for open tickets
    source_dir      text NOT NULL,
    synthetic       boolean NOT NULL DEFAULT true,
    row_counts      jsonb NOT NULL,
    cleaning_report jsonb NOT NULL
);
