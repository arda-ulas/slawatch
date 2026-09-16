-- Model risk scores joined to the ticket and its dimensions, for the dashboard.
--
-- v_ticket_risk: one row per scored ticket with the creation-time dimensions, the predicted
-- breach probability and band, the actual outcome where known, and which split of the
-- time-based evaluation the ticket fell in (train / validation / test / unlabelled).
--
-- v_risk_band_summary: per split x band, how many tickets were flagged and how many of the
-- labelled ones actually breached - the calibration check an account team can read directly.

CREATE OR REPLACE VIEW v_ticket_risk AS
SELECT
    t.ticket_id,
    t.creation_ts,
    date_trunc('month', t.creation_ts)::date AS creation_month,
    t.customer_id,
    c.customer_name,
    c.tier,
    c.industry,
    s.region,
    s.province,
    sv.service_type,
    t.assignment_group,
    t.severity,
    t.ticket_type,
    t.channel,
    t.status,
    t.active_outage_id IS NOT NULL        AS has_active_outage,
    t.open_backlog_at_creation,
    t.sla_target_hours,
    r.model_version,
    r.probability,
    r.risk_band,
    r.split,
    t.sla_breached,
    t.resolution_hours
FROM ticket_risk_score r
JOIN fact_ticket t USING (ticket_id)
JOIN dim_customer c USING (customer_id)
JOIN dim_site s USING (site_id)
JOIN dim_service sv USING (service_id);

CREATE OR REPLACE VIEW v_risk_band_summary AS
SELECT
    split,
    risk_band,
    count(*)                                                      AS tickets,
    count(*) FILTER (WHERE sla_breached IS NOT NULL)              AS tickets_with_outcome,
    count(*) FILTER (WHERE sla_breached)                          AS breached_tickets,
    round(100.0 * count(*) FILTER (WHERE sla_breached)
          / NULLIF(count(*) FILTER (WHERE sla_breached IS NOT NULL), 0), 2)
                                                                  AS observed_breach_rate_pct,
    round(100.0 * avg(probability), 2)                            AS mean_predicted_pct
FROM v_ticket_risk
GROUP BY split, risk_band
ORDER BY split, CASE risk_band WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END;
