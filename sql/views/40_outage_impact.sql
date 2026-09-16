-- Outage-incident impact: ticket volume and breach rate during each incident versus a
-- 28-day baseline for the same region + service type. Membership is derived from time,
-- region and service type here (independently of fact_ticket.active_outage_id).

CREATE OR REPLACE VIEW v_outage_impact AS
WITH ticket_geo AS (
    SELECT
        t.ticket_id,
        t.creation_ts,
        t.severity,
        t.sla_breached,
        si.region,
        sv.service_type
    FROM fact_ticket t
    JOIN dim_site    si USING (site_id)
    JOIN dim_service sv USING (service_id)
    WHERE t.status <> 'cancelled'
),
during AS (
    SELECT
        o.outage_id,
        count(*)                                                  AS tickets,
        count(*) FILTER (WHERE g.sla_breached)                    AS breached,
        count(*) FILTER (WHERE g.sla_breached IS NOT NULL)        AS with_outcome,
        count(*) FILTER (WHERE g.severity IN ('critical', 'major')) AS critical_major
    FROM outage_incident o
    JOIN ticket_geo g
      ON g.region = o.region
     AND g.service_type = o.service_type
     AND g.creation_ts >= o.start_ts
     AND g.creation_ts <  o.end_ts
    GROUP BY o.outage_id
),
baseline AS (
    SELECT
        o.outage_id,
        count(*)                                                  AS tickets,
        count(*) FILTER (WHERE g.sla_breached)                    AS breached,
        count(*) FILTER (WHERE g.sla_breached IS NOT NULL)        AS with_outcome
    FROM outage_incident o
    JOIN ticket_geo g
      ON g.region = o.region
     AND g.service_type = o.service_type
     AND g.creation_ts >= o.start_ts - interval '28 days'
     AND g.creation_ts <  o.start_ts
    GROUP BY o.outage_id
),
shaped AS (
    SELECT
        o.*,
        EXTRACT(EPOCH FROM (o.end_ts - o.start_ts)) / 3600.0 AS duration_hours,
        COALESCE(d.tickets, 0)        AS tickets_during,
        d.breached                    AS breached_during,
        d.with_outcome                AS outcome_during,
        d.critical_major,
        COALESCE(b.tickets, 0)        AS tickets_baseline,
        b.breached                    AS breached_baseline,
        b.with_outcome                AS outcome_baseline
    FROM outage_incident o
    LEFT JOIN during   d USING (outage_id)
    LEFT JOIN baseline b USING (outage_id)
)
SELECT
    outage_id,
    cause,
    region,
    service_type,
    start_ts,
    end_ts,
    round(duration_hours, 1)                                              AS duration_hours,
    tickets_during,
    round(tickets_during / duration_hours, 2)                             AS tickets_per_hour_during,
    round(tickets_baseline / (28 * 24.0), 3)                              AS tickets_per_hour_baseline,
    round((tickets_during / duration_hours)
          / NULLIF(tickets_baseline / (28 * 24.0), 0), 1)                 AS volume_multiplier,
    round(100.0 * breached_during / NULLIF(outcome_during, 0), 1)         AS breach_rate_during_pct,
    round(100.0 * breached_baseline / NULLIF(outcome_baseline, 0), 1)     AS breach_rate_baseline_pct,
    round(100.0 * critical_major / NULLIF(tickets_during, 0), 1)          AS critical_major_share_pct
FROM shaped
ORDER BY start_ts;

-- Whole-book comparison: tickets opened while an outage was active for their
-- region + service type versus everything else.
CREATE OR REPLACE VIEW v_outage_vs_normal AS
WITH flagged AS (
    SELECT
        t.*,
        EXISTS (
            SELECT 1
            FROM outage_incident o
            WHERE o.region = si.region
              AND o.service_type = sv.service_type
              AND t.creation_ts >= o.start_ts
              AND t.creation_ts <  o.end_ts
        ) AS during_outage
    FROM fact_ticket t
    JOIN dim_site    si USING (site_id)
    JOIN dim_service sv USING (service_id)
    WHERE t.status <> 'cancelled'
)
SELECT
    during_outage,
    count(*)                                                              AS tickets,
    round(100.0 * count(*) FILTER (WHERE sla_breached)
          / NULLIF(count(*) FILTER (WHERE sla_breached IS NOT NULL), 0), 2) AS breach_rate_pct,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY resolution_hours)::numeric, 2)
                                                                          AS p50_resolution_hours,
    round(100.0 * count(*) FILTER (WHERE severity IN ('critical', 'major')) / count(*), 1)
                                                                          AS critical_major_share_pct,
    round(avg(open_backlog_at_creation), 1)                               AS avg_backlog_at_creation
FROM flagged
GROUP BY during_outage;
