-- MTTR / p50 / p90 resolution hours by severity and service type, with subtotals.
-- GROUPING SETS produce the per-severity, per-service and overall rows in one pass;
-- 'all' marks the rolled-up dimension.

CREATE OR REPLACE VIEW v_resolution_stats AS
SELECT
    CASE WHEN GROUPING(t.severity) = 1     THEN 'all' ELSE t.severity END     AS severity,
    CASE WHEN GROUPING(s.service_type) = 1 THEN 'all' ELSE s.service_type END AS service_type,
    count(*)                                                                  AS resolved_tickets,
    round(avg(t.resolution_hours), 2)                                         AS mttr_hours,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY t.resolution_hours)::numeric, 2)
                                                                              AS p50_hours,
    round(percentile_cont(0.9) WITHIN GROUP (ORDER BY t.resolution_hours)::numeric, 2)
                                                                              AS p90_hours,
    round(avg(t.sla_target_hours), 2)                                         AS avg_target_hours,
    round(100.0 * count(*) FILTER (WHERE t.sla_breached) / count(*), 2)       AS breach_rate_pct
FROM fact_ticket t
JOIN dim_service s USING (service_id)
WHERE t.is_resolved
GROUP BY GROUPING SETS ((t.severity, s.service_type), (t.severity), (s.service_type), ());
