-- Weekly service-desk KPIs (the source of the Excel report, slawatch-report).
--
-- Weeks run Monday 00:00 to Sunday 24:00 UTC; `week_ending` is the Sunday. Only full weeks
-- inside the loaded window are listed (a week is complete when its Monday-after is at or
-- before the load snapshot). Cancelled tickets are excluded everywhere.
--
--   tickets_opened      created in the week
--   tickets_resolved    final resolution in the week; breaches, compliance and the resolution
--                       percentiles are over this set (resolution-week basis, so the latest
--                       week is never an unfinished cohort)
--   open_backlog        open tickets at the end of the week (f_open_tickets), of which
--                       backlog_past_due are already past their expected resolution date
--                       ("aged over the SLA") and high_risk_open carry the model's high band
--
-- v_weekly_kpi              one row per week, whole desk
-- v_weekly_kpi_by_customer  week x customer (opened / resolved / breached / compliance / p50)
-- v_weekly_kpi_by_service   week x service type, the same plus p90

CREATE OR REPLACE VIEW v_weekly_kpi AS
WITH bounds AS (
    SELECT
        date_trunc('week', min(t.creation_ts) AT TIME ZONE 'UTC') AS first_week,
        (SELECT max(snapshot_ts) FROM load_run) AT TIME ZONE 'UTC' AS snapshot
    FROM fact_ticket t
),
weeks AS (
    SELECT ws AS week_start_ts, ws + interval '7 days' AS week_end_ts
    FROM bounds, generate_series(first_week, snapshot - interval '7 days', interval '1 week') ws
),
opened AS (
    SELECT
        date_trunc('week', creation_ts AT TIME ZONE 'UTC') AS ws,
        count(*) AS tickets_opened
    FROM fact_ticket
    WHERE status <> 'cancelled'
    GROUP BY 1
),
resolved AS (
    SELECT
        date_trunc('week', resolution_ts AT TIME ZONE 'UTC') AS ws,
        count(*)                                  AS tickets_resolved,
        count(*) FILTER (WHERE sla_breached)      AS breached_tickets,
        round(avg(resolution_hours), 2)           AS mttr_hours,
        round(percentile_cont(0.5) WITHIN GROUP (ORDER BY resolution_hours)::numeric, 2)
                                                  AS p50_resolution_hours,
        round(percentile_cont(0.9) WITHIN GROUP (ORDER BY resolution_hours)::numeric, 2)
                                                  AS p90_resolution_hours
    FROM fact_ticket
    WHERE is_resolved AND status <> 'cancelled'
    GROUP BY 1
),
backlog AS (
    SELECT
        w.week_start_ts,
        count(*)                                        AS open_backlog,
        count(*) FILTER (WHERE o.past_due)              AS backlog_past_due,
        count(*) FILTER (WHERE o.risk_band = 'high')    AS high_risk_open
    FROM weeks w
    CROSS JOIN LATERAL f_open_tickets(w.week_end_ts AT TIME ZONE 'UTC') o
    GROUP BY w.week_start_ts
)
SELECT
    w.week_start_ts::date                                   AS week_start,
    (w.week_end_ts - interval '1 day')::date                AS week_ending,
    w.week_end_ts AT TIME ZONE 'UTC'                        AS backlog_as_of,
    COALESCE(o.tickets_opened, 0)                           AS tickets_opened,
    COALESCE(r.tickets_resolved, 0)                         AS tickets_resolved,
    COALESCE(r.breached_tickets, 0)                         AS breached_tickets,
    round(100.0 * (1 - COALESCE(r.breached_tickets, 0)::numeric
                       / NULLIF(r.tickets_resolved, 0)), 2) AS sla_compliance_pct,
    r.mttr_hours,
    r.p50_resolution_hours,
    r.p90_resolution_hours,
    COALESCE(b.open_backlog, 0)                             AS open_backlog,
    COALESCE(b.backlog_past_due, 0)                         AS backlog_past_due,
    COALESCE(b.high_risk_open, 0)                           AS high_risk_open
