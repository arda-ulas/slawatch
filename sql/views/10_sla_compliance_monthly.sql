-- SLA compliance by customer x service type x month.
-- Compliance denominator = tickets with a known outcome (resolved, or open and already
-- past their expected resolution date at the snapshot). Cancelled tickets are excluded.

CREATE OR REPLACE VIEW v_sla_compliance_monthly AS
WITH base AS (
    SELECT
        t.customer_id,
        c.customer_name,
        c.tier,
        c.industry,
        s.service_type,
        date_trunc('month', t.creation_ts)::date AS month,
        t.sla_breached,
        t.is_resolved,
        t.resolution_hours,
        t.sla_target_hours
    FROM fact_ticket t
    JOIN dim_customer c USING (customer_id)
    JOIN dim_service  s USING (service_id)
    WHERE t.status <> 'cancelled'
)
SELECT
    customer_id,
    customer_name,
    tier,
    industry,
    service_type,
    month,
    count(*)                                                       AS tickets,
    count(*) FILTER (WHERE is_resolved)                            AS resolved_tickets,
    count(*) FILTER (WHERE sla_breached IS NOT NULL)               AS tickets_with_outcome,
    count(*) FILTER (WHERE sla_breached)                           AS breached_tickets,
    round(100.0 * (1 - count(*) FILTER (WHERE sla_breached)::numeric
                       / NULLIF(count(*) FILTER (WHERE sla_breached IS NOT NULL), 0)), 2)
                                                                   AS sla_compliance_pct,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY resolution_hours)::numeric, 2)
                                                                   AS p50_resolution_hours,
    round(avg(resolution_hours / sla_target_hours) FILTER (WHERE is_resolved), 3)
                                                                   AS avg_target_utilisation
FROM base
GROUP BY customer_id, customer_name, tier, industry, service_type, month;