FROM weeks w
LEFT JOIN opened   o ON o.ws = w.week_start_ts
LEFT JOIN resolved r ON r.ws = w.week_start_ts
LEFT JOIN backlog  b ON b.week_start_ts = w.week_start_ts;

CREATE OR REPLACE VIEW v_weekly_kpi_by_customer AS
WITH opened AS (
    SELECT
        date_trunc('week', creation_ts AT TIME ZONE 'UTC') AS ws,
        customer_id,
        count(*) AS tickets_opened
    FROM fact_ticket
    WHERE status <> 'cancelled'
    GROUP BY 1, 2
),
resolved AS (
    SELECT
        date_trunc('week', resolution_ts AT TIME ZONE 'UTC') AS ws,
        customer_id,
        count(*)                                  AS tickets_resolved,
        count(*) FILTER (WHERE sla_breached)      AS breached_tickets,
        round(percentile_cont(0.5) WITHIN GROUP (ORDER BY resolution_hours)::numeric, 2)
                                                  AS p50_resolution_hours
    FROM fact_ticket
    WHERE is_resolved AND status <> 'cancelled'
    GROUP BY 1, 2
)
SELECT
    k.week_start,
    k.week_ending,
    c.customer_id,
    c.customer_name,
    c.tier,
    c.industry,
    COALESCE(o.tickets_opened, 0)                           AS tickets_opened,
    COALESCE(r.tickets_resolved, 0)                         AS tickets_resolved,
    COALESCE(r.breached_tickets, 0)                         AS breached_tickets,
    round(100.0 * (1 - COALESCE(r.breached_tickets, 0)::numeric
                       / NULLIF(r.tickets_resolved, 0)), 2) AS sla_compliance_pct,
    r.p50_resolution_hours
FROM v_weekly_kpi k
CROSS JOIN dim_customer c
LEFT JOIN opened   o ON o.ws = k.week_start AND o.customer_id = c.customer_id
LEFT JOIN resolved r ON r.ws = k.week_start AND r.customer_id = c.customer_id;

CREATE OR REPLACE VIEW v_weekly_kpi_by_service AS
WITH opened AS (
    SELECT
        date_trunc('week', t.creation_ts AT TIME ZONE 'UTC') AS ws,
        s.service_type,
        count(*) AS tickets_opened
    FROM fact_ticket t
    JOIN dim_service s USING (service_id)
    WHERE t.status <> 'cancelled'
    GROUP BY 1, 2
),
resolved AS (
    SELECT
        date_trunc('week', t.resolution_ts AT TIME ZONE 'UTC') AS ws,
        s.service_type,
        count(*)                                  AS tickets_resolved,
        count(*) FILTER (WHERE t.sla_breached)    AS breached_tickets,
        round(percentile_cont(0.5) WITHIN GROUP (ORDER BY t.resolution_hours)::numeric, 2)
                                                  AS p50_resolution_hours,
        round(percentile_cont(0.9) WITHIN GROUP (ORDER BY t.resolution_hours)::numeric, 2)
                                                  AS p90_resolution_hours
    FROM fact_ticket t
    JOIN dim_service s USING (service_id)
    WHERE t.is_resolved AND t.status <> 'cancelled'
    GROUP BY 1, 2
),
service_types AS (
    SELECT DISTINCT service_type FROM dim_service
)
SELECT
    k.week_start,
    k.week_ending,
    st.service_type,
    COALESCE(o.tickets_opened, 0)                           AS tickets_opened,
    COALESCE(r.tickets_resolved, 0)                         AS tickets_resolved,
    COALESCE(r.breached_tickets, 0)                         AS breached_tickets,
    round(100.0 * (1 - COALESCE(r.breached_tickets, 0)::numeric
                       / NULLIF(r.tickets_resolved, 0)), 2) AS sla_compliance_pct,
    r.p50_resolution_hours,
    r.p90_resolution_hours
FROM v_weekly_kpi k
CROSS JOIN service_types st
LEFT JOIN opened   o ON o.ws = k.week_start AND o.service_type = st.service_type
LEFT JOIN resolved r ON r.ws = k.week_start AND r.service_type = st.service_type;
